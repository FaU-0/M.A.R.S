# -*- coding: utf-8 -*-
"""Вынос SQL в сервис database.

1. В gateway нет SQL: ни одного SELECT, INSERT, UPDATE, DELETE, CREATE.
2. Каждый запрос из queries.py выполняется на пустой базе (имена и параметры сходятся со схемой).
3. Миграции: база, созданная прежним gateway (до таблицы schema_version), обновляется без потери данных.
4. Резервная копия: создаётся по запросу с подписью, это рабочая база, старые копии удаляются.
5. Одиночный режим: python gateway/mars.py без run_all работает с той же базой внутри процесса.

    python tests/test_database.py
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from database import queries, schema  # noqa: E402
from database.store import QueryError, Store, decode, encode  # noqa: E402

FAILS = []


def check(c, m):
    print(("OK    " if c else "FAIL  ") + m)
    if not c:
        FAILS.append(m)


def part_no_sql():
    print("--- gateway без SQL ---")
    # серверная часть gateway разбита на gateway/src/a*.py и склеивается сборкой в mars.py
    src = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "gateway" / "src").glob("a*.py")))
    hits = [l.strip()[:80] for l in src.splitlines()
            if re.search(r"\b(SELECT|INSERT INTO|UPDATE \w+ SET|DELETE FROM|CREATE TABLE|CREATE INDEX)\b", l)]
    check(not hits, "в gateway/src/a*.py нет SQL" + ("" if not hits else ": " + "; ".join(hits[:3])))
    built = (ROOT / "gateway" / "mars.py").read_text(encoding="utf-8")
    check("import sqlite3" not in built, "gateway/mars.py не импортирует sqlite3")


def part_queries(tmp):
    print("--- все запросы на пустой базе ---")
    st = Store(Path(tmp) / "q.sqlite3")
    bad = []
    for name, (mode, sql) in queries.QUERIES.items():
        n = sql.count("?")
        try:
            r = st.call(name, [1] * n)
            if mode == "list" and not isinstance(r, list):
                bad.append(name + ": ждали список")
        except Exception as e:  # noqa: BLE001
            bad.append("%s: %s" % (name, e))
    check(not bad, "%d именованных запросов выполняются%s" % (len(queries.QUERIES), "" if not bad else ": " + "; ".join(bad[:3])))
    pid = st.call("patients.add", ["Иванов Иван", 1])
    st.call("patients.update_card", [pid, {"allergies": "пенициллин", "evil; DROP TABLE patients": "x"}])
    check(st.call("patients.by_id", [pid])["allergies"] == "пенициллин", "patients.update_card меняет только поля карточки из белого списка")
    check(len(st.call("appointments.between", [0, 10 ** 10, 5])) == 0 and isinstance(st.call("jobs.due", [10 ** 10, ["transcribe"]]), list),
          "динамические запросы appointments.between и jobs.due работают")
    try:
        st.call("DROP TABLE patients", [])
        check(False, "произвольный SQL не выполняется")
    except QueryError:
        check(True, "произвольный SQL вместо имени отклоняется")
    salt = os.urandom(16)
    did = st.call("doctors.add", decode(encode(["doc1", "Врач", salt, b"\x00\xffhash", 0, 1])))
    row = decode(encode(st.call("doctors.by_login", ["doc1"])))
    check(row["id"] == did and row["salt"] == salt, "байты (соль и хэш пароля) проходят через JSON без искажений")
    st.close()


def part_migrations(tmp):
    print("--- миграции ---")
    path = Path(tmp) / "old.sqlite3"
    old = sqlite3.connect(str(path))
    old.executescript(schema.V1)  # так базу создавал прежний gateway: та же схема, но без schema_version
    old.execute("INSERT INTO patients(full_name, created) VALUES('Старый пациент', 1)")
    old.commit()
    old.close()
    st = Store(path)
    check(st.version == len(schema.MIGRATIONS), "старая база обновлена до версии %d" % st.version)
    check(st.call("patients.search", ["%Старый%"])[0]["full_name"] == "Старый пациент", "данные старой базы на месте")
    st.close()
    st = Store(path)
    n = sqlite3.connect(str(path)).execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    check(n == len(schema.MIGRATIONS), "повторный запуск не применяет миграции заново")
    st.close()


def signed(port, path, body):
    from shared import security
    raw = json.dumps(body).encode()
    hdr = {"Content-Type": "application/json"}
    hdr.update(security.sign(os.environ["SERVICE_SECRET"], "POST", path, raw))
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=raw, method="POST", headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def part_service(tmp):
    print("--- сервис database: резервные копии ---")
    port = 18950
    env = dict(os.environ, DATA_DIR=tmp, MARS_DATABASE_PORT=str(port), SERVICE_SECRET="test-secret-123", BACKUP_KEEP="2", BACKUP_HOURS="24")
    os.environ["SERVICE_SECRET"] = env["SERVICE_SECRET"]
    p = subprocess.Popen([sys.executable, "-m", "database.main"], cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ok = False
        for _ in range(40):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/ready" % port, timeout=1) as r:
                    ok = json.loads(r.read().decode())["schema_version"] == len(schema.MIGRATIONS)
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        check(ok, "database готов, /ready показывает версию схемы")
        bdir = Path(tmp) / "backups"
        check(len(list(bdir.glob("mars-*.sqlite3"))) == 1, "при первом запуске создана резервная копия")
        s, r = signed(port, "/v1/q", {"name": "patients.add", "args": ["Пациент для копии", 1]})
        check(s == 200, "запрос по имени через HTTP")
        s, r = signed(port, "/v1/q", {"name": "DELETE FROM patients", "args": []})
        check(s == 400 and r.get("error") == "bad_query", "произвольный SQL через HTTP отклоняется (400)")
        req = urllib.request.Request("http://127.0.0.1:%d/v1/q" % port, data=b'{"name":"patients.search","args":["%"]}', method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            check(False, "запрос без подписи отклонён")
        except urllib.error.HTTPError as e:
            check(e.code == 401, "запрос без подписи отклонён (401)")
        files = []
        for _ in range(3):
            time.sleep(1.1)  # имя копии содержит секунды
            s, r = signed(port, "/v1/backup", {})
            files.append(r.get("file"))
        left = sorted(bdir.glob("mars-*.sqlite3"))
        check(s == 200 and len(left) == 2, "резервная копия по запросу; хранится BACKUP_KEEP=2 последних (%d)" % len(left))
        c = sqlite3.connect(str(left[-1]))
        n = c.execute("SELECT COUNT(*) FROM patients WHERE full_name='Пациент для копии'").fetchone()[0]
        v = c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        check(n == 1 and v == len(schema.MIGRATIONS) and c.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
              "копия - целая база с данными и версией схемы")
    finally:
        p.terminate()
        p.wait(10)


def part_solo(tmp):
    print("--- одиночный режим: python gateway/mars.py ---")
    port = 18960
    env = dict(os.environ, DATA_DIR=tmp + "/solo", PORT=str(port), ADMIN_PASSWORD="test-pass-1234")
    env.pop("MARS_SERVICES", None)
    p = subprocess.Popen([sys.executable, str(ROOT / "gateway" / "mars.py")], cwd=str(ROOT), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import http.cookiejar
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def call(method, path, body=None):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method,
                                         data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
            with op.open(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        for _ in range(40):
            try:
                call("GET", "/health")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        s, _ = call("POST", "/auth/login", {"login": "admin", "password": "test-pass-1234"})
        check(s == 200, "вход администратора")
        s, ap = call("POST", "/appointments", {"full_name": "Одиночный режим"})
        s2, lst = call("GET", "/appointments")
        check(s == 201 and any(a["id"] == ap["id"] for a in lst["appointments"]), "приём создан и виден в списке")
        s, st = call("GET", "/status")
        check(st == {"status": "ok", "services": {}}, "без run_all вторичных сервисов нет, /status = ok")
        check((Path(tmp) / "solo" / "mars.sqlite3").is_file(), "база лежит в DATA_DIR, как раньше")
    finally:
        p.terminate()
        p.wait(10)


def main():
    tmp = tempfile.mkdtemp(prefix="mars_db_")
    part_no_sql()
    part_queries(tmp)
    part_migrations(tmp)
    part_service(tmp)
    part_solo(tmp)
    print("\nИтог: %s" % ("всё прошло" if not FAILS else "ошибок: %d" % len(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
