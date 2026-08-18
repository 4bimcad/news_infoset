#!/usr/bin/env python3
"""
discover_feeds.py

Прогоняет список доменов из config/sources.yaml (у которых feed_url: null)
и пробует стандартные пути RSS-фидов + парсит <link rel="alternate"
type="application/rss+xml"> из HTML главной страницы.

ВАЖНО: этот скрипт нужно запускать НЕ из песочницы Claude (у неё нет
доступа к произвольным доменам), а локально у себя или в GitHub Actions
(в этом репозитории всё равно будет открытый доступ в интернет).

Запуск:
    pip install requests beautifulsoup4 pyyaml feedparser
    python scripts/discover_feeds.py

Результат: печатает найденные feed_url для каждого источника и
предлагает готовый YAML-фрагмент, который нужно вставить обратно
в config/sources.yaml.
"""

import re
import sys
from urllib.parse import urljoin

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

COMMON_PATHS = [
    "/feed/",
    "/feed",
    "/rss/",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/atom.xml",
    "/?feed=rss2",
    "/index.xml",
]


def looks_like_feed(text: str) -> bool:
    """Грубая проверка: похоже ли содержимое на валидный RSS/Atom."""
    head = text[:500].lower()
    return "<rss" in head or "<feed" in head or "<?xml" in head


def try_common_paths(homepage: str) -> str | None:
    for path in COMMON_PATHS:
        url = urljoin(homepage.rstrip("/") + "/", path.lstrip("/"))
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code == 200 and looks_like_feed(r.text):
                parsed = feedparser.parse(r.text)
                if parsed.entries:
                    return url
        except requests.RequestException:
            continue
    return None


def try_html_link_tag(homepage: str) -> str | None:
    try:
        r = requests.get(homepage, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        link = soup.find(
            "link",
            attrs={"type": re.compile("rss|atom", re.I)},
        )
        if link and link.get("href"):
            feed_url = urljoin(homepage, link["href"])
            r2 = requests.get(feed_url, headers=HEADERS, timeout=10)
            if r2.status_code == 200 and looks_like_feed(r2.text):
                return feed_url
    except requests.RequestException:
        pass
    return None


def discover(homepage: str) -> str | None:
    return try_html_link_tag(homepage) or try_common_paths(homepage)


def main():
    with open("config/sources.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    results = {}
    for src in cfg["sources"]:
        if src.get("feed_url"):
            continue  # уже подтверждён
        if src.get("needs_custom_scraper"):
            print(f"[SKIP] {src['id']} — требует отдельного scraper'а (не RSS)")
            continue

        print(f"[...] проверяю {src['id']} ({src['homepage']})")
        feed_url = discover(src["homepage"])
        if feed_url:
            print(f"[OK]   {src['id']} -> {feed_url}")
            results[src["id"]] = feed_url
        else:
            print(f"[FAIL] {src['id']} — RSS не найден, нужен точечный scraper")

    print("\n--- Вставь это в config/sources.yaml вместо feed_url: null ---\n")
    for src_id, feed_url in results.items():
        print(f"{src_id}: {feed_url}")


if __name__ == "__main__":
    sys.exit(main())
