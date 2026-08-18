#!/usr/bin/env python3
"""
fetch_news.py

Читает config/sources.yaml, парсит RSS каждого источника (feedparser),
нормализует поля и делает upsert в таблицу news_items в Supabase
через REST API (PostgREST) с service_role ключом.

Переменные окружения (задаются как GitHub Actions secrets):
    SUPABASE_URL           -- https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY    -- service_role key (НЕ anon key!)

Запуск:
    pip install feedparser pyyaml requests
    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python scripts/fetch_news.py
"""

import hashlib
import os
import sys
import time
from datetime import datetime, timezone

import feedparser
import requests
import yaml
from deep_translator import GoogleTranslator

from scrape_gobpe_noticias import scrape_gobpe_noticias

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

MAX_ITEMS_PER_SOURCE = 20  # не тащим всю историю фида, только свежее


def require_env():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY не заданы", file=sys.stderr)
        sys.exit(1)


def parse_published(entry) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            dt = datetime.fromtimestamp(time.mktime(val), tz=timezone.utc)
            return dt.isoformat()
    return None


def extract_image(entry) -> str | None:
    # media_content / media_thumbnail (частый паттерн у WordPress-сайтов)
    if "media_content" in entry and entry.media_content:
        return entry.media_content[0].get("url")
    if "media_thumbnail" in entry and entry.media_thumbnail:
        return entry.media_thumbnail[0].get("url")
    # иногда картинка зашита в content/summary как <img src="...">
    import re

    html = entry.get("summary", "") or ""
    m = re.search(r'<img[^>]+src="([^"]+)"', html)
    return m.group(1) if m else None


def clean_excerpt(entry) -> str:
    import re

    text = entry.get("summary", "") or ""
    text = re.sub(r"<[^>]+>", "", text)  # убрать HTML-теги
    text = text.strip()
    return text[:300]


def translate_to_spanish(text: str) -> str:
    """Переводит текст на испанский через бесплатный Google Translate
    (без API-ключа, через deep-translator). Если сервис недоступен --
    возвращает оригинал, чтобы не терять новость целиком."""
    if not text:
        return text
    try:
        return GoogleTranslator(source="auto", target="es").translate(text)
    except Exception as e:
        print(f"WARN: translation failed, keeping original: {e}")
        return text


def fetch_source(src: dict) -> list[dict]:
    keywords = src.get("filter_keywords")
    needs_translation = src.get("lang") == "en"

    # --- источники через scraper (не RSS) ---
    if src.get("scraper") == "gobpe":
        raw_items = scrape_gobpe_noticias(
            src["homepage"], source_id=src["id"], category=src["category"]
        )
        items = []
        for item in raw_items:
            if keywords and not matches_keywords(item["title"], item["excerpt"], keywords):
                continue
            items.append(item)
        return items

    # --- источники через RSS ---
    if not src.get("feed_url"):
        return []

    parsed = feedparser.parse(src["feed_url"])
    items = []
    for entry in parsed.entries[:MAX_ITEMS_PER_SOURCE]:
        url = entry.get("link")
        if not url:
            continue

        title = entry.get("title", "").strip()
        excerpt = clean_excerpt(entry)

        if keywords and not matches_keywords(title, excerpt, keywords):
            continue  # не про минерку — пропускаем

        if needs_translation:
            title = translate_to_spanish(title)
            excerpt = translate_to_spanish(excerpt)

        items.append(
            {
                "source": src["id"],
                "category": src["category"],
                "title": title,
                "url": url,
                "excerpt": excerpt,
                "image_url": extract_image(entry),
                "published_at": parse_published(entry),
            }
        )
    return items


def upsert_items(items: list[dict]):
    if not items:
        return
    endpoint = f"{SUPABASE_URL}/rest/v1/news_items"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        # игнорировать дубли по уникальному url вместо ошибки
        "Prefer": "resolution=ignore-duplicates,return=minimal",
    }
    # шлём батчами по 50, чтобы не упереться в лимиты payload
    batch_size = 50
    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        r = requests.post(endpoint, headers=headers, json=batch, timeout=30)
        if r.status_code not in (200, 201, 204):
            print(f"WARN: upsert batch failed [{r.status_code}]: {r.text[:300]}")


def main():
    require_env()

    with open("config/sources.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    total = 0
    for src in cfg["sources"]:
        if not src.get("feed_url") and src.get("scraper") != "gobpe":
            continue
        try:
            items = fetch_source(src)
            upsert_items(items)
            print(f"[OK] {src['id']}: {len(items)} записей")
            total += len(items)
        except Exception as e:
            print(f"[ERROR] {src['id']}: {e}")

    print(f"\nГотово. Обработано записей: {total}")


if __name__ == "__main__":
    main()
