# Новостной блок для infoset.org.pe

Агрегатор новостей по горной добыче Перу (+ немного международных) в стиле ukr.net.
Полностью бесплатный стек: Supabase Free + GitHub Actions + статический фронтенд.

## Как это работает

```
GitHub Actions (cron, каждые 45 мин)
        │
        ▼
scripts/fetch_news.py  ──►  парсит RSS источников (config/sources.yaml)
        │
        ▼
Supabase Postgres (news_items)  ──►  upsert через service_role key
        │
        ▼
Главная страница infoset.org.pe  ──►  читает через anon key (RLS: только SELECT)
```

## Шаги запуска

### 1. Создать таблицу в Supabase

В существующем (или новом) Supabase-проекте открой SQL Editor и выполни
содержимое `sql/schema.sql`.

### 2. Определить рабочие RSS-адреса

```bash
pip install requests beautifulsoup4 pyyaml feedparser
python scripts/discover_feeds.py
```

Скрипт пройдётся по доменам из `config/sources.yaml`, у которых
`feed_url: null`, и попробует найти рабочий RSS. Результат — вставить
обратно в `config/sources.yaml`.

Для источников, где RSS не найдётся (вероятно El Peruano — Normas Legales,
возможно Andina/Gestión) — под них нужен отдельный точечный scraper
(структура HTML у каждого сайта своя, унифицировать нельзя). Дай знать,
когда дойдёшь до этого шага — соберём отдельный scraper под конкретную
страницу.

### 3. Настроить GitHub Actions

В репозитории: Settings → Secrets and variables → Actions → New repository secret:

- `SUPABASE_URL` — например `https://xxxx.supabase.co`
- `SUPABASE_SERVICE_KEY` — **service_role** key (Project Settings → API).
  Не anon key! Он должен остаться только в GitHub Secrets, никогда не
  попадать во фронтенд.

Workflow `.github/workflows/fetch-news.yml` уже настроен на запуск
каждые 45 минут + вручную через вкладку Actions.

### 4. Вставить блок на главную страницу

В `frontend/news-block.html` заменить:

```js
const SUPABASE_URL = "https://YOUR_PROJECT.supabase.co";
const SUPABASE_ANON_KEY = "YOUR_ANON_KEY"; // Project Settings → API → anon/public
```

и вставить весь блок в HTML главной страницы infoset.org.pe (плюс
`<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>`
перед `</body>`, если его ещё нет).

## Почему это бесплатно и не сломается

- **Supabase Free**: 500 MB БД (заголовки+ссылки — это килобайты, не мегабайты
  на тысячи записей), unlimited API requests, 5 GB egress/мес — с запасом
  для новостного виджета на главной странице.
- **Авто-пауза Supabase** (7 дней без обращений к БД) не грозит: cron
  каждые 45 минут сам держит проект живым.
- **GitHub Actions**: в публичном репозитории — безлимитно; в приватном —
  2000 бесплатных минут/месяц, скрипт занимает секунды, укладывается
  с огромным запасом.
- **cleanup_old_news()** в schema.sql чистит записи старше 60 дней —
  таблица не растёт бесконечно.

## Важно на будущее (не блокирует сейчас)

Supabase с 30 мая 2026 требует явные `grant` для PostgREST-доступа на
**новых** проектах (для существующих — правило вступает в силу 30 октября
2026). В `sql/schema.sql` grants уже добавлены — если увидишь ошибки
доступа через anon key ближе к этой дате, первым делом проверь именно это.
