# PostgreSQL pinger / heartbeat (Задание №2)

Сервис-«сердцебиение» поверх PostgreSQL 18. Каждые N секунд (по умолчанию раз в
5 минут) отправляет в БД запрос `SELECT VERSION();` и пишет результат:

- успешное подключение/ответ БД → **stdout** (`[OK] ... Version: PostgreSQL 18.x ...`);
- ошибка подключения/запроса → **stderr** (`[ERR] ...`);
- «нетипичный» ответ на запрос версии, но без ошибки → **stdout** (`[OK] connected: unusual ...`).

Приложение **не зависает** при сетевых проблемах: каждое подключение идёт с
`connect_timeout`. Ошибка не отменяет следующую проверку — цикл продолжается через
интервал `PINGER_INTERVAL`.

## Быстрый старт через Docker Compose

```bash
# соберёт образы (БД PostgreSQL 18 + пингер) и поднимет оба сервиса
docker compose up --build -d

# посмотреть stdout/stderr пингера
docker compose logs -f pinger
```

Compose использует `env_file: ./pinger.env` для передачи переменных среды
(пункты 1.5–1.7). Дубликат логов при этом пишется в `./logs/pinger.log`
(монтируется из `/var/log` контейнера).

### Тегированный образ

Образ пингера собирается с тегом версии:

```bash
docker build -t postgres-pinger:0.1.0 .
```

(в `docker-compose.yml` уже задано `image: postgres-pinger:0.1.0`).

## Демонстрация сценариев

1. **Успешное подключение** — БД доступна, пингер пишет `[OK] DB is up. Version: PostgreSQL 18.6 ...`.
2. **Неуспешное первое + успешное второе подключение**: запустить `pinger` пока
   БД ещё не поднялась (или остановить контейнер `db`), увидеть `[ERR]` в stderr,
   затем запустить/поднять БД — следующая проверка через интервал даст `[OK]`.
   Пример «вручную»:
   ```bash
   # первый запуск без БД -> ошибки в stderr, но процесс продолжает цикл
   PINGER_INTERVAL=5 PGUSER=app_user PGPASSWORD=user_secret python src/pinger.py --config config/app.conf
   ```
   затем поднимите БД (`docker compose up -d db`) и через `PINGER_INTERVAL` появится `[OK]`.

## Переменные среды

| Переменная                    | Значение по умолчанию | Описание                                        | Требование |
|-------------------------------|-----------------------|-------------------------------------------------|------------|
| `PINGER_INTERVAL`             | `300`                 | период опроса, сек                              | 1.5        |
| `PINGER_CONNECT_TIMEOUT`      | `10`                  | таймаут подключения, сек                        | защита     |
| `PGUSER`                      | —                     | логин БД (обязателен)                           | 1.6        |
| `PGPASSWORD`                  | —                     | пароль БД (обязателен)                          | 1.6        |
| `PINGER_LOG_FILE`             | унаследовано          | путь к файлу для дублирования логов (необязателен) | 1.7      |
| `DB_HOST` / `DB_PORT` / `DB_NAME` | из конфига          | необязательные переопределения конфига         | —          |

Статические настройки (host/port/dbname/sslmode) читаются из `config/app.conf`
(TOML, рядом с приложением). Поля проверяются по «белому списку», как в задании №1,
поэтому их нельзя дополнить собственной опцией извне.

## Структура

```
├── docker-compose.yml               # БД + пингер, env_file -> ./pinger.env
├── docker/
│   ├── Dockerfile.db                # PostgreSQL 18 + локаль ru_RU.UTF-8
│   ├── postgresql.conf              # конфиг СУБД (scram-sha-256 и т.п.)
│   └── init.sql                     # создаёт app_user с USAGE на public
├── config/
│   └── app.conf                     # статические настройки подключения (TOML)
├── src/
│   └── pinger.py                    # основной сервис (Python 3.11+, psycopg 3)
├── pinger.env                       # пример значений переменных среды (1.5–1.7)
├── Dockerfile                       # образ пингера (тег 0.1.0)
├── requirements.txt
└── README.md
```

## Запуск без Docker

```bash
pip install -r requirements.txt
PINGER_INTERVAL=300 PGUSER=app_user PGPASSWORD=user_secret \
  python src/pinger.py --config config/app.conf
```
