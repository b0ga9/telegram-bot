import os
import logging
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

MODEL = os.getenv("OPENAI_MODEL") or "gpt-5.6-luna"
POST_TOPIC = os.getenv("POST_TOPIC", "").strip()

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
if not OPENAI_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")
if not CHANNEL_ID:
    raise RuntimeError("TELEGRAM_CHANNEL_ID is not set")
if not POST_TOPIC:
    raise RuntimeError("POST_TOPIC is not set")

STYLE = '''
Ты пишешь посты для Telegram-канала на русском языке.

Стиль:
- живой, понятный и современный русский язык;
- без канцелярита и лишней воды;
- короткие абзацы;
- сильный заголовок;
- допускаются уместные эмодзи;
- факты не выдумывай;
- если в исходной информации нет точного факта, не выдавай догадку за факт;
- не используй Markdown-таблицы;
- итоговый текст должен быть готов к публикации в Telegram.
'''.strip()

async def tg(method: str, data: dict):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=data)
        if response.status_code >= 400:
            logging.error("Telegram API error %s: %s", response.status_code, response.text[:2000])
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")
        return result

async def ai(prompt: str) -> str:
    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {OPENAI_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": MODEL,
        "instructions": STYLE,
        "input": prompt,
        "store": False,
    }

    logging.info("Using OpenAI model: %s", MODEL)

    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(url, headers=headers, json=body)
        if response.status_code >= 400:
            logging.error("OpenAI API error %s: %s", response.status_code, response.text[:2000])
        response.raise_for_status()
        data = response.json()

    text = data.get("output_text")
    if text:
        return text.strip()

    parts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(content["text"])

    text = "\n".join(parts).strip()
    if not text:
        raise RuntimeError(f"OpenAI returned no text: {data}")
    return text

async def main():
    logging.info("Generating post for topic: %s", POST_TOPIC)
    post = await ai(POST_TOPIC)
    logging.info("Publishing post to %s", CHANNEL_ID)
    await tg("sendMessage", {"chat_id": CHANNEL_ID, "text": post})
    logging.info("Post published successfully")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
