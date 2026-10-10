from __future__ import annotations

import json
import os
import pathlib
import re
import secrets
import time
import requests
import uuid
import urllib3
import hmac
import hashlib
from datetime import datetime
from urllib.parse import parse_qsl
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware

# 1. ОТКЛЮЧАЕМ ПРЕДУПРЕЖДЕНИЯ (Для работы со Сбером)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 2. ИНИЦИАЛИЗАЦИЯ
app = FastAPI()

# 3. НАСТРОЙКА CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def register_telegram_webhook():
    """Говорит Telegram, куда слать нажатия кнопок «Оплата пришла» / «Отмена»."""
    if not PUBLIC_URL:
        print("PUBLIC_URL не задан в .env — кнопки подтверждения оплаты работать не будут.")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            json={
                "url": f"{PUBLIC_URL}/telegram-webhook",
                "secret_token": WEBHOOK_SECRET,
                "allowed_updates": ["callback_query", "message"],
            },
            timeout=10,
        )
        if not resp.json().get("ok"):
            print(f"Telegram отказал в регистрации вебхука: {resp.text}")
    except Exception as e:
        print(f"Не удалось зарегистрировать вебхук Telegram: {e}")


# --- НАСТРОЙКИ БЕЗОПАСНОСТИ NEXTECH ---
# Секреты берутся из .env (см. .env.example) — никогда не хардкодь их здесь,
# этот файл лежит в репозитории и может стать публичным.
load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Ключ GigaChat необязателен: без него магазин работает, ИИ-чат просто отвечает «недоступен».
AUTH_KEY = os.environ.get("GIGACHAT_AUTH_KEY", "")

