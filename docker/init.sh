#!/bin/bash
set -euo pipefail

: "${APP_DB_USER:?APP_DB_USER must be set in db.env}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD must be set in db.env}"

# Создаём отдельную роль приложения. Пароль приходит из локального db.env,
# а не хранится в Git или SQL-файле.
psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=app_db_user="$APP_DB_USER" \
  --set=app_db_password="$APP_DB_PASSWORD" \
  --set=app_db_name="$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_db_user', :'app_db_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'app_db_user')
\gexec

GRANT CONNECT ON DATABASE :"app_db_name" TO :"app_db_user";
GRANT USAGE ON SCHEMA public TO :"app_db_user";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"app_db_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO :"app_db_user";
SQL
