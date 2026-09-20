#!/usr/bin/env python3
"""
Сервис «pinger/heartbeat» для PostgreSQL 18 (Задание №2).

Периодически (по умолчанию раз в 5 минут) отправляет запрос версии БД
`SELECT VERSION();` и пишет результат успеха в stdout, ошибки — в stderr.
Не зависает при сетевых проблемах (connect_timeout), ошибка подключения не
отменяет следующую проверку — цикл продолжается.

Параметры (переменные среды окружения):
  PINGER_INTERVAL       интервал между опросами, секунды            (по умолч. 300)
  PINGER_CONNECT_TIMEOUT  таймаут подключения, секунды              (по умолч. 10)
  PGUSER                логин БД                                    (обязательный)
  PGPASSWORD            пароль БД                                    (обязательный)
  PINGER_LOG_FILE       необязательный путь к файлу для дублирования логов
  DB_HOST / DB_PORT / DB_NAME  переопределение настроек конфига (необязательно)

Статические настройки подключения (host/port/dbname/sslmode) читаются из
конфиг-файла (TOML), лежащего рядом (по умолчанию config/app.conf), с теми же
правилами белого списка и проверки типов, что и в задании №1.
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

VERSION_PATTERN = re.compile(r"^\s*PostgreSQL\s+\d+\.\d+", re.IGNORECASE)

# Белый список ключей конфига + валидаторы (как в задании №1).
ALLOWED_KEYS = {
    "host": (str, re.compile(r"^[A-Za-z0-9._\-\[\]:]+$")),
    "port": (int, range(1, 65536)),
    "dbname": (str, re.compile(r"^[A-Za-z0-9_\-]+$")),
    "sslmode": (str, {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}),
}


@dataclass(frozen=True)
class DbConfig:
    host: str = "localhost"
    port: int = 5432
    dbname: str = "appdb"
    sslmode: str = "prefer"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DbConfig":
        values: dict[str, Any] = {}
        for key, val in raw.items():
            if key not in ALLOWED_KEYS:
                raise ValueError(f"unknown config key: {key!r}")
            expected, guard = ALLOWED_KEYS[key]
            if expected is int:
                if isinstance(val, bool) or not isinstance(val, int):
                    raise ValueError(f"config key {key!r} must be an integer")
                if val not in guard:
                    raise ValueError(f"config key {key!r} out of range: {val!r}")
            elif isinstance(guard, re.Pattern):
                if not isinstance(val, str) or not guard.fullmatch(val):
                    raise ValueError(f"config key {key!r} has invalid value")
            elif isinstance(guard, set):
                if not isinstance(val, str) or val not in guard:
                    raise ValueError(f"config key {key!r} has invalid value {val!r}")
            values[key] = val
        return cls(**values)

    def to_connect_kwargs(self, user: str, password: str, connect_timeout: int) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "sslmode": self.sslmode,
            "user": user,
            "password": password,
            "connect_timeout": connect_timeout,
        }


# --------------------------------------------------------------------------- #
# Логирование: stdout / stderr + (опционально) дублирование в файл
# --------------------------------------------------------------------------- #

class Logger:
    def __init__(self, log_file: str | None = None):
        self.log_file = Path(log_file).expanduser() if log_file else None

    def _write(self, channel, msg: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, file=channel, flush=True)
        if self.log_file is not None:
            try:
                with open(self.log_file, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    def ok(self, msg: str) -> None:
        """Факт успешного подключения/ответа -> stdout."""
        self._write(sys.stdout, f"[OK] {msg}")

    def err(self, msg: str) -> None:
        """Ошибка подключения -> stderr."""
        self._write(sys.stderr, f"[ERR] {msg}")


# --------------------------------------------------------------------------- #
# Конфиг
# --------------------------------------------------------------------------- #

def load_config(path: str | Path) -> DbConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    import tomllib
    with open(p, "rb") as fh:
        data = tomllib.load(fh)
    section = data.get("database")
    if not isinstance(section, dict):
        raise ValueError(f"no [database] section in {p}")
    return DbConfig.from_dict(section)


def env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}")
    if val < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {val}")
    return val


# --------------------------------------------------------------------------- #
# Проверка БД
# --------------------------------------------------------------------------- #

def check_database(logger: Logger, cfg: DbConfig, user: str, password: str,
                   connect_timeout: int) -> bool:
    """
    Один цикл проверки. Возвращает True, если подключение к БД успешно.
    Любая ошибка пишется в stderr и возвращается False (цикл продолжается).
    """
    kwargs = cfg.to_connect_kwargs(user, password, connect_timeout)
    try:
        with psycopg.connect(**kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION();")
                row = cur.fetchone()
    except psycopg.Error as exc:
        logger.err(f"DB connection/query error: {exc}")
        return False

    version = row[0] if row else ""
    if not version:
        # «не типичный ответ» без ошибки (пустая строка) -> stdout.
        logger.ok("connected, but SELECT VERSION() returned an empty result")
        return True
    if not VERSION_PATTERN.match(version):
        logger.ok(f"connected: unusual (non-typical) version response -> {version!r}")
        return True
    logger.ok(f"DB is up. Version: {version}")
    return True


# --------------------------------------------------------------------------- #
# Основной цикл
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="PostgreSQL pinger/heartbeat (Task #2)")
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(__file__), "..", "config", "app.conf"),
        help="Путь к конфиг-файлу подключения (TOML).",
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
        # Возможные переопределения через env (для compose: host="db").
        host = os.environ.get("DB_HOST")
        port = os.environ.get("DB_PORT")
        dbname = os.environ.get("DB_NAME")
        if host:
            cfg = DbConfig(host=host, port=int(port) if port else cfg.port,
                           dbname=dbname or cfg.dbname, sslmode=cfg.sslmode)
        elif port or dbname:
            cfg = DbConfig(host=cfg.host, port=int(port) if port else cfg.port,
                           dbname=dbname or cfg.dbname, sslmode=cfg.sslmode)

        user = os.environ.get("PGUSER")
        password = os.environ.get("PGPASSWORD")
        interval = env_int("PINGER_INTERVAL", 300, 1)
        connect_timeout = env_int("PINGER_CONNECT_TIMEOUT", 10, 1)
        log_file = os.environ.get("PINGER_LOG_FILE")

        if not user or not password:
            raise ValueError("PGUSER and PGPASSWORD environment variables are required")
    except (ValueError, OSError) as exc:
        sys.exit(f"ERROR: {exc}")

    logger = Logger(log_file)
    logger.ok(
        f"pinger started: host={cfg.host} port={cfg.port} dbname={cfg.dbname} "
        f"interval={interval}s connect_timeout={connect_timeout}s "
        f"log_file={log_file or '(none)'}"
    )

    # Останавливаемся по SIGTERM/SIGINT корректно.
    stopping = {"flag": False}

    def _stop(_signum, _frame):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    first = True
    while not stopping["flag"]:
        if not first:
            logger.ok(f"sleeping {interval}s until next check...")
            # Дробим ожидание, чтобы реагировать на сигнал остановки.
            slept = 0
            while slept < interval and not stopping["flag"]:
                time.sleep(1)
                slept += 1
        first = False

        if stopping["flag"]:
            break
        check_database(logger, cfg, user, password, connect_timeout)

    logger.ok("pinger stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
