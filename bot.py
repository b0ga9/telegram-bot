import os
import logging
import httpx
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # e.g. @my_channel
MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")  # optional, recommended

if not TELEGRAM_TOKEN or not OPENAI_KEY or not CHANNEL_ID:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN, OPENAI_API_KEY and TELEGRAM_CHANNEL_ID in .env")

logging.basicConfig(level=logging.INFO)

TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

STYLE = """Ты редактор Telegram-канала. Пиши на русском.
Стиль: живой, современный, естественный, без канцелярита.
Не выдумывай факты. Если пользователь дал конкретные факты — не меняй их.
Делай готовый Telegram-пост: короткий цепляющий заголовок, основной текст,
при необходимости эмодзи, в конце 1-3 уместных хэштега.
Не добавляй фразы вроде «вот пост» или пояснения от себя."""

drafts = {}

async def tg(method, data):
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{TG}/{method}", json=data)
        r.raise_for_status()
        return r.json()

async def ai(prompt):
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    body = {
        "model": MODEL,
        "instructions": STYLE,
        "input": prompt,
        "store": False,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
        return data["output"][0]["content"][0]["text"]

def allowed(user_id):
    return not ALLOWED_USER_ID or str(user_id) == str(ALLOWED_USER_ID)

async def handle_update(update):
    msg = update.get("message")
    if not msg or not msg.get("text"):
        return

    user_id = msg["from"]["id"]
    if not allowed(user_id):
        return

    chat_id = msg["chat"]["id"]
    text = msg["text"].strip()

    if text == "/start":
        await tg("sendMessage", {
            "chat_id": chat_id,
            "text": "Готов. Команды:\n/post <тема> — создать пост\n/publish — опубликовать последний пост\n/draft — показать черновик\n/cancel — удалить черновик"
        })
        return

    if text.startswith("/post "):
        prompt = text[6:].strip()
        if not prompt:
            await tg("sendMessage", {"chat_id": chat_id, "text": "Напиши тему после /post."})
            return
        await tg("sendMessage", {"chat_id": chat_id, "text": "Готовлю пост…"})
        try:
            result = await ai(prompt)
            drafts[user_id] = result
            await tg("sendMessage", {
                "chat_id": chat_id,
                "text": "📝 Черновик:\n\n" + result + "\n\nДля публикации: /publish"
            })
        except Exception as e:
            logging.exception(e)
            await tg("sendMessage", {"chat_id": chat_id, "text": f"Ошибка OpenAI: {e}"})
        return

    if text == "/draft":
        draft = drafts.get(user_id)
        await tg("sendMessage", {
            "chat_id": chat_id,
            "text": draft or "Черновика пока нет."
        })
        return

    if text == "/cancel":
        drafts.pop(user_id, None)
        await tg("sendMessage", {"chat_id": chat_id, "text": "Черновик удалён."})
        return

    if text == "/publish":
        draft = drafts.get(user_id)
        if not draft:
            await tg("sendMessage", {"chat_id": chat_id, "text": "Сначала создай пост через /post."})
            return
        try:
            await tg("sendMessage", {"chat_id": CHANNEL_ID, "text": draft})
            drafts.pop(user_id, None)
            await tg("sendMessage", {"chat_id": chat_id, "text": "✅ Опубликовано в канале."})
        except Exception as e:
            logging.exception(e)
            await tg("sendMessage", {
                "chat_id": chat_id,
                "text": "Не удалось опубликовать. Проверь, что бот добавлен в канал администратором с правом публикации."
            })
        return

    await tg("sendMessage", {
        "chat_id": chat_id,
        "text": "Используй /post <тема>, например:\n/post Напиши пост про новое обновление Roblox"
    })

async def main():
    offset = 0
    logging.info("Bot started")
    while True:
        data = await tg("getUpdates", {"timeout": 50, "offset": offset})
        for update in data.get("result", []):
            offset = update["update_id"] + 1
            try:
                await handle_update(update)
            except Exception:
                logging.exception("Update failed")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
