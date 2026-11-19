# ДЗ №6 — хранение секретов в HashiCorp Vault

Реализация для существующего Python-сервиса `postgres-pinger` (ДЗ №2).
Исходный `main` не изменяется: реализация находится в отдельной ветке
`hw6-vault` этого же репозитория.

## 0. Быстрый запуск (одной командой)

```bash
bash scripts/demo-up.sh
```

Скрипт идемпотентный: поднимает Docker, создаёт `db.env`/`pinger.env` при
отсутствии, запускает db и vault, заново настраивает Vault (policy, AppRole,
секрет), записывает актуальные role-id/secret-id в `pinger.env`, запускает
pinger и печатает статус. Это нужно потому, что dev mode хранит состояние
Vault только в памяти: после любого рестарта контейнера Vault настройку надо
повторять, а старые role-id/secret-id становятся недействительными.

Ниже — те же шаги вручную.

## 1. Подготовка

```bash
git checkout hw6-vault
cp db.env.example db.env
cp pinger.env.example pinger.env
```

В `db.env` задать собственные `POSTGRES_PASSWORD` и `APP_DB_PASSWORD`.
Запомнить значение `APP_DB_PASSWORD`: точно такой же пароль нужно записать
в Vault на следующем этапе. Не добавлять `db.env` и `pinger.env` в Git.

## 2. Запуск PostgreSQL и Vault

```bash
docker compose up --build -d db vault
docker compose ps
```

Vault доступен внутри Compose по `http://vault:8200`, с хоста — по
`http://localhost:8200`.

Для лабораторной работы используется dev mode с фиксированным учебным root
token `dev-only-root-token`. Это НЕ production-конфигурация. В dev mode
хранилище Vault находится в памяти; смонтированный `./vault/data:/vault/file`
выполняет требование задания о внешнем volume, но сам по себе не обеспечивает
персистентность данных при перезапуске dev-сервера.

## 3. Настройка Vault

Команды выполняются из корня проекта. Root token используется только для
администрирования Vault, он не передаётся приложению.

```bash
export VAULT_ADDR=http://localhost:8200
export VAULT_TOKEN=dev-only-root-token

# Включить KV v2
docker compose exec -e VAULT_TOKEN="$VAULT_TOKEN" vault \
  vault secrets enable -path=secret kv-v2

# Создать политику с правом чтения только одного секрета
docker compose exec -e VAULT_TOKEN="$VAULT_TOKEN" vault \
  vault policy write pinger-read /vault/policies/pinger-read.hcl

# Включить AppRole
docker compose exec -e VAULT_TOKEN="$VAULT_TOKEN" vault \
  vault auth enable approle

# Создать роль приложения
docker compose exec -e VAULT_TOKEN="$VAULT_TOKEN" vault \
  vault write auth/approle/role/pinger \
  token_policies="pinger-read" token_ttl="5m" token_max_ttl="15m"

# Записать логин и пароль приложения в KV v2.
# Подставить APP_DB_PASSWORD из локального db.env.
docker compose exec -e VAULT_TOKEN="$VAULT_TOKEN" vault \
  vault kv put secret/postgres username="app_user" password="ВАШ_APP_DB_PASSWORD"

# Получить Role ID и Secret ID
docker compose exec -e VAULT_TOKEN="$VAULT_TOKEN" vault \
  vault read -field=role_id auth/approle/role/pinger/role-id

docker compose exec -e VAULT_TOKEN="$VAULT_TOKEN" vault \
  vault write -f -field=secret_id auth/approle/role/pinger/secret-id
```

Скопировать полученные значения в `pinger.env`:
- `VAULT_ROLE_ID` — Role ID;
- `VAULT_SECRET_ID` — Secret ID;
- `VAULT_ADDR=http://vault:8200`;
- `VAULT_SECRET_PATH=postgres`;
- `VAULT_KV_MOUNT=secret`.

Secret ID является чувствительным значением. Не публиковать его в Git.

## 4. Запуск приложения

```bash
docker compose up --build -d pinger
docker compose logs -f pinger
```

Pinger перед каждым запросом к PostgreSQL:
1. Создаёт клиент Vault по `VAULT_ADDR`.
2. Выполняет AppRole login с Role ID и Secret ID.
3. Получает краткоживущий Vault token.
4. Читает `secret/postgres` через KV v2.
5. Использует полученные `username` и `password` для подключения к PostgreSQL.

Root token не используется приложением. Политика AppRole разрешает только чтение
`secret/data/postgres`.

## 5. Проверка правильного и неправильного пароля

Правильный пароль:

```bash
docker compose logs -f pinger
```

Ожидается `[OK] DB is up. Version: PostgreSQL 18...`.

Неправильный пароль — изменить значение в Vault, не меняя `db.env`:

```bash
docker compose exec -e VAULT_TOKEN="$VAULT_TOKEN" vault \
  vault kv put secret/postgres username="app_user" password="WRONG_PASSWORD"
```

В логах pinger должна появиться ошибка аутентификации PostgreSQL (`password
authentication failed`). Это показывает, что приложение действительно читает
секрет из Vault. Восстановить правильное значение:

```bash
docker compose exec -e VAULT_TOKEN="$VAULT_TOKEN" vault \
  vault kv put secret/postgres username="app_user" password="ВАШ_APP_DB_PASSWORD"
```

После следующей проверки pinger должен снова подключиться.

## 6. Важное замечание о PostgreSQL volume

PostgreSQL инициализирует роль приложения только при первом создании кластера.
Если база уже была создана со старым `init.sql`, init-скрипт повторно не
выполнится. Для чистого учебного стенда можно удалить старый контейнер и
volume БД, но это удалит данные PostgreSQL:

```bash
docker compose down -v
docker compose up --build -d db vault
```

Выполнять `down -v` только если данные БД не нужны.

## 7. Что показать преподавателю

1. `docker compose ps` — запущенные db, vault и pinger.
2. Файл `docker-compose.yml` с Vault и внешним volume `/vault/file`.
3. Политику `pinger-read` и AppRole `pinger`.
4. Секрет в Vault (значения пароля на демонстрации не раскрывать).
5. Успешное подключение pinger к PostgreSQL.
6. Ошибку при неправильном пароле и успешное восстановление после возврата
   правильного секрета.
