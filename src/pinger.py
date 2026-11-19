#!/usr/bin/env python3
"""PostgreSQL pinger with credentials retrieved from HashiCorp Vault (AppRole)."""
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

import hvac
import psycopg
from hvac.exceptions import VaultError

VERSION_PATTERN = re.compile(r"^\s*PostgreSQL\s+\d+\.\d+", re.IGNORECASE)

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
                if isinstance(val, bool) or not isinstance(val, int) or val not in guard:
                    raise ValueError(f"config key {key!r} has invalid value")
            elif isinstance(guard, re.Pattern):
                if not isinstance(val, str) or not guard.fullmatch(val):
                    raise ValueError(f"config key {key!r} has invalid value")
            elif isinstance(guard, set):
                if not isinstance(val, str) or val not in guard:
                    raise ValueError(f"config key {key!r} has invalid value")
            values[key] = val
        return cls(**values)


class Logger:
    def __init__(self, log_file: str | None = None):
        self.log_file = Path(log_file).expanduser() if log_file else None

    def _write(self, channel, msg: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, file=channel, flush=True)
        if self.log_file:
            try:
                with self.log_file.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    def ok(self, msg: str) -> None:
        self._write(sys.stdout, f"[OK] {msg}")

    def err(self, msg: str) -> None:
        self._write(sys.stderr, f"[ERR] {msg}")


def load_config(path: str | Path) -> DbConfig:
    import tomllib

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    with p.open("rb") as fh:
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
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def get_db_credentials_from_vault() -> tuple[str, str]:
    """Authenticate with AppRole and read the DB secret on every DB check."""
    vault_addr = os.environ["VAULT_ADDR"]
    role_id = os.environ["VAULT_ROLE_ID"]
    secret_id = os.environ["VAULT_SECRET_ID"]
    secret_path = os.environ.get("VAULT_SECRET_PATH", "postgres")
    mount_point = os.environ.get("VAULT_KV_MOUNT", "secret")

    client = hvac.Client(url=vault_addr, timeout=5)
    login = client.auth.approle.login(role_id=role_id, secret_id=secret_id)
    client.token = login["auth"]["client_token"]

    response = client.secrets.kv.v2.read_secret_version(
        path=secret_path,
        mount_point=mount_point,
    )
    secret = response["data"]["data"]
    username = secret.get("username")
    password = secret.get("password")
    if not isinstance(username, str) or not username:
        raise ValueError("Vault secret does not contain a valid 'username'")
    if not isinstance(password, str) or not password:
        raise ValueError("Vault secret does not contain a valid 'password'")
    return username, password


def check_database(logger: Logger, cfg: DbConfig, connect_timeout: int) -> bool:
    try:
        # Секрет перечитывается из Vault перед каждым запросом к PostgreSQL.
        user, password = get_db_credentials_from_vault()
    except (KeyError, ValueError, VaultError, OSError) as exc:
        logger.err(f"Vault secret retrieval/authentication error: {exc}")
        return False

    try:
        with psycopg.connect(
            host=cfg.host,
            port=cfg.port,
            dbname=cfg.dbname,
            sslmode=cfg.sslmode,
            user=user,
            password=password,
            connect_timeout=connect_timeout,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION();")
                row = cur.fetchone()
    except psycopg.Error as exc:
        logger.err(f"DB connection/query error: {exc}")
        return False

    version = row[0] if row else ""
    if not version:
        logger.ok("connected, but SELECT VERSION() returned an empty result")
    elif not VERSION_PATTERN.match(version):
        logger.ok(f"connected: unusual version response -> {version!r}")
    else:
        logger.ok(f"DB is up. Version: {version}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="PostgreSQL pinger with HashiCorp Vault")
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(__file__), "..", "config", "app.conf"),
        help="Path to TOML database config",
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
        host = os.environ.get("DB_HOST")
        port = os.environ.get("DB_PORT")
        dbname = os.environ.get("DB_NAME")
        if host or port or dbname:
            cfg = DbConfig(
                host=host or cfg.host,
                port=int(port) if port else cfg.port,
                dbname=dbname or cfg.dbname,
                sslmode=cfg.sslmode,
            )
        # Проверяем наличие параметров AppRole до запуска основного цикла.
        for name in ("VAULT_ADDR", "VAULT_ROLE_ID", "VAULT_SECRET_ID"):
            if not os.environ.get(name):
                raise ValueError(f"{name} environment variable is required")
        interval = env_int("PINGER_INTERVAL", 300, 1)
        connect_timeout = env_int("PINGER_CONNECT_TIMEOUT", 10, 1)
        log_file = os.environ.get("PINGER_LOG_FILE")
    except (ValueError, OSError, KeyError) as exc:
        sys.exit(f"ERROR: {exc}")

    logger = Logger(log_file)
    logger.ok(
        f"pinger started: host={cfg.host} port={cfg.port} dbname={cfg.dbname} "
        f"interval={interval}s connect_timeout={connect_timeout}s"
    )

    stopping = {"flag": False}

    def _stop(_signum, _frame):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    first = True
    while not stopping["flag"]:
        if not first:
            logger.ok(f"sleeping {interval}s until next check...")
            slept = 0
            while slept < interval and not stopping["flag"]:
                time.sleep(1)
                slept += 1
        first = False
        if not stopping["flag"]:
            check_database(logger, cfg, connect_timeout)

    logger.ok("pinger stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
