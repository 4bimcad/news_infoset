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
from difflib import SequenceMatcher

import feedparser
import requests
import yaml
from deep_translator import GoogleTranslator

from scrape_gobpe_noticias import scrape_gobpe_noticias

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

MAX_ITEMS_PER_SOURCE = 20  # не тащим всю историю фида, только свежее
RETENTION_DAYS = 7         # старые новости чистим при каждом запуске

# Многие сайты режут запросы без нормального User-Agent (особенно с облачных IP)
USER_AGENT = "Mozilla/5.0 (compatible; InfosetNewsBot/1.0; +https://infoset.org.pe)"

TRANSLATE_PAUSE = 0.4  # сек между запросами к переводчику (лимит Google ~5/с)


def require_env():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY не заданы", file=sys.stderr)
        sys.exit(1)


def supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }


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


TITLE_SIMILARITY_THRESHOLD = 0.85  # выше -- дубль (одна новость от разных СМИ)


def titles_similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= TITLE_SIMILARITY_THRESHOLD


def deduplicate_items(items: list[dict], existing_titles: set[str] | None = None) -> list[dict]:
    """Убирает дубли одной и той же новости с разных источников
    (например, один пресс-релиз, синдицированный Energiminas и El Comercio,
    иногда с небольшими отличиями в формулировке -- "US$ 40.000 millones"
    vs "US$40.000 millones"). Сравнение НЕЧЁТКОЕ (similarity ratio),
    а не точное совпадение строк -- иначе такие варианты проскакивают.
    Порядок источников в sources.yaml определяет приоритет -- оставляем
    первую встреченную версию. existing_titles -- заголовки, уже
    сохранённые в базе за последние дни (кросс-раневая защита)."""
    seen_titles = list(existing_titles or [])
    result = []
    for item in items:
        key = normalize_title(item["title"])
        if any(titles_similar(key, seen) for seen in seen_titles):
            continue
        seen_titles.append(key)
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# RSS-парсинг
# ---------------------------------------------------------------------------

def parse_published(entry) -> str:
    """Дата публикации в ISO (UTC). Если у записи даты нет -- ставим
    текущее время, иначе такие записи никогда не попадут под очистку
    (published_at = null) и будут копиться в базе."""
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            # feedparser отдаёт struct_time в UTC; calendar.timegm корректнее mktime
            import calendar
            dt = datetime.fromtimestamp(calendar.timegm(val), tz=timezone.utc)
            return dt.isoformat()
    return datetime.now(timezone.utc).isoformat()


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


def translate_to_spanish(text: str, retries: int = 3) -> str:
    """Переводит текст на испанский через бесплатный Google Translate
    (без API-ключа, через deep-translator). Между запросами пауза, при
    ошибке (в том числе 'too many requests') -- повтор с нарастающей
    задержкой. Если не вышло -- возвращает оригинал, чтобы не терять
    новость целиком."""
    if not text:
        return text
    for attempt in range(retries):
        try:
            result = GoogleTranslator(source="auto", target="es").translate(text)
            time.sleep(TRANSLATE_PAUSE)
            return result
        except Exception as e:
            wait = 2 * (2 ** attempt)  # 2с, 4с, 8с
            print(
                f"WARN: перевод, попытка {attempt + 1}/{retries}: "
                f"{str(e)[:80]} (жду {wait}с)"
            )
            time.sleep(wait)
    print("WARN: перевод не удался, оставляю оригинал")
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


