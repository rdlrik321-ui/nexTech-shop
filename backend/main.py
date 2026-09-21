from __future__ import annotations

import json
import os
import pathlib
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

# --- НАСТРОЙКИ БЕЗОПАСНОСТИ NEXTECH ---
# Секреты берутся из .env (см. .env.example) — никогда не хардкодь их здесь,
# этот файл лежит в репозитории и может стать публичным.
load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
AUTH_KEY = os.environ["GIGACHAT_AUTH_KEY"]
SELLER_CHAT_ID = os.environ["SELLER_CHAT_ID"]

# ХРАНИЛИЩЕ ИСТОРИИ
chat_history = []

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


def user_from_init_data(authorization: str | None) -> dict | None:
    """Проверенные данные пользователя из initData или None, если подписи нет/она неверна."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    init_data = authorization.replace("Bearer ", "")
    if not verify_telegram_data(init_data, BOT_TOKEN):
        return None
    return json.loads(dict(parse_qsl(init_data)).get("user", "{}"))


def has_bought(user_id, product_id: str) -> bool:
    return any(o["user_id"] == user_id and product_id in o["product_ids"] for o in orders_store)


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
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        return hmac.compare_digest(expected_hash, received_hash)
    except Exception:
        return False


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

    text = (
        "🆕 <b>Новый заказ nexTech — покупатель сообщает об оплате по СБП</b>\n\n"
        f"👤 {buyer_name} ({contact})\n"
        f"🛒 {request.description}\n"
        f"💰 <b>{request.amount:.0f} ₽</b>\n\n"
        "⚠️ Проверьте поступление перевода в банке перед сборкой заказа."
    )

    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": SELLER_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"Telegram sendMessage error: {resp.status_code} {resp.text}")
        raise HTTPException(status_code=502, detail="Не удалось отправить уведомление продавцу.")

    orders_store.append({
        "user_id": buyer_id,
        "product_ids": request.product_ids,
        "amount": request.amount,
        "date": datetime.now().isoformat(timespec="seconds"),
    })
    ORDERS_FILE.write_text(json.dumps(orders_store, ensure_ascii=False, indent=2), encoding="utf-8")

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
    can_review = bool(user) and has_bought(user.get("id"), product_id)
    return {"reviews": list(reversed(items)), "average": average, "count": len(items), "can_review": can_review}


@app.options("/ask")
async def options_handler():
    """Обработка предварительных запросов браузера (CORS)"""
    return {"status": "ok"}


@app.post("/ask")
async def ask_ai(request: ChatRequest, authorization: str = Header(None)):
    """Защищенный эндпоинт общения с ИИ"""
    global chat_history
    
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

    # --- ЕСЛИ ВСЕ ОК, РАБОТАЕМ С GIGACHAT ---
    token = get_access_token()
    if not token:
        return {"reply": "Система nexTech: Ошибка авторизации в ядре ИИ."}

    chat_history.append({"role": "user", "content": request.message})
    
    if len(chat_history) > 10:
        chat_history.pop(0)

    system_prompt = """Ты — интеллектуальный ассистент магазина электроники nexTech. 
Твоя цель: консультировать клиентов по наличию и помогать с выбором комплектующих.

--- ПРАВИЛА ПОВЕДЕНИЯ (СТРОГО): ---
1. СТИЛЬ: Деловой, вежливый, на "Вы". Ответы должны быть лаконичными и по делу.
2. ПРИВЕТСТВИЕ: На "Привет" или "Здравствуйте" отвечай кратко: "Здравствуйте! Вас приветствует nexTech. Какое железо или периферия Вас интересует?". НЕ ПЕРЕЧИСЛЯЙ товары сразу.
3. БЕЗОПАСНОСТЬ: Категорически запрещено выдумывать акции, скидки, бонусы или подарки. Цены строго фиксированные.
4. СПАМ-ФИЛЬТР: Выдавай полный список товаров ТОЛЬКО по прямому запросу ("Что есть?", "Весь прайс", "Покажи ассортимент"). 
5. ФОКУС: Отвечай только на вопросы о ПК-железе. Если спрашивают о другом — вежливо вернись к теме магазина.

--- АКТУАЛЬНЫЙ ПРАЙС (ДАННЫЕ ДЛЯ КОНСУЛЬТАЦИИ): ---
* Видеокарта: Sapphire Nitro+ RX 580 (8GB) — 5 800 ₽
* Процессор: AMD Ryzen 5 5600X — 12 500 ₽
* Процессор: Intel Core i5-12400F — 11 500 ₽
* Мышь: VXE R1 SE — 3 200 ₽
* Мышь: Logitech G Pro X Superlight — 9 200 ₽

--- ЛОГИКА ПРОДАЖ: ---
- Если клиент сомневается, подчеркни надежность (например, "Sapphire Nitro+ — это топовое исполнение с отличным охлаждением").
- Если клиент выбрал товар и готов к покупке, ОБЯЗАТЕЛЬНО добавь в конце сообщения метку: [КУПИТЬ: Название товара].
- Если товара нет в списке выше — отвечай, что его сейчас нет в наличии."""

    messages_to_send = [{"role": "system", "content": system_prompt}] + chat_history

    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    payload = {
        "model": "GigaChat",
        "messages": messages_to_send,
        "temperature": 0.3
    }
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}'
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, verify=False)
        result = response.json()
        ai_reply = result['choices'][0]['message']['content']
        
        chat_history.append({"role": "assistant", "content": ai_reply})
        return {"reply": ai_reply}
    except Exception as e:
        print(f"Ошибка запроса: {e}")
        return {"reply": "Ошибка связи с ядром GigaChat."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)