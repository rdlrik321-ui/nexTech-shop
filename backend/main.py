import json
import os
import requests
import uuid
import urllib3
import hmac
import hashlib
from urllib.parse import parse_qsl
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel
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

# --- ЮKASSA (СБП) ---
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY", "")
# Куда вернуть покупателя после оплаты (диплинк на бота/мини-апп) — задай в .env
YOOKASSA_RETURN_URL = os.environ.get("YOOKASSA_RETURN_URL", "https://t.me")
YOOKASSA_API = "https://api.yookassa.ru/v3/payments"

# ХРАНИЛИЩЕ ИСТОРИИ
chat_history = []

# Платежи в памяти: payment_id -> {status, amount, description, buyer_name, contact, notified}
# Переживает только один запуск процесса — для продакшена перенести в БД.
payments_store: dict[str, dict] = {}


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


class PaymentRequest(BaseModel):
    amount: float
    description: str


def require_telegram_user(authorization: str | None) -> dict:
    """Общая проверка initData для платёжных эндпоинтов. Возвращает данные покупателя."""
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
    parsed_data = dict(parse_qsl(init_data))
    return json.loads(parsed_data.get("user", "{}"))


def notify_seller_of_payment(payment_id: str) -> None:
    """Шлёт продавцу уведомление об оплаченном заказе ровно один раз."""
    payment = payments_store.get(payment_id)
    if not payment or payment.get("notified"):
        return

    buyer_name = payment.get("buyer_name") or "Без имени"
    contact = payment.get("contact") or ""
    text = (
        "✅ <b>Оплачен заказ nexTech (СБП)</b>\n\n"
        f"👤 {buyer_name} ({contact})\n"
        f"🛒 {payment['description']}\n"
        f"💰 <b>{payment['amount']:.0f} ₽</b>\n"
        f"🧾 Платёж: {payment_id}"
    )
    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": SELLER_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )
    if resp.status_code == 200:
        payment["notified"] = True
    else:
        print(f"Telegram sendMessage error (payment): {resp.status_code} {resp.text}")


def fetch_yookassa_payment(payment_id: str) -> dict:
    """Источник правды по статусу — всегда спрашиваем саму ЮKassa, а не доверяем чужим данным."""
    resp = requests.get(
        f"{YOOKASSA_API}/{payment_id}",
        auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
        timeout=10,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Не удалось получить статус платежа в ЮKassa.")
    return resp.json()


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
        "🆕 <b>Новый заказ nexTech</b>\n\n"
        f"👤 {buyer_name} ({contact})\n"
        f"🛒 {request.description}\n"
        f"💰 <b>{request.amount:.0f} ₽</b>"
    )

    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": SELLER_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"Telegram sendMessage error: {resp.status_code} {resp.text}")
        raise HTTPException(status_code=502, detail="Не удалось отправить уведомление продавцу.")

    return {"ok": True}


@app.options("/create-payment")
async def create_payment_options():
    """Обработка предварительных запросов браузера (CORS)"""
    return {"status": "ok"}


@app.post("/create-payment")
async def create_payment(request: PaymentRequest, authorization: str = Header(None)):
    """Создаёт платёж в ЮKassa (СБП/карта) и возвращает ссылку на оплату"""
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Приём онлайн-оплаты пока не настроен.")

    user = require_telegram_user(authorization)
    buyer_name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "Без имени"
    buyer_username = user.get("username")
    buyer_id = user.get("id")
    contact = f"@{buyer_username}" if buyer_username else f'<a href="tg://user?id={buyer_id}">открыть чат</a>'

    body = {
        "amount": {"value": f"{request.amount:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": YOOKASSA_RETURN_URL},
        "description": request.description[:128],
    }
    resp = requests.post(
        YOOKASSA_API,
        json=body,
        auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
        headers={"Idempotence-Key": str(uuid.uuid4())},
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        print(f"YooKassa create payment error: {resp.status_code} {resp.text}")
        raise HTTPException(status_code=502, detail="ЮKassa отклонила запрос на оплату.")

    payment = resp.json()
    payments_store[payment["id"]] = {
        "status": payment["status"],
        "amount": request.amount,
        "description": request.description,
        "buyer_name": buyer_name,
        "contact": contact,
        "notified": False,
    }

    return {
        "payment_id": payment["id"],
        "confirmation_url": payment["confirmation"]["confirmation_url"],
    }


@app.get("/payment-status/{payment_id}")
async def payment_status(payment_id: str, authorization: str = Header(None)):
    """Опрос статуса оплаты со стороны мини-аппа. Источник правды — сама ЮKassa."""
    require_telegram_user(authorization)

    if payment_id not in payments_store:
        raise HTTPException(status_code=404, detail="Платёж не найден.")

    fresh = fetch_yookassa_payment(payment_id)
    payments_store[payment_id]["status"] = fresh["status"]

    if fresh["status"] == "succeeded":
        notify_seller_of_payment(payment_id)

    return {"status": fresh["status"]}


@app.post("/yookassa-webhook")
async def yookassa_webhook(payload: dict):
    """Необязательный быстрый путь уведомления — сам статус всё равно перепроверяем в ЮKassa API,
    поэтому вебхуку не нужно доверять напрямую (его никто криптографически не подписывает)."""
    payment_id = (payload.get("object") or {}).get("id")
    if not payment_id or payment_id not in payments_store:
        return {"ok": True}

    fresh = fetch_yookassa_payment(payment_id)
    payments_store[payment_id]["status"] = fresh["status"]
    if fresh["status"] == "succeeded":
        notify_seller_of_payment(payment_id)

    return {"ok": True}


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