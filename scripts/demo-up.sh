#!/usr/bin/env bash
# ДЗ №6: подъём демо-стенда одной командой.
#
# Vault запущен в dev mode, поэтому его состояние (policy, AppRole, секрет,
# role-id/secret-id) живёт только в памяти контейнера. Этот скрипт каждый раз
# заново настраивает Vault и записывает актуальные role-id/secret-id в pinger.env.
#
# Скрипт идемпотентный: его можно запускать повторно, в том числе на уже
# поднятом стенде и на существующем кластере PostgreSQL.
#
# Использование:  bash scripts/demo-up.sh
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT=$(pwd)
VAULT_TOKEN="dev-only-root-token"
VAULT_ROLE="pinger"
SECRET_PATH="postgres"
export VAULT_TOKEN

say()  { printf '\n=== %s\n' "$*"; }
ok()   { printf '[OK] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; }
die()  { printf '[ERR] %s\n' "$*" >&2; exit 1; }

vc() { docker compose exec -T -e VAULT_TOKEN="$VAULT_TOKEN" vault vault "$@"; }

wait_healthy() {
  local name="$1" status
  for _ in $(seq 1 40); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null)
    [ "$status" = "healthy" ] && return 0
    sleep 3
  done
  return 1
}

say "1/7 Docker daemon"
if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
  warn "daemon не отвечает, запускаю Docker Desktop"
  powershell.exe -NoProfile -Command "Start-Process 'C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe'" >/dev/null 2>&1
  for _ in $(seq 1 40); do
    docker version --format '{{.Server.Version}}' >/dev/null 2>&1 && break
    sleep 10
  done
fi
docker version --format '{{.Server.Version}}' >/dev/null 2>&1 || die "Docker daemon не поднялся"
ok "docker $(docker version --format '{{.Server.Version}}')"

say "2/7 env-файлы"
if [ ! -f db.env ]; then
  warn "db.env отсутствует, генерирую пароли"
  {
    echo "POSTGRES_DB=appdb"
    echo "POSTGRES_USER=app_admin"
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 16)"
    echo "APP_DB_USER=app_user"
    echo "APP_DB_PASSWORD=$(openssl rand -hex 16)"
  } > db.env
fi
[ -f pinger.env ] || cp pinger.env.example pinger.env
APP_USER=$(grep '^APP_DB_USER=' db.env | cut -d= -f2)
APP_PW=$(grep '^APP_DB_PASSWORD=' db.env | cut -d= -f2)
[ -n "${APP_USER:-}" ] && [ -n "${APP_PW:-}" ] || die "APP_DB_USER/APP_DB_PASSWORD не заданы в db.env"
ok "учётка приложения: $APP_USER (пароль из db.env, длина ${#APP_PW})"

say "3/7 PostgreSQL + Vault"
docker compose up --build -d db vault || die "compose up db vault"
wait_healthy pinger_db   || die "pinger_db не стал healthy"
wait_healthy pinger_vault || die "pinger_vault не стал healthy"
ok "pinger_db и pinger_vault healthy"

say "4/7 настройка Vault"
if vc secrets list 2>/dev/null | grep -q '^secret/'; then
  ok "KV v2 на secret/ уже включён"
else
  vc secrets enable -path=secret kv-v2 || die "не удалось включить KV v2"
fi

vc policy write pinger-read /vault/policies/pinger-read.hcl >/dev/null || die "policy write"
ok "policy pinger-read загружена"

if vc auth list 2>/dev/null | grep -q '^approle/'; then
  ok "AppRole уже включён"
else
  vc auth enable approle >/dev/null || die "auth enable approle"
fi

vc write "auth/approle/role/$VAULT_ROLE" \
  token_policies="pinger-read" token_ttl="5m" token_max_ttl="15m" >/dev/null || die "role write"
ok "AppRole $VAULT_ROLE: pinger-read, ttl 5m/15m"

vc kv put "secret/$SECRET_PATH" username="$APP_USER" password="$APP_PW" >/dev/null || die "kv put"
ok "секрет secret/$SECRET_PATH записан"

ROLE_ID=$(vc read -field=role_id "auth/approle/role/$VAULT_ROLE/role-id" | tr -d '\r\n ')
SECRET_ID=$(vc write -f -field=secret_id "auth/approle/role/$VAULT_ROLE/secret-id" | tr -d '\r\n ')
[ -n "$ROLE_ID" ] && [ -n "$SECRET_ID" ] || die "не удалось получить role-id/secret-id"

say "5/7 pinger.env"
cat > pinger.env <<EOF
PINGER_INTERVAL=5
PINGER_CONNECT_TIMEOUT=10
PINGER_LOG_FILE=/var/log/pinger.log

VAULT_ADDR=http://vault:8200
VAULT_ROLE_ID=$ROLE_ID
VAULT_SECRET_ID=$SECRET_ID
VAULT_SECRET_PATH=$SECRET_PATH
VAULT_KV_MOUNT=secret
EOF
ok "pinger.env обновлён (role-id ${ROLE_ID:0:8}..., secret-id ${SECRET_ID:0:8}...)"

say "6/7 проверка пароля приложения в PostgreSQL"
if docker compose exec -T -e PGPASSWORD="$APP_PW" db \
     psql -h 127.0.0.1 -U "$APP_USER" -d appdb -tAc 'select 1' >/dev/null 2>&1; then
  ok "пароль из db.env подходит для $APP_USER"
else
  warn "пароль не подходит (старый кластер), выставляю его через app_admin"
  docker compose exec -T db psql -U app_admin -d appdb \
    -c "ALTER ROLE \"$APP_USER\" WITH PASSWORD '$APP_PW';" >/dev/null || die "ALTER ROLE"
  docker compose exec -T -e PGPASSWORD="$APP_PW" db \
    psql -h 127.0.0.1 -U "$APP_USER" -d appdb -tAc 'select 1' >/dev/null || die "пароль всё ещё не подходит"
  ok "пароль выставлен и проверен"
fi

say "7/7 запуск pinger"
docker compose up --build -d pinger || die "compose up pinger"
sleep 8

docker compose ps
printf '\n--- pinger ---\n'
docker compose logs --tail=6 pinger | grep -E '\[(OK|ERR)\]' || docker compose logs --tail=10 pinger

if docker compose logs --tail=20 pinger | grep -q '\[OK\] DB is up'; then
  ok "pinger читает секрет из Vault и подключается к PostgreSQL"
else
  warn "в логах пока нет '[OK] DB is up', смотри: docker compose logs -f pinger"
fi

cat <<'HINT'

--- демонстрация неверного секрета ---
export VAULT_TOKEN=dev-only-root-token
docker compose exec -e VAULT_TOKEN="$VAULT_TOKEN" vault \
  vault kv put secret/postgres username=app_user password=WRONG_PASSWORD
docker compose logs --tail=5 pinger     # [ERR] ... не прошёл проверку подлинности (по паролю)

--- возврат правильного секрета ---
docker compose exec -e VAULT_TOKEN="$VAULT_TOKEN" vault \
  vault kv put secret/postgres username=app_user password="$(grep '^APP_DB_PASSWORD=' db.env | cut -d= -f2)"
docker compose logs --tail=5 pinger     # снова [OK] DB is up
HINT
