from __future__ import annotations

import asyncio
import logging
import hashlib
import json
import time
import re
from datetime import datetime, timezone
from html import escape

import httpx
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import Settings, load_settings
from formatting import telegram_html
from market_engine import MarketEngine, MarketSignal, should_publish_market, should_publish_pulse
from news_engine import NewsResult, fetch_important_news, format_news_post
from pulse_engine import format_pulse_post, generate_pulse
from visual_engine import build_market_card
from storage import SQLiteStorage


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("trd-pulse")


def event_key_for_signal(kind: str, signal: MarketSignal) -> str:
    """Stable short-window key for deduplicating repeated market signals."""
    breadth = getattr(signal, "breadth", {}) or {}
    payload = {
        "kind": kind,
        "bucket": int(time.time() // 3600),
        "regime": str(getattr(signal, "regime", "")),
        "score": getattr(signal, "score", None),
        "positive_pct": breadth.get("positive_pct"),
        "negative_pct": breadth.get("negative_pct"),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_admin(update: Update, settings: Settings) -> bool:
    return bool(update.effective_user and update.effective_user.id in settings.admin_user_ids)


async def admin_only(update: Update, settings: Settings) -> bool:
    """Проверяет доступ администратора и сообщает обычному пользователю об отказе."""
    if is_admin(update, settings):
        return True

    if update.effective_message:
        user = update.effective_user
        name = escape(user.first_name) if user and user.first_name else "пользователь"

        await update.effective_message.reply_text(
            f"🚫 <b>Доступ запрещён, {name}.</b>\n\n"
            "Вы не являетесь администратором.\n"
            "Эта команда доступна только администраторам TRD PULSE.",
            parse_mode=ParseMode.HTML,
        )

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
    state.setdefault("startup_notification_sent", False)
    state.setdefault("market_checks", 0)
    state.setdefault("news_checks", 0)
    state.setdefault("pulse_published", 0)
    state.setdefault("market_published", 0)
    state.setdefault("news_published", 0)
    return state


def get_settings(application: Application) -> Settings:
    return application.bot_data["settings"]


def get_engine(application: Application) -> MarketEngine:
    return application.bot_data["market_engine"]


def get_client(application: Application) -> httpx.AsyncClient:
    return application.bot_data["http_client"]


async def send_post(bot, chat_id, text: str, image_path=None, reply_markup=None):
    # Telegram HTML does not support \n. Engine formatters may produce it,
    # so normalize line-break tags at the final sending boundary.
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
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
    # MARKET is visual-first. Keep a compact caption so the command also works
    # when visual cards are disabled.
    return (
        f"<b>TRD / MARKET</b>\n"
        f"<b>{signal.regime.replace('_', ' ')}</b> · score {signal.score}\n"
        f"BTC {market.get('btc', {}).get('current_price', '—')} · "
        f"ETH {market.get('eth', {}).get('current_price', '—')}"
    )


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

    state = get_state(context.application)
    started = state["monitor_started_at"].strftime("%Y-%m-%d %H:%M UTC")
    await update.effective_message.reply_text(
        "<b>TRD PULSE</b>\n\n"
        "<blockquote>"
        "<b>ПАНЕЛЬ УПРАВЛЕНИЯ</b>\n\n"
        "<b>MARKET</b> — текущее состояние рынка\n"
        "<b>PULSE</b> — последний рыночный сигнал\n"
        "<b>NEWS</b> — важное событие\n"
        "<b>STATUS</b> — состояние системы\n\n"
        f"<b>Мониторинг:</b> активен\n"
        f"<b>Запуск:</b> {started}"
        "</blockquote>",
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
    except Exception as exc:
        logger.exception("Manual NEWS failed")
        await update.effective_message.reply_text(
            "Не удалось получить важную новость.\n\n"
            f"Ошибка: <code>{escape(type(exc).__name__)}</code>\n"
            "Подробности смотри в GitHub Actions → Run logs.",
            parse_mode=ParseMode.HTML,
        )


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
    except Exception as exc:
        logger.exception("Manual PULSE failed")
        await update.effective_message.reply_text(
            "Не удалось сформировать Pulse.\n\n"
            f"Ошибка: <code>{escape(type(exc).__name__)}</code>\n"
            "Подробности смотри в GitHub Actions → Run logs.",
            parse_mode=ParseMode.HTML,
        )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings(context.application)
    if not await admin_only(update, settings):
        return

    state = get_state(context.application)
    started = state["monitor_started_at"].strftime("%Y-%m-%d %H:%M UTC")
    admins = ", ".join(str(user_id) for user_id in settings.admin_user_ids)
    await update.effective_message.reply_text(
        "<b>TRD / SYSTEM STATUS</b>\n\n"
        "<blockquote>"
        f"<b>Система:</b> работает\n"
        f"<b>Запуск:</b> {started}\n"
        f"<b>Администраторы:</b> {len(settings.admin_user_ids)}\n"
        f"<b>IDs:</b> {admins}\n\n"
        f"<b>MARKET:</b> каждые {settings.market_check_interval // 60} мин\n"
        f"<b>NEWS:</b> каждые {settings.news_check_interval // 60} мин\n"
        f"<b>Последняя NEWS:</b> {'есть' if state['last_news_text'] else 'нет'}\n"
        f"<b>Проверок MARKET:</b> {state['market_checks']}\n"
        f"<b>Проверок NEWS:</b> {state['news_checks']}\n"
        f"<b>Публикаций MARKET:</b> {state['market_published']}\n"
        f"<b>Публикаций PULSE:</b> {state['pulse_published']}\n"
        f"<b>Публикаций NEWS:</b> {state['news_published']}"
        "</blockquote>",
        parse_mode=ParseMode.HTML,
    )


async def automatic_market_check(application: Application):
    settings = get_settings(application)
    state = get_state(application)

    state["market_checks"] += 1
    market, _, signal = await collect_market(application)
    now = asyncio.get_running_loop().time()

    market_event_key = event_key_for_signal("MARKET", signal)
    storage = application.bot_data["storage"]
    if (
        should_publish_market(signal)
        and not storage.is_duplicate("MARKET", market_event_key)
        and not storage.cooldown_active("MARKET", settings.market_cooldown)
    ):
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
        state["market_published"] += 1
        storage.mark_published("MARKET", market_event_key)
        logger.info("Automatic MARKET published")

    pulse_event_key = event_key_for_signal("PULSE", signal)
    if (
        should_publish_pulse(signal)
        and not storage.is_duplicate("PULSE", pulse_event_key)
        and not storage.cooldown_active("PULSE", settings.pulse_cooldown)
    ):
        try:
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
            state["pulse_published"] += 1
            storage.mark_published("PULSE", pulse_event_key)
            logger.info("Automatic PULSE published")
        except Exception:
            # Ошибка PULSE не должна блокировать последующие NEWS/MARKET проверки.
            logger.exception("Automatic PULSE failed; continuing monitor loop")


async def automatic_news_check(application: Application):
    settings = get_settings(application)
    state = get_state(application)
    now = asyncio.get_running_loop().time()

    storage = application.bot_data["storage"]
    if storage.cooldown_active("NEWS", settings.news_cooldown):
        return

    state["news_checks"] += 1
    try:
        news = await fetch_important_news(
            get_client(application),
            api_url=settings.openai_api,
            api_key=settings.openai_api_key,
            model=settings.openai_model,
        )
        state["last_news_at"] = now

        if news.importance < 9 or news.key.upper() == "NONE":
            return
        news_event_key = str(news.fingerprint or news.key or "").strip()
        if not news_event_key or storage.is_duplicate("NEWS", news_event_key):
            logger.info("Automatic NEWS skipped as duplicate")
            return

        text = format_news_post(news)
        await send_post(application.bot, settings.telegram_channel_id, text)
        state["last_news_text"] = text
        state["last_news_key"] = news.key
        state["last_news_hash"] = news.fingerprint
        state["news_published"] += 1
        storage.mark_published("NEWS", news_event_key)
        logger.info("Automatic NEWS published")
    except Exception:
        state["last_news_at"] = now
        logger.exception("Automatic NEWS failed; continuing monitor loop")


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


async def configure_commands(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Проверка доступа и панель управления"),
        BotCommand("market", "Состояние рынка"),
        BotCommand("pulse", "Рыночный сигнал"),
        BotCommand("news", "Важные новости"),
        BotCommand("status", "Состояние системы"),
    ])


async def send_startup_notification(application: Application):
    settings = get_settings(application)
    state = get_state(application)

    # A bot can message only users who have already opened/started it.
    text = (
        "<b>TRD PULSE</b>\n\n"
        "<blockquote>"
        "<b>SYSTEM ONLINE</b>\n\n"
        "<b>Статус:</b> работает\n"
        f"<b>Администраторов:</b> {len(settings.admin_user_ids)}\n"
        f"<b>MARKET:</b> каждые {settings.market_check_interval // 60} мин\n"
        f"<b>NEWS:</b> каждые {settings.news_check_interval // 60} мин\n"
        f"<b>Проверок MARKET:</b> {state['market_checks']}\n"
        f"<b>Проверок NEWS:</b> {state['news_checks']}\n"
        f"<b>Публикаций MARKET:</b> {state['market_published']}\n"
        f"<b>Публикаций PULSE:</b> {state['pulse_published']}\n"
        f"<b>Публикаций NEWS:</b> {state['news_published']}"
        "</blockquote>"
    )

    for user_id in settings.admin_user_ids:
        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=main_menu(),
            )
            logger.info("Startup notification sent to admin %s", user_id)
        except Exception:
            logger.exception("Startup notification failed for admin %s", user_id)


async def post_init(application: Application):
    settings = load_settings()
    application.bot_data["settings"] = settings
    application.bot_data["market_engine"] = MarketEngine(settings.coingecko_api)
    application.bot_data["http_client"] = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    application.bot_data["storage"] = SQLiteStorage()
    get_state(application)
    await configure_commands(application)
    await preflight_checks(application, settings)
    application.bot_data["monitor_task"] = asyncio.create_task(monitor_loop(application))
    logger.info("TRD Pulse initialized")
    await send_startup_notification(application)


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

    storage = application.bot_data.get("storage")
    if storage:
        storage.close()

    logger.info("TRD Pulse stopped")


async def preflight_checks(application: Application, settings: Settings):
    """Проверяет Telegram-конфигурацию до запуска фонового мониторинга."""
    logger.info("=== TRD PULSE PREFLIGHT ===")
    logger.info("Admin IDs: %s", ", ".join(map(str, settings.admin_user_ids)))
    logger.info("OpenAI model: %s", settings.openai_model)
    logger.info("Channel ID configured: yes")
    logger.info("OpenAI key configured: yes")

    me = await application.bot.get_me()
    logger.info("Telegram OK: @%s (id=%s)", me.username, me.id)

    # Реально проверяем OpenAI key + model через Responses API.
    # Запрос минимальный, чтобы ошибка была обнаружена до старта мониторинга.
    try:
        response = await get_client(application).post(
            settings.openai_api,
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.openai_model,
                "input": "Reply with exactly: OK",
                "max_output_tokens": 16,
            },
        )
        if response.status_code >= 400:
            detail = response.text[:500].replace("\n", " ")
            raise RuntimeError(f"HTTP {response.status_code}: {detail}")
        logger.info("OpenAI OK: model=%s", settings.openai_model)
    except Exception as exc:
        raise RuntimeError(
            "OpenAI preflight failed. Проверь OPENAI_API_KEY и OPENAI_MODEL. "
            f"Причина: {type(exc).__name__}: {exc}"
        ) from exc

    # Проверяем, что бот действительно может обратиться к каналу.
    # Не отправляем тестовое сообщение, чтобы не засорять канал.
    try:
        chat = await application.bot.get_chat(settings.telegram_channel_id)
        logger.info("Telegram channel OK: %s (%s)", chat.title or chat.username or chat.id, chat.id)
    except Exception as exc:
        raise RuntimeError(
            "Не удалось получить Telegram_CHANNEL. Проверь TELEGRAM_CHANNEL_ID "
            "и права бота в канале. "
            f"Причина: {type(exc).__name__}: {exc}"
        ) from exc


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
