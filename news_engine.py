from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import httpx


@dataclass
class NewsResult:
    importance: int
    key: str
    title: str
    summary: str
    why_it_matters: str
    sources: list[str]

    @property
    def fingerprint(self) -> str:
        raw = f"{self.key}|{self.title}|{self.summary}".lower()
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


NEWS_PROMPT = """
Ты — редактор Telegram-канала TRD Pulse.

Найди за последние 24 часа ОДНО действительно важное событие,
которое может заметно повлиять на мировые рынки, Bitcoin, Ethereum
или другие риск-активы.

Приоритет:
1. ФРС / ЕЦБ / ставки
2. инфляция / занятость / макроданные
3. доллар / облигации / доходности
4. крупные движения фондового рынка
5. Bitcoin / Ethereum / ETF / важная регуляция
6. крупные геополитические события с реальным рыночным эффектом

Не публикуй:
- мелкие новости;
- слухи;
- рекламные материалы;
- обычные движения цены без важной причины;
- повтор уже известного события.

Проверь событие минимум по 2 независимым источникам.
Не выдумывай факты и URL.

Верни ТОЛЬКО JSON:
{
  "importance": 0,
  "key": "короткий стабильный идентификатор события или NONE",
  "title": "очень короткий заголовок",
  "summary": "1-2 коротких предложения: что произошло",
  "why_it_matters": "1 короткое предложение: почему это важно",
  "sources": ["https://...", "https://..."]
}

importance: 0-10.
Если важного события нет, верни importance 0, key NONE и пустые поля.
"""


async def fetch_important_news(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    api_key: str,
    model: str,
) -> NewsResult:
    payload = {
        "model": model,
        "input": NEWS_PROMPT,
        "tools": [{"type": "web_search"}],
        "max_output_tokens": 900,
    }
    response = await client.post(
        api_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    response.raise_for_status()
    data = response.json()

    chunks: list[str] = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"}:
                    chunks.append(content.get("text", ""))

    raw = "".join(chunks).strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()

    parsed = json.loads(raw)
    sources = [str(url) for url in parsed.get("sources", []) if str(url).startswith("http")][:3]

    return NewsResult(
        importance=max(0, min(10, int(parsed.get("importance", 0)))),
        key=str(parsed.get("key", "NONE")).strip() or "NONE",
        title=str(parsed.get("title", "")).strip(),
        summary=str(parsed.get("summary", "")).strip(),
        why_it_matters=str(parsed.get("why_it_matters", "")).strip(),
        sources=sources,
    )


def format_news_post(news: NewsResult) -> str:
    lines = ["📰 **TRD NEWS**", "", f"**{news.title}**", ""]
    if news.summary:
        lines.append(news.summary)
    if news.why_it_matters:
        lines.extend(["", f"⚡ **Почему важно:** {news.why_it_matters}"])
    if news.sources:
        lines.extend(["", "Источники:"])
        lines.extend([f"• {source}" for source in news.sources])
    return "\n".join(lines)