# ИИ-консультант: любой OpenAI-совместимый провайдер (по умолчанию Google Gemini) → иначе GigaChat → иначе «недоступен».
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = (os.environ.get("LLM_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
# Можно перечислить несколько моделей через запятую — при сбое/перегрузке берётся следующая.
LLM_MODELS = [m.strip() for m in (os.environ.get("LLM_MODEL") or "gemini-3-flash-preview,gemini-flash-latest").split(",") if m.strip()]
SELLER_CHAT_ID = os.environ["SELLER_CHAT_ID"]
# Публичный адрес этого бэкенда (тот же, что и API_URL в index.html) — нужен,
# чтобы Telegram знал, куда слать нажатия кнопок «Оплата пришла» / «Отмена».
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
# Адрес самого мини-аппа (GitHub Pages) — для кнопки «Открыть магазин» в ответе на /start.
WEBAPP_URL = os.environ.get("WEBAPP_URL") or "https://rdlrik321-ui.github.io/nexTech-shop/"

# ИСТОРИЯ ЧАТА С ИИ — у каждого покупателя своя (по Telegram id)
chat_histories: dict[int, list[dict]] = {}

# --- ОТЗЫВЫ ---
# Реальные отзывы покупателей, ничего не выдумываем. Хранятся в файле рядом с бэкендом.
REVIEWS_FILE = pathlib.Path(__file__).parent / "reviews.json"


def load_reviews() -> dict:
    if REVIEWS_FILE.exists():
        try:
            return json.loads(REVIEWS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_reviews(data: dict) -> None:
    REVIEWS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


reviews_store: dict[str, list[dict]] = load_reviews()

# Отзывы могут оставлять только те, кто отправил заказ с этим товаром.
ORDERS_FILE = pathlib.Path(__file__).parent / "orders.json"


def load_orders() -> list:
    if ORDERS_FILE.exists():
        try:
            return json.loads(ORDERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


orders_store: list[dict] = load_orders()


def save_orders() -> None:
    ORDERS_FILE.write_text(json.dumps(orders_store, ensure_ascii=False, indent=2), encoding="utf-8")


def find_order(order_id: str) -> dict | None:
    return next((o for o in orders_store if o.get("id") == order_id), None)


# Секрет вебхука Telegram — генерируется заново при каждом запуске и тут же
# регистрируется в setWebhook, поэтому хранить его в .env не нужно.
WEBHOOK_SECRET = secrets.token_urlsafe(32)


def user_from_init_data(authorization: str | None) -> dict | None:
    """Проверенные данные пользователя из initData или None, если подписи нет/она неверна."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    init_data = authorization.replace("Bearer ", "")
    if not verify_telegram_data(init_data, BOT_TOKEN):
        return None
    return json.loads(dict(parse_qsl(init_data)).get("user", "{}"))


def has_reviewed(user_id, product_id: str) -> bool:
    return any(r.get("user_id") == user_id for r in reviews_store.get(product_id, []))


def has_bought(user_id, product_id: str) -> bool:
    """Покупкой считаем только заказ, который продавец подтвердил кнопкой «Оплата пришла»."""
    return any(
        o["user_id"] == user_id and product_id in o["product_ids"] and o.get("status") == "confirmed"
        for o in orders_store
    )


def verify_telegram_data(init_data: str, bot_token: str) -> bool:
    """Функция валидации данных от Telegram"""
    if not init_data:
        return False
    try:
        parsed_data = dict(parse_qsl(init_data))
        received_hash = parsed_data.get('hash')
        if not received_hash:
            return False
        
        parsed_data.pop('hash', None)
        if time.time() - int(parsed_data.get('auth_date', 0)) > 86400:
            return False
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        return hmac.compare_digest(expected_hash, received_hash)
    except Exception:
        return False


CATEGORY_LABELS = {"gpu": "Видеокарта", "cpu": "Процессор", "ram": "Память", "per": "Периферия"}


def catalog_text() -> str:
    """Прайс для ИИ берём из products.json, чтобы он не расходился с приложением."""
    try:
        products = json.loads((pathlib.Path(__file__).parent.parent / "products.json").read_text(encoding="utf-8"))
    except Exception:
        return "(каталог сейчас недоступен — отправляй клиента к менеджеру)"
    lines = []
    for p in products:
        stock = p.get("stock")
        note = "наличие уточняется" if stock is None else (f"в наличии {stock} шт" if stock > 0 else "под заказ")
        specs = "; ".join(f"{k}: {v}" for k, v in p.get("specs", []))
        lines.append(
            f"* {CATEGORY_LABELS.get(p['category'], p['category'])}: {p['name']} — "
            f"{format(p['price'], ',').replace(',', ' ')} ₽ ({note}); класс {p.get('grade', '?')}; {specs}"
        )
    return "\n".join(lines)


AI_UNAVAILABLE = "ИИ-консультант временно недоступен. По вопросам наличия и оплаты напишите менеджеру в разделе «Поддержка»."

SYSTEM_PROMPT = """Ты — ассистент магазина комплектующих и периферии для ПК nexTech (Томск). Консультируй по товарам из прайса и помогай с выбором.

--- ПРАВИЛА (СТРОГО): ---
1. Стиль: вежливо, на «Вы», коротко и по делу.
2. На приветствие отвечай кратко и спроси, что интересует. Не перечисляй весь ассортимент, пока не попросили.
3. Опирайся ТОЛЬКО на прайс ниже. Цены, характеристики и наличие не выдумывай. Не придумывай акции, скидки, подарки, сроки и гарантию.
4. Если товара нет в прайсе — скажи, что такого сейчас нет. Если наличие «уточняется» — не утверждай ни «есть», ни «нет», предложи уточнить у менеджера.
5. Отвечай только про ПК-железо и условия магазина. На другие темы вежливо верни разговор к магазину.
6. Если клиент готов купить конкретный товар, в конце ответа добавь метку: [КУПИТЬ: Название товара].
7. Называй товары точными названиями из прайса — под твоим ответом автоматически появятся карточки с фото и кнопкой «В корзину». Не используй таблицы и заголовки, для перечисления — короткие пункты. Не повторяй подробно то, что видно в карточке.

--- УСЛОВИЯ МАГАЗИНА (всё остальное — к менеджеру): ---
- Самовывоз в Томске. Оплата переводом по СБП, после перевода менеджер проверяет поступление и подтверждает заказ в чате.
- Классы состояния: S — абсолютный топ, новое железо или флагманы; A — идеальное состояние, минимальный износ, обслужено и протестировано; B — надёжный рабочий класс, проверенное б/у железо. Если класс другой — отправь уточнить у менеджера.
- Перед выдачей железо проходит проверку и стресс-тест.
- Про гарантию, сроки и доставку ничего не обещай: направляй к менеджеру в разделе «Поддержка».

--- ПРАЙС: ---
{catalog}"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def find_products(text: str, limit: int = 4) -> list:
    """id товаров каталога, упомянутых в тексте (в порядке появления)."""
    try:
        catalog = json.loads((pathlib.Path(__file__).parent.parent / "products.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    t, found = _norm(text), []
    for p in catalog:
        name = _norm(p["name"])
        keys = [name]
        head, _, tail = name.partition(" ")
        if len(tail) >= 8:
            keys.append(tail)  # то же название без первого слова-бренда
        positions = [t.find(k) for k in keys if k in t]
        if positions:
            found.append((min(positions), p["id"]))
    return [pid for _, pid in sorted(found)][:limit]


BUY_MARK = re.compile(r"\[\s*КУПИТЬ\s*:\s*([^\]]+)\]", re.I)


def split_reply(reply: str):
    """Убирает служебную метку [КУПИТЬ: …] и возвращает (текст, id упомянутых товаров, id к покупке)."""
    buy = next(iter([pid for m in BUY_MARK.finditer(reply) for pid in find_products(m.group(1), 1)]), None)
    clean = BUY_MARK.sub("", reply).strip()
    ids = find_products(clean)
    if buy and buy not in ids:
        ids = [buy] + ids[:3]
    return clean, ids, buy


def build_system_prompt() -> str:
    return SYSTEM_PROMPT.replace("{catalog}", catalog_text())


def ask_openai_compatible(messages: list) -> str:
    last_error = None
    for model in LLM_MODELS:
        try:
            resp = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": 0.3},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"ИИ: модель {model} не ответила: {e}")
            last_error = e
    raise last_error


def ask_gigachat(messages: list) -> str:
    token = get_access_token()
    if not token:
        raise RuntimeError("не удалось получить токен GigaChat")
    resp = requests.post(
        "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        json={"model": "GigaChat", "messages": messages, "temperature": 0.3},
        verify=False,
        timeout=40,
    )
    return resp.json()["choices"][0]["message"]["content"]


def get_access_token():
    """Получение временного токена от GigaChat"""
    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
        'Authorization': f'Basic {AUTH_KEY}',
        'RqUID': str(uuid.uuid4())
    }
    try:
        response = requests.post(url, headers=headers, data={'scope': 'GIGACHAT_API_PERS'}, verify=False)
        if response.status_code == 200:
            return response.json().get('access_token')
        return None
    except Exception as e:
        print(f"Ошибка токена: {e}")
        return None


class ChatRequest(BaseModel):
    message: str


class OrderRequest(BaseModel):
    amount: float
    description: str
    product_ids: list[str] = []


class ReviewRequest(BaseModel):
    product_id: str
    rating: int = Field(ge=1, le=5)
    text: str


@app.options("/create-order")
async def create_order_options():
    """Обработка предварительных запросов браузера (CORS)"""
    return {"status": "ok"}


@app.post("/create-order")
async def create_order(request: OrderRequest, authorization: str = Header(None)):
    """Приём заказа: проверяем клиента и уведомляем продавца в Telegram"""

    # ПРОВЕРКА КЛИЕНТА НА ВШИВОСТЬ 🛡️
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Система nexTech: Отказано в доступе. Токен авторизации отсутствует."
        )

    init_data = authorization.replace("Bearer ", "")

    if not verify_telegram_data(init_data, BOT_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Система nexTech: Критическая ошибка верификации. Доступ заблокирован."
        )

    # Достаём данные покупателя из подписанного initData
    parsed_data = dict(parse_qsl(init_data))
    user = json.loads(parsed_data.get("user", "{}"))
    buyer_name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "Без имени"
    buyer_username = user.get("username")
    buyer_id = user.get("id")
    contact = f"@{buyer_username}" if buyer_username else f'<a href="tg://user?id={buyer_id}">открыть чат</a>'

    order_id = uuid.uuid4().hex
    text = (
        "🆕 <b>Новый заказ nexTech — покупатель сообщает об оплате по СБП</b>\n\n"
        f"👤 {buyer_name} ({contact})\n"
        f"🛒 {request.description}\n"
        f"💰 <b>{request.amount:.0f} ₽</b>\n\n"
        "⚠️ Проверьте поступление перевода в банке, затем нажмите кнопку ниже."
    )

    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": SELLER_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [[
                {"text": "✅ Оплата пришла", "callback_data": f"confirm:{order_id}"},
                {"text": "❌ Отмена", "callback_data": f"cancel:{order_id}"},
            ]]},
        },
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"Telegram sendMessage error: {resp.status_code} {resp.text}")
        raise HTTPException(status_code=502, detail="Не удалось отправить уведомление продавцу.")

    seller_message_id = resp.json()["result"]["message_id"]

    orders_store.append({
        "id": order_id,
        "user_id": buyer_id,
        "product_ids": request.product_ids,
        "amount": request.amount,
        "description": request.description,
        "date": datetime.now().isoformat(timespec="seconds"),
        "status": "pending",
        "seller_message_id": seller_message_id,
        "seller_message_text": text,
    })
    save_orders()

    return {"ok": True, "order_id": order_id}


