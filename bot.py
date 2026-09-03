import os
import logging
import httpx


TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
MODEL = os.getenv("OPENAI_MODEL", "gpt-5")

# Тема поста берётся из GitHub Actions через переменную POST_TOPIC.
POST_TOPIC = os.getenv("POST_TOPIC", "").strip()

if not TELEGRAM_TOKEN or not OPENAI_KEY or not CHANNEL_ID:
    raise RuntimeError(
        "Set TELEGRAM_BOT_TOKEN, OPENAI_API_KEY and TELEGRAM_CHANNEL_ID "
        "in GitHub Actions Secrets."
    )

if not POST_TOPIC:
    raise RuntimeError("Set POST_TOPIC in GitHub Actions variables.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

STYLE = """Ты редактор Telegram-канала. Пиши на русском.
Стиль: живой, современный, естественный, без канцелярита.
Не выдумывай факты. Если пользователь дал конкретные факты — не меняй их.
Делай готовый Telegram-пост: короткий цепляющий заголовок, основной текст,
при необходимости эмодзи, в конце 1-3 уместных хэштега.
Не добавляй фразы вроде «вот пост» или пояснения от себя."""


async def tg(method, data):
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(f"{TG}/{method}", json=data)
        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")

        return result


async def ai(prompt):
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

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json=body,
        )
        response.raise_for_status()
        data = response.json()

    # Responses API обычно возвращает удобное поле output_text.
    # Оставляем запасной разбор для совместимости.
    if data.get("output_text"):
        return data["output_text"].strip()

    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return content["text"].strip()

    raise RuntimeError("OpenAI returned an empty response.")


async def main():
    logging.info("Generating post for topic: %s", POST_TOPIC)

    post = await ai(POST_TOPIC)

    if not post:
        raise RuntimeError("Generated post is empty.")

    logging.info("Publishing post to Telegram channel.")

    await tg(
        "sendMessage",
        {
            "chat_id": CHANNEL_ID,
            "text": post,
        },
    )

    logging.info("Post published successfully.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
