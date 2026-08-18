-- Схема для новостного блока infoset.org.pe
-- Рассчитана на Supabase Free tier (500 MB DB, unlimited API requests)

create table if not exists news_items (
  id            bigint generated always as identity primary key,
  source        text not null,              -- 'rumbominero', 'mining_com', ...
  category      text not null,              -- 'noticias' | 'normativa' | 'internacional'
  title         text not null,
  url           text not null unique,       -- защита от дублей при повторном upsert
  excerpt       text,
  image_url     text,
  published_at  timestamptz,
  fetched_at    timestamptz default now()
);

-- Индекс под основной паттерн запроса фронтенда: свежие новости по категории
create index if not exists idx_news_category_published
  on news_items (category, published_at desc);

-- Автоочистка: не даём таблице расти бесконечно (важно на 500 MB лимите).
-- Держим только последние 60 дней. Можно вызывать вручную или через pg_cron.
create or replace function cleanup_old_news() returns void as $$
  delete from news_items where published_at < now() - interval '60 days';
$$ language sql;

-- === Доступ ===
-- Frontend обращается через anon key, поэтому нужен RLS + explicit grant
-- (с 30 мая 2026 Supabase требует явные grants для PostgREST на новых
-- проектах, действующие проекты — начиная с 30 октября 2026).

alter table news_items enable row level security;

-- Публичное чтение всем (anon), запись запрещена -- запись идёт только
-- через service_role key из GitHub Actions, который RLS не обходит проверку.
create policy "Public read access"
  on news_items for select
  using (true);

grant select on news_items to anon;
grant select on news_items to authenticated;

-- service_role используется напрямую из GitHub Actions для upsert
-- и по умолчанию обходит RLS -- отдельный grant не нужен.
