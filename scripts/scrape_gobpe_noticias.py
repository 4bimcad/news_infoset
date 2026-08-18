#!/usr/bin/env python3
"""
scrape_gobpe_noticias.py

Точечный scraper для страниц "Noticias" на gob.pe (MINEM, MINAM и
другие институции на той же платформе используют одинаковую вёрстку:
<li class="scrollable__item"> -> <a class="card__mock"> + <time datetime="...">).

Используется для источников с needs_custom_scraper: true в sources.yaml
(elperuano_normas сюда НЕ входит -- у него другая платформа/вёрстка,
понадобится отдельный scraper).

ВАЖНО: gob.pe отдаёт 418 (блокировка ботов) при простом requests.get()
с обычными заголовками. Нужен более полный набор заголовков браузера
и, если не поможет, задержка между запросами.
"""

from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
}


def scrape_gobpe_noticias(url: str, source_id: str, category: str, max_items: int = 20) -> list[dict]:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    items = []
    seen_urls = set()  # страница дублирует карточки (моб/десктоп версии карусели)

    for li in soup.select("li.scrollable__item"):
        link_tag = li.select_one("a.card__mock")
        time_tag = li.select_one("time")
        excerpt_tag = li.select_one(".flex-1.mb-6 .z-10.relative")
        img_tag = li.select_one("img")

        if not link_tag or not link_tag.get("href"):
            continue

        url_item = link_tag["href"]
        # ссылки на живой странице бывают относительными ("/institucion/...")
        # -- без этого браузер на infoset.org.pe достраивал бы их к своему
        # домену вместо gob.pe, отсюда были 404
        url_item = urljoin(url, url_item)

        if url_item in seen_urls:
            continue
        seen_urls.add(url_item)

        title = link_tag.get_text(strip=True)

        published_at = None
        if time_tag and time_tag.get("datetime"):
            try:
                dt = datetime.strptime(
                    time_tag["datetime"].split(".")[0], "%Y-%m-%d %H:%M:%S"
                )
                published_at = dt.replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                pass

        excerpt = excerpt_tag.get_text(strip=True) if excerpt_tag else ""
        image_url = urljoin(url, img_tag["src"]) if img_tag and img_tag.get("src") else None

        items.append(
            {
                "source": source_id,
                "category": category,
                "title": title,
                "url": url_item,
                "excerpt": excerpt[:300],
                "image_url": image_url,
                "published_at": published_at,
            }
        )

        if len(items) >= max_items:
            break

    return items


if __name__ == "__main__":
    # быстрый локальный тест
    import json

    result = scrape_gobpe_noticias(
        "https://www.gob.pe/institucion/minem/noticias",
        source_id="minem_noticias",
        category="normativa",
    )
    print(json.dumps(result[:3], ensure_ascii=False, indent=2))
    print(f"\nВсего найдено: {len(result)}")
