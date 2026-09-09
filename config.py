from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_channel_id: str
    admin_user_ids: tuple[int, ...]

    openai_api_key: str
    openai_model: str

    visual_enabled: bool
    visual_dir: Path

    market_check_interval: int
    news_check_interval: int
    market_cooldown: int
    pulse_cooldown: int
    news_cooldown: int

    coingecko_api: str = "https://api.coingecko.com/api/v3"
    openai_api: str = "https://api.openai.com/v1/responses"


def _bool(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _required(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Не задана переменная окружения: {name}")
    return value


def _optional(name: str, default: str) -> str:
    value = (os.getenv(name) or "").strip()
    return value or default


def _admin_ids() -> tuple[int, ...]:
    # Primary secret: ADMIN_USER_IDS. Keep ADMIN_USER_ID as backward-compatible fallback.
    raw = (os.getenv("ADMIN_USER_IDS") or "").strip()
    if not raw:
        raw = (os.getenv("ADMIN_USER_ID") or "").strip()
    if not raw:
        raise RuntimeError(
            "Не задан администратор. Создай GitHub Secret ADMIN_USER_IDS "
            "(например: 123456789). ADMIN_USER_ID поддерживается для совместимости."
        )

    ids: list[int] = []
    for value in raw.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            ids.append(int(value))
        except ValueError as exc:
            raise RuntimeError(f"Некорректный Telegram ID администратора: {value}") from exc

    if not ids:
        raise RuntimeError("Не задан ни один администратор: ADMIN_USER_IDS / ADMIN_USER_ID")
    return tuple(dict.fromkeys(ids))


def load_settings() -> Settings:
    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        telegram_channel_id=_required("TELEGRAM_CHANNEL_ID"),
        admin_user_ids=_admin_ids(),
        openai_api_key=_required("OPENAI_API_KEY"),
        openai_model=_optional("OPENAI_MODEL", "gpt-5.6-luna"),
        visual_enabled=_bool("TRD_VISUAL_ENABLED"),
        visual_dir=Path(os.getenv("TRD_VISUAL_DIR") or "/tmp/trd_visuals"),
        market_check_interval=int(os.getenv("MARKET_CHECK_INTERVAL") or 600),
        news_check_interval=int(os.getenv("NEWS_CHECK_INTERVAL") or 3600),
        market_cooldown=int(os.getenv("MARKET_COOLDOWN") or 7200),
        pulse_cooldown=int(os.getenv("PULSE_COOLDOWN") or 3600),
        news_cooldown=int(os.getenv("NEWS_COOLDOWN") or 2700),
    )