@app.post("/telegram-webhook")
async def telegram_webhook(update: dict, x_telegram_bot_api_secret_token: str = Header(None)):
    """Обрабатывает нажатия кнопок «Оплата пришла» / «Отмена» под заказом у продавца."""
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Неверный секрет вебхука.")

    msg = update.get("message")
    if msg and (msg.get("chat") or {}).get("type") == "private" and not (msg.get("from") or {}).get("is_bot"):
        if (msg.get("text") or "").strip().startswith("/start"):
            text = ("Добро пожаловать в nexTech — комплектующие и периферия для ПК.\n\n"
                    "Самовывоз в Томске, оплата переводом по СБП. Откройте каталог кнопкой ниже.")
        else:
            text = "Откройте магазин кнопкой ниже. Связаться с менеджером можно из магазина, раздел «Поддержка»."
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": msg["chat"]["id"],
                    "text": text,
                    "reply_markup": {"inline_keyboard": [[{"text": "Открыть магазин", "web_app": {"url": WEBAPP_URL}}]]},
                },
                timeout=10,
            )
        except Exception as e:
            print(f"Не удалось ответить на сообщение: {e}")
        return {"ok": True}

    cq = update.get("callback_query")
    if not cq:
        return {"ok": True}

    action, _, order_id = cq.get("data", "").partition(":")
    order = find_order(order_id)

    def answer(text: str, show_alert: bool = False) -> None:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": cq["id"], "text": text, "show_alert": show_alert},
            timeout=10,
        )

    if action not in ("confirm", "cancel") or not order:
        answer("Заказ не найден.", show_alert=True)
        return {"ok": True}
    if order["status"] != "pending":
        answer("Уже обработано.")
        return {"ok": True}

    if action == "confirm":
        order["status"] = "confirmed"
        status_line = "✅ ОПЛАТА ПОДТВЕРЖДЕНА"
        buyer_text = (
            f"✅ Оплата по заказу «{order['description']}» на {order['amount']:.0f} ₽ подтверждена.\n"
            "Менеджер свяжется, чтобы согласовать самовывоз в Томске."
        )
        answer("Отмечено как оплаченный.")
    else:
        order["status"] = "cancelled"
        status_line = "❌ ОТМЕНЕНО"
        buyer_text = (
            f"❌ Заказ «{order['description']}» на {order['amount']:.0f} ₽ отменён.\n"
            "Если это ошибка — напишите менеджеру в этом чате."
        )
        answer("Заказ отменён.")

    save_orders()

    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": order["user_id"], "text": buyer_text},
        timeout=10,
    )
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
        json={
            "chat_id": SELLER_CHAT_ID,
            "message_id": order["seller_message_id"],
            "text": f"{order['seller_message_text']}\n\n{status_line}",
            "parse_mode": "HTML",
        },
        timeout=10,
    )

    return {"ok": True}


