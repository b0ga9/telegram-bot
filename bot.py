from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import Settings, load_settings
from formatting import telegram_html
from market_engine import MarketEngine, MarketSignal, should_publish_market, should_publish_pulse
from news_engine import NewsResult, fetch_important_news, format_news_post
from pulse_engine import format_pulse_post, generate_pulse
from visual_engine import build_market_card


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("trd-pulse")


def is_admin(update: Update, settings: Settings) -> bool:
    return bool(update.effective_user and update.effective_user.id == settings.admin_user_id)


async def admin_only(update: Update, settings: Settings) -> bool:
    if is_admin(update, settings):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Команда доступна только администратору.")
    return False


def get_state(application: Application) -> dict:
    state = application.bot_data
    state.setdefault("last_market_at", 0.0)
    state.setdefault("last_pulse_at", 0.0)
    state.setdefault("last_news_at", 0.0)
    state.setdefault("last_news_key", None)
    state.setdefault("last_news_hash", None)
    state.setdefault("last_news_text", "")
    state.setdefault("monitor_started_at", datetime.now(timezone.utc))
    return state


def get_settings(application: Application) -> Settings:
    return application.bot_data["settings"]


def get_engine(application: Application) -> MarketEngine:
    return application.bot_data["market_engine"]


def get_client(application: Application) -> httpx.AsyncClient:
    return application.bot_data["http_client"]


async def send_post(bot, chat_id, text: str, image_path=None, reply_markup=None):
    if image_path and len(text) <= 1024:
        await bot.send_photo(
            chat_id=chat_id,
            photo=image_path,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
        return

    if image_path:
        await bot.send_photo(chat_id=chat_id, photo=image_path)

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )


def format_market_post(market: dict, signal: MarketSignal) -> str:
    # MARKET is visual-first: the card itself is the post.
    return ""


