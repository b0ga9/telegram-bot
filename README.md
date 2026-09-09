# TRD Pulse

Новая минимальная архитектура проекта.

## Контент

- **NEWS** — важное событие: факт + смысл.
- **PULSE** — короткая интерпретация сильного рыночного движения.
- **MARKET** — цифры и рыночные данные, подтверждающие контекст.

Удалено из архитектуры:

- LIVE BOARD
- отдельный PRICES-раздел
- `/publish`
- preview/editor workflow
- закреплённый постоянно обновляемый пост

## Структура

```text
bot.py              # Telegram + lifecycle + мониторинг
config.py           # настройки окружения
market_engine.py    # данные, история цен, breadth, regime, score
news_engine.py      # поиск и фильтрация важных новостей
pulse_engine.py     # короткая интерпретация market signal
visual_engine.py    # только MARKET-карточки
formatting.py       # форматирование
```

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Скопируй `.env.example` в `.env` и задай переменные окружения.

## Команды

- `/start`
- `/market`
- `/news`
- `/pulse`
- `/status`

## Важный принцип

`MARKET ENGINE` — центр системы.

Он получает данные, хранит историю, считает изменения, breadth,
regime и score.

- MARKET использует числа напрямую.
- PULSE получает сигнал рынка.
- NEWS работает отдельно и не выдумывает рыночные факты.

## Что стоит сделать следующим этапом

1. Добавить SQLite для дедупликации и cooldown после рестарта.
2. Добавить unit-тесты для market scoring.
3. Настроить нормальный постоянный деплой (VPS/Docker), а не бесконечный polling в GitHub Actions.
4. Проверить конкретный формат structured output OpenAI под используемый аккаунт/API и при необходимости заменить текущий JSON parsing на schema response format.