@app.options("/reviews")
async def reviews_options():
    """Обработка предварительных запросов браузера (CORS)"""
    return {"status": "ok"}


@app.post("/reviews")
async def create_review(request: ReviewRequest, authorization: str = Header(None)):
    """Публикует отзыв покупателя. Автор — реальное имя/юзернейм из Telegram."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Система nexTech: Отказано в доступе. Токен авторизации отсутствует."
        )
    init_data = authorization.replace("Bearer ", "")
    if not verify_telegram_data(init_data, BOT_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Система nexTech: Критическая ошибка верификации. Доступ заблокирован."
        )

    user_for_check = json.loads(dict(parse_qsl(init_data)).get("user", "{}"))
    if not has_bought(user_for_check.get("id"), request.product_id):
        raise HTTPException(status_code=403, detail="Отзыв могут оставить только покупатели этого товара.")

    if has_reviewed(user_for_check.get("id"), request.product_id):
        raise HTTPException(status_code=409, detail="Вы уже оставили отзыв на этот товар.")

    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Текст отзыва не может быть пустым.")
    text = text[:500]

    parsed_data = dict(parse_qsl(init_data))
    user = json.loads(parsed_data.get("user", "{}"))
    author = (
        " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
        or (f"@{user.get('username')}" if user.get("username") else "Покупатель nexTech")
    )

    review = {
        "author": author,
        "rating": request.rating,
        "text": text,
        "date": datetime.now().strftime("%d.%m.%Y"),
        "user_id": user.get("id"),
    }
    reviews_store.setdefault(request.product_id, []).append(review)
    save_reviews(reviews_store)

    return review


@app.get("/reviews/{product_id}")
async def get_reviews(product_id: str, authorization: str = Header(None)):
    """Список отзывов по товару — публичный; can_review показывает, покупал ли этот пользователь товар."""
    items = reviews_store.get(product_id, [])
    average = round(sum(r["rating"] for r in items) / len(items), 1) if items else 0
    user = user_from_init_data(authorization)
    can_review = bool(user) and has_bought(user.get("id"), product_id) and not has_reviewed(user.get("id"), product_id)
    return {"reviews": list(reversed(items)), "average": average, "count": len(items), "can_review": can_review}


@app.options("/ask")
async def options_handler():
    """Обработка предварительных запросов браузера (CORS)"""
    return {"status": "ok"}


@app.post("/ask")
async def ask_ai(request: ChatRequest, authorization: str = Header(None)):
    """Защищенный эндпоинт общения с ИИ"""
    # ПРОВЕРКА КЛИЕНТА НА ВШИВОСТЬ 🛡️
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Система nexTech: Отказано в доступе. Токен авторизации отсутствует."
        )
    
    # Вытаскиваем чистый initData
    init_data = authorization.replace("Bearer ", "")
    
    # Сверяем хэш с подписью Telegram
    if not verify_telegram_data(init_data, BOT_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Система nexTech: Критическая ошибка верификации. Доступ заблокирован."
        )

    user_id = json.loads(dict(parse_qsl(init_data)).get("user", "{}")).get("id", 0)
    chat_history = chat_histories.setdefault(user_id, [])

    if not LLM_API_KEY and not AUTH_KEY:
        return {"reply": AI_UNAVAILABLE}

    chat_history.append({"role": "user", "content": request.message})
    if len(chat_history) > 10:
        chat_history.pop(0)

    messages = [{"role": "system", "content": build_system_prompt()}] + chat_history

    try:
        ai_reply = ask_openai_compatible(messages) if LLM_API_KEY else ask_gigachat(messages)
    except Exception as e:
        print(f"Ошибка запроса к ИИ: {e}")
        chat_history.pop()  # вопрос без ответа в историю не оставляем
        return {"reply": "Ошибка связи с ИИ. Попробуйте позже или напишите менеджеру в разделе «Поддержка»."}

    clean_reply, product_ids, buy_id = split_reply(ai_reply)
    chat_history.append({"role": "assistant", "content": clean_reply})
    return {"reply": clean_reply, "products": product_ids, "buy": buy_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)