def fetch_source(src: dict, known_urls: set[str]) -> list[dict]:
    keywords = src.get("filter_keywords")
    needs_translation = src.get("lang") == "en"

    # --- источники через scraper (не RSS) ---
    if src.get("scraper") == "gobpe":
        raw_items = scrape_gobpe_noticias(
            src["homepage"], source_id=src["id"], category=src["category"]
        )
        items = []
        n_total = len(raw_items)
        n_known = n_kw = 0
        for item in raw_items:
            if item.get("url") in known_urls:
                n_known += 1
                continue
            if keywords and not matches_keywords(item["title"], item["excerpt"], keywords):
                n_kw += 1
                continue
            items.append(item)
        print(
            f"  {src['id']}: со страницы={n_total}, уже в базе={n_known}, "
            f"не прошли фильтр={n_kw}, новых={len(items)}"
        )
        return items

    # --- источники через RSS ---
    if not src.get("feed_url"):
        return []

    parsed = feedparser.parse(src["feed_url"], agent=USER_AGENT)
    status = parsed.get("status")
    if parsed.bozo or (status and status >= 400) or not parsed.entries:
        print(
            f"  [FEED?] {src['id']}: http={status} bozo={parsed.bozo} "
            f"exc={str(parsed.get('bozo_exception'))[:100]} "
            f"entries={len(parsed.entries)}"
        )

    items = []
    n_feed = n_known = n_kw = 0
    for entry in parsed.entries[:MAX_ITEMS_PER_SOURCE]:
        n_feed += 1
        url = entry.get("link")
        if not url:
            continue
        if url in known_urls:  # уже в базе: не переводим и не тащим повторно
            n_known += 1
            continue

        title = entry.get("title", "").strip()
        excerpt = clean_excerpt(entry)
        source_id = src["id"]

        if src.get("dynamic_source"):
            source_id, title = extract_dynamic_source(entry, fallback_id=src["id"])

        if keywords and not matches_keywords(title, excerpt, keywords):
            n_kw += 1
            continue  # не про минерку -- пропускаем

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

    print(
        f"  {src['id']}: в ленте={n_feed}, уже в базе={n_known}, "
        f"не прошли фильтр={n_kw}, новых={len(items)}"
    )
    return items


# ---------------------------------------------------------------------------
# Supabase: чтение существующих данных / запись / очистка
# ---------------------------------------------------------------------------

def fetch_recent_column(column: str, days: int) -> list[str]:
    """Читает колонку (title / url) у новостей за последние N дней.
    Постранично, потому что PostgREST по умолчанию отдаёт максимум
    1000 строк за запрос."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    endpoint = f"{SUPABASE_URL}/rest/v1/news_items"
    page_size = 1000
    offset = 0
    values: list[str] = []
    while True:
        params = {
            "select": column,
            "published_at": f"gte.{cutoff}",
            "order": "id.asc",
            "limit": str(page_size),
            "offset": str(offset),
        }
        r = requests.get(endpoint, headers=supabase_headers(), params=params, timeout=30)
        r.raise_for_status()
        rows = r.json()
        values.extend(row[column] for row in rows if row.get(column))
        if len(rows) < page_size:
            break
        offset += page_size
    return values


def fetch_recent_titles(days: int = 3) -> set[str]:
    """Нормализованные заголовки уже сохранённых новостей за последние N
    дней -- для дедупликации МЕЖДУ разными запусками (если разные СМИ
    публикуют один и тот же пресс-релиз не одновременно, а с разницей
    в час-два)."""
    try:
        return {normalize_title(t) for t in fetch_recent_column("title", days)}
    except requests.RequestException as e:
        print(f"WARN: не удалось получить существующие заголовки: {e}")
        return set()


def fetch_recent_urls(days: int = RETENTION_DAYS) -> set[str]:
    """URL уже сохранённых новостей -- чтобы не тратить запросы на
    перевод того, что уже есть в базе."""
    try:
        return set(fetch_recent_column("url", days))
    except requests.RequestException as e:
        print(f"WARN: не удалось получить существующие URL: {e}")
        return set()


def upsert_items(items: list[dict]):
    if not items:
        return
    # on_conflict=url -- обязателен для настоящего upsert через PostgREST;
    # без него Prefer: resolution=ignore-duplicates не работает и любая
    # попытка вставить уже существующий url падает с ошибкой 409.
    endpoint = f"{SUPABASE_URL}/rest/v1/news_items?on_conflict=url"
    headers = {
        **supabase_headers(),
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
    params = {"published_at": f"lt.{cutoff}"}
    r = requests.delete(endpoint, headers=supabase_headers(), params=params, timeout=30)
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

    known_urls = fetch_recent_urls()
    print(f"Уже в базе за {RETENTION_DAYS} дн.: {len(known_urls)} URL\n")

    all_items = []
    for src in cfg["sources"]:
        if not src.get("feed_url") and src.get("scraper") != "gobpe":
            continue
        try:
            items = fetch_source(src, known_urls)
            print(f"[OK] {src['id']}: {len(items)} новых записей")
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