# -*- coding: utf-8 -*-
"""Хранилище SQLite: миграции, именованные запросы, резервные копии.

Используется сервисом database (main.py), а в одиночном режиме (python gateway/mars.py) - прямо внутри gateway.
Значения передаются в JSON, поэтому байты (соль и хэш пароля) кодируются как {"$b": base64}.
"""
import base64
import sqlite3
import threading
import time
from pathlib import Path

from .queries import DYNAMIC, QUERIES
from .schema import MIGRATIONS


class QueryError(ValueError):
    """Неизвестный запрос или неверные параметры: ошибка вызывающего, а не базы."""


def encode(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$b": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, dict):
        return {k: encode(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [encode(x) for x in v]
    return v


def decode(v):
    if isinstance(v, dict):
        if set(v) == {"$b"}:
            return base64.b64decode(v["$b"])
        return {k: decode(x) for k, x in v.items()}
    if isinstance(v, list):
        return [decode(x) for x in v]
    return v


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self.version = self.migrate()

    def migrate(self):
        """Применяет недостающие шаги MIGRATIONS. Возвращает номер версии схемы."""
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL, applied INTEGER NOT NULL)")
        row = self.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        cur = row[0] or 0
        for n, script in enumerate(MIGRATIONS, 1):
            if n <= cur:
                continue
            self.conn.execute("BEGIN")
            try:
                for stmt in [x.strip() for x in script.split(";") if x.strip()]:
                    self.conn.execute(stmt)
                self.conn.execute("INSERT INTO schema_version(version, applied) VALUES(?,?)", (n, int(time.time())))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            cur = n
        return cur

    def call(self, name, args=()):
        """Выполняет запрос по имени. list -> [строки], one -> строка или None, run -> lastrowid."""
        args = list(decode(list(args or [])))
        with self.lock:
            if name in QUERIES:
                mode, sql = QUERIES[name]
                try:
                    cur = self.conn.execute(sql, args)
                except sqlite3.ProgrammingError as e:
                    raise QueryError("%s: %s" % (name, e))
                if mode == "run":
                    return cur.lastrowid
                rows = [dict(r) for r in cur.fetchall()]
                return rows if mode == "list" else (rows[0] if rows else None)
            if name in DYNAMIC:
                try:
                    return DYNAMIC[name][1](self.conn, *args)
                except (TypeError, sqlite3.ProgrammingError) as e:
                    raise QueryError("%s: %s" % (name, e))
        raise QueryError("неизвестный запрос: %s" % name)

    def backup(self, folder, keep=14):
        """Копия базы средствами SQLite (согласованная, без остановки записи). Старые копии сверх keep удаляются."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / ("mars-%s.sqlite3" % time.strftime("%Y%m%d-%H%M%S"))
        tmp = dest.with_suffix(".part")
        out = sqlite3.connect(str(tmp))
        try:
            with self.lock:
                self.conn.backup(out)
        finally:
            out.close()
        tmp.replace(dest)
        old = sorted(folder.glob("mars-*.sqlite3"))[:-keep] if keep > 0 else []
        for f in old:
            try:
                f.unlink()
            except OSError:
                pass
        return dest

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
