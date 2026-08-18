#!/usr/bin/env python3
"""
fetch_news.py

Читает config/sources.yaml, парсит RSS/scraper каждого источника,
нормализует поля, дедуплицирует и делает upsert в таблицу news_items
в Supabase через REST API (PostgREST) с service_role ключом.
Также чистит записи старше RETENTION_DAYS.

Переменные окружения (задаются как GitHub Actions secrets):
    SUPABASE_URL           -- https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY   -- service_role key (НЕ anon key!)

Запуск:
    pip install feedparser pyyaml requests deep-translator beautifulsoup4
    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python scripts/fetch_news.py
"""

import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import feedparser
import requests
import yaml
from deep_translator import GoogleTranslator

from scrape_gobpe_noticias import scrape_gobpe_noticias

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

MAX_ITEMS_PER_SOURCE = 20  # не тащим всю историю фида, только свежее
RETENTION_DAYS = 7         # старые новости чистим при каждом запуске


def require_env():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY не заданы", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Нормализация / дедупликация
# ---------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    """Нормализует заголовок для сравнения: убирает регистр, знаки
    препинания, диакритику и лишние пробелы. Нужно, чтобы поймать
    одну и ту же новость (пресс-релиз), опубликованную разными СМИ
    под идентичным или почти идентичным заголовком."""
    text = title.lower().strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def matches_keywords(title: str, excerpt: str, keywords: list[str]) -> bool:
    """Для широких фидов (Gestión, El Comercio, MINEM, MINAM) фильтруем
    по ключевым словам, чтобы не заливать таблицу нерелевантным потоком
    (курс доллара, электричество/топливо и т.п.)."""
    haystack = f"{title} {excerpt}".lower()
    return any(kw.lower() in haystack for kw in keywords)


def deduplicate_items(items: list[dict], existing_titles: set[str] | None = None) -> list[dict]:
    """Убирает дубли одной и той же новости с разных источников
    (например, один пресс-релиз, синдицированный Energiminas и Proactivo).
    Порядок источников в sources.yaml определяет приоритет -- оставляем
    первую встреченную версию. existing_titles -- заголовки, уже
    сохранённые в базе за последние дни (кросс-раневая защита)."""
    seen_titles = set(existing_titles or set())
    result = []
    for item in items:
        key = normalize_title(item["title"])
        if key in seen_titles:
            continue
        seen_titles.add(key)
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# RSS-парсинг
# ---------------------------------------------------------------------------

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
    html = entry.get("summary", "") or ""
    m = re.search(r'<img[^>]+src="([^"]+)"', html)
    return m.group(1) if m else None


def clean_excerpt(entry) -> str:
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


def extract_dynamic_source(entry, fallback_id: str) -> tuple[str, str]:
    """Для Google News (и похожих агрегаторов): достаёт реальное издание
    из тега <source> и убирает суффикс ' - Издание' из заголовка,
    который Google добавляет сам. Возвращает (source_label, чистый title)."""
    title = entry.get("title", "").strip()
    source_label = fallback_id

    src = entry.get("source")
    if src and isinstance(src, dict) and src.get("title"):
        source_label = src["title"].strip()
        suffix = f" - {source_label}"
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
    return source_label, title


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
        source_id = src["id"]

        if src.get("dynamic_source"):
            source_id, title = extract_dynamic_source(entry, fallback_id=src["id"])

        if keywords and not matches_keywords(title, excerpt, keywords):
            continue  # не про минерку — пропускаем

        if needs_translation:
            title = translate_to_spanish(title)
            excerpt = translate_to_spanish(excerpt)

        items.append(
            {
                "source": source_id,
                "category": src["category"],
                "title": title,
                "url": url,
                "excerpt": excerpt,
                "image_url": extract_image(entry),
                "published_at": parse_published(entry),
            }
        )
    return items


# ---------------------------------------------------------------------------
# Supabase: чтение существующих заголовков / запись / очистка
# ---------------------------------------------------------------------------

def fetch_recent_titles(days: int = 3) -> set[str]:
    """Подтягивает нормализованные заголовки уже сохранённых новостей за
    последние N дней -- нужно для дедупликации МЕЖДУ разными запусками
    (не только внутри одного забега cron), если разные СМИ публикуют
    один и тот же пресс-релиз не одновременно, а с разницей в час-два."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    endpoint = f"{SUPABASE_URL}/rest/v1/news_items"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    params = {"select": "title", "published_at": f"gte.{cutoff}"}
    try:
        r = requests.get(endpoint, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return {normalize_title(row["title"]) for row in r.json()}
    except requests.RequestException as e:
        print(f"WARN: не удалось получить существующие заголовки: {e}")
        return set()


def upsert_items(items: list[dict]):
    if not items:
        return
    # on_conflict=url — обязателен для настоящего upsert через PostgREST;
    # без него Prefer: resolution=ignore-duplicates не работает и любая
    # попытка вставить уже существующий url падает с ошибкой 409.
    endpoint = f"{SUPABASE_URL}/rest/v1/news_items?on_conflict=url"
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


def cleanup_old_news():
    """Удаляет новости старше RETENTION_DAYS -- держит таблицу компактной
    (важно на лимите 500 MB бесплатного Supabase) и страницу свежей."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    endpoint = f"{SUPABASE_URL}/rest/v1/news_items"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    params = {"published_at": f"lt.{cutoff}"}
    r = requests.delete(endpoint, headers=headers, params=params, timeout=30)
    if r.status_code not in (200, 204):
        print(f"WARN: cleanup failed [{r.status_code}]: {r.text[:300]}")
    else:
        print(f"Очистка: удалены новости старше {RETENTION_DAYS} дней")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    require_env()

    with open("config/sources.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    all_items = []
    for src in cfg["sources"]:
        if not src.get("feed_url") and src.get("scraper") != "gobpe":
            continue
        try:
            items = fetch_source(src)
            print(f"[OK] {src['id']}: {len(items)} записей")
            all_items.extend(items)
        except Exception as e:
            print(f"[ERROR] {src['id']}: {e}")

    before = len(all_items)
    existing_titles = fetch_recent_titles()
    all_items = deduplicate_items(all_items, existing_titles)
    print(f"\nДедупликация: {before} -> {len(all_items)} (убрано {before - len(all_items)} повторов)")

    upsert_items(all_items)
    cleanup_old_news()

    print(f"Готово. Загружено записей: {len(all_items)}")


if __name__ == "__main__":
    main()