async def collect_market(application: Application):
    client = get_client(application)
    engine = get_engine(application)
    now = asyncio.get_running_loop().time()
    market, coins = await asyncio.gather(
        engine.fetch_market(client),
        engine.fetch_monitored_coins(client),
    )
    engine.update_history(coins, now)
    signal = engine.analyze(coins, market, now)
    return market, coins, signal


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("MARKET"), KeyboardButton("PULSE")],
            [KeyboardButton("NEWS"), KeyboardButton("STATUS")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие",
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings(context.application)
    if not await admin_only(update, settings):
        return

    await update.effective_message.reply_text(
        "<b>TRD PULSE / CONTROL</b>\n\n"
        "<blockquote>Управление системой и ручной запуск модулей.<br><br>"
        "<b>MARKET</b> — данные в визуальных карточках<br>"
        "<b>PULSE</b> — краткий рыночный контекст<br>"
        "<b>NEWS</b> — только важные события<br>"
        "<b>STATUS</b> — состояние системы</blockquote>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings(context.application)
    if not await admin_only(update, settings):
        return

    text = (update.effective_message.text or "").strip()
    mapping = {
        "MARKET": "/market",
        "PULSE": "/pulse",
        "NEWS": "/news",
        "STATUS": "/status",
    }
    command = mapping.get(text)
    if not command:
        return

    # Route button actions to the same command handlers.
    if command == "/market":
        await market_command(update, context)
    elif command == "/pulse":
        await pulse_command(update, context)
    elif command == "/news":
        await news_command(update, context)
    elif command == "/status":
        await status_command(update, context)


async def market_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings(context.application)
    if not await admin_only(update, settings):
        return

    try:
        market, _, signal = await collect_market(context.application)
        image_path = None
        if settings.visual_enabled:
            image_path = build_market_card(market, signal, settings.visual_dir)
        await send_post(context.bot, update.effective_chat.id, format_market_post(market, signal), image_path)
    except Exception:
        logger.exception("Manual MARKET failed")
        await update.effective_message.reply_text("Не удалось получить данные рынка.")


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings(context.application)
    if not await admin_only(update, settings):
        return

    try:
        news = await fetch_important_news(
            get_client(context.application),
            api_url=settings.openai_api,
            api_key=settings.openai_api_key,
            model=settings.openai_model,
        )
        if news.importance < 7 or news.key.upper() == "NONE":
            await update.effective_message.reply_text("Сейчас нет новости, которая проходит фильтр важности.")
            return

        text = format_news_post(news)
        state = get_state(context.application)
        state["last_news_text"] = text
        state["last_news_key"] = news.key
        state["last_news_hash"] = news.fingerprint
        await send_post(context.bot, update.effective_chat.id, text)
    except Exception:
        logger.exception("Manual NEWS failed")
        await update.effective_message.reply_text("Не удалось получить важную новость.")


async def pulse_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings(context.application)
    if not await admin_only(update, settings):
        return

    try:
        market, _, signal = await collect_market(context.application)
        state = get_state(context.application)
        pulse = await generate_pulse(
            get_client(context.application),
            api_url=settings.openai_api,
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            market=market,
            signal=signal,
            recent_news=state["last_news_text"],
        )
        await send_post(
            context.bot,
            update.effective_chat.id,
            format_pulse_post(pulse, signal),
        )
    except Exception:
        logger.exception("Manual PULSE failed")
        await update.effective_message.reply_text("Не удалось сформировать Pulse.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings(context.application)
    if not await admin_only(update, settings):
        return

    state = get_state(context.application)
    started = state["monitor_started_at"].strftime("%Y-%m-%d %H:%M UTC")
    await update.effective_message.reply_text(
        "<b>TRD / SYSTEM STATUS</b>\n\n"
        "<blockquote>"
        f"<b>Монитор:</b> работает<br>"
        f"<b>Запуск:</b> {started}<br>"
        f"<b>MARKET:</b> каждые {settings.market_check_interval // 60} мин<br>"
        f"<b>NEWS:</b> каждые {settings.news_check_interval // 60} мин<br>"
        f"<b>Последняя NEWS:</b> {'есть' if state['last_news_text'] else 'нет'}"
        "</blockquote>",
        parse_mode=ParseMode.HTML,
    )


async def automatic_market_check(application: Application):
    settings = get_settings(application)
    state = get_state(application)

    market, _, signal = await collect_market(application)
    now = asyncio.get_running_loop().time()

    if should_publish_market(signal) and now - state["last_market_at"] >= settings.market_cooldown:
        image_path = None
        if settings.visual_enabled:
            image_path = build_market_card(market, signal, settings.visual_dir)
        await send_post(
            application.bot,
            settings.telegram_channel_id,
            format_market_post(market, signal),
            image_path,
        )
        state["last_market_at"] = now
        logger.info("Automatic MARKET published")

    if should_publish_pulse(signal) and now - state["last_pulse_at"] >= settings.pulse_cooldown:
        pulse = await generate_pulse(
            get_client(application),
            api_url=settings.openai_api,
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            market=market,
            signal=signal,
            recent_news=state["last_news_text"],
        )
        await send_post(
            application.bot,
            settings.telegram_channel_id,
            format_pulse_post(pulse, signal),
        )
        state["last_pulse_at"] = now
        logger.info("Automatic PULSE published")


async def automatic_news_check(application: Application):
    settings = get_settings(application)
    state = get_state(application)
    now = asyncio.get_running_loop().time()

    if now - state["last_news_at"] < settings.news_cooldown:
        return

    news = await fetch_important_news(
        get_client(application),
        api_url=settings.openai_api,
        api_key=settings.openai_api_key,
        model=settings.openai_model,
    )
    state["last_news_at"] = now

    if news.importance < 9 or news.key.upper() == "NONE":
        return
    if news.key == state["last_news_key"] or news.fingerprint == state["last_news_hash"]:
        logger.info("Automatic NEWS skipped as duplicate")
        return

    text = format_news_post(news)
    await send_post(application.bot, settings.telegram_channel_id, text)
    state["last_news_text"] = text
    state["last_news_key"] = news.key
    state["last_news_hash"] = news.fingerprint
    logger.info("Automatic NEWS published")


async def monitor_loop(application: Application):
    settings = get_settings(application)
    logger.info("TRD monitoring started")
    last_market_check = 0.0
    last_news_check = 0.0

    await asyncio.sleep(5)

    while True:
        try:
            now = asyncio.get_running_loop().time()

            if now - last_market_check >= settings.market_check_interval:
                await automatic_market_check(application)
                last_market_check = now

            if now - last_news_check >= settings.news_check_interval:
                await automatic_news_check(application)
                last_news_check = now

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Monitor loop error")

        await asyncio.sleep(15)


async def post_init(application: Application):
    settings = load_settings()
    application.bot_data["settings"] = settings
    application.bot_data["market_engine"] = MarketEngine(settings.coingecko_api)
    application.bot_data["http_client"] = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    get_state(application)
    application.bot_data["monitor_task"] = asyncio.create_task(monitor_loop(application))
    logger.info("TRD Pulse initialized")


async def post_shutdown(application: Application):
    task = application.bot_data.get("monitor_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    client = application.bot_data.get("http_client")
    if client:
        await client.aclose()

    logger.info("TRD Pulse stopped")


def main():
    settings = load_settings()

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("market", market_command))
    application.add_handler(CommandHandler("news", news_command))
    application.add_handler(CommandHandler("pulse", pulse_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))

    logger.info("Starting TRD Pulse")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
