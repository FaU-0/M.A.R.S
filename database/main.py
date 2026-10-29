# -*- coding: utf-8 -*-
"""Сервис database: SQLite M.A.R.S., миграции, все запросы, очередь заданий, резервные копии.

Gateway обращается сюда по HTTP и SQL не содержит: POST /v1/q {name, args} выполняет запрос из queries.py.
Резервная копия: при запуске, если последней больше BACKUP_HOURS часов, потом каждые BACKUP_HOURS часов,
и по запросу POST /v1/backup. Хранятся BACKUP_KEEP последних копий в DATA_DIR/backups.
"""
import logging
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.store import QueryError, Store, encode  # noqa: E402
from shared import config  # noqa: E402
from shared.errors import error_body  # noqa: E402
from shared.service_base import Service  # noqa: E402

SVC = "database"
log = logging.getLogger("mars.database")
STORE = None


def backup_dir():
    return Path(os.environ.get("BACKUP_DIR") or (config.data_dir() / "backups"))


def keep():
    return int(os.environ.get("BACKUP_KEEP", "14"))


def hours():
    return float(os.environ.get("BACKUP_HOURS", "24"))


def r_query(data, rid):
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return 400, error_body("bad_request", "Нужно имя запроса", SVC)
    try:
        res = STORE.call(data["name"], data.get("args") or [])
    except QueryError as e:
        log.warning("[%s] %s", rid, e)
        return 400, error_body("bad_query", str(e), SVC)
    return 200, {"result": encode(res)}


def r_backup(data, rid):
    dest = STORE.backup(backup_dir(), keep())
    log.info("[%s] резервная копия %s", rid, dest)
    return 200, {"file": str(dest), "size": dest.stat().st_size}


def backup_loop():
    """Копия при запуске, если последней больше BACKUP_HOURS часов, затем раз в BACKUP_HOURS часов."""
    period = hours() * 3600
    if period <= 0:
        return
    while True:
        files = sorted(backup_dir().glob("mars-*.sqlite3"))
        age = time.time() - files[-1].stat().st_mtime if files else None
        if age is None or age >= period:
            try:
                log.info("резервная копия %s", STORE.backup(backup_dir(), keep()))
            except Exception:  # noqa: BLE001
                log.exception("резервная копия не создана")
            age = 0
        time.sleep(max(60.0, period - age))


def ready():
    return STORE is not None, {"schema_version": STORE.version if STORE else None}


if __name__ == "__main__":
    STORE = Store(config.secure_data_dir() / "mars.sqlite3")
    threading.Thread(target=backup_loop, daemon=True).start()
    Service(SVC, routes={("POST", "/v1/q"): r_query, ("POST", "/v1/backup"): r_backup}, ready_fn=ready).run()
