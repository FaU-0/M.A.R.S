# -*- coding: utf-8 -*-
"""Аудит, ссылки врача, отчёт без расшифровки, лимиты загрузок и устаревшие задания (одиночный режим gateway).

1. Ссылка врача в комнату работает только с его сессией: после выхода - 401.
2. В журнале аудита логин врача (doctor:<логин>), а не просто «doctor»; просмотры карточки, истории, файлов, поиск,
   печать заключения записываются; текст поиска (фамилия) в журнал не попадает.
3. Сохранённый отчёт по лицу не хранит текст расшифровки; у измерения записано, кто прислал и проверено ли сервером.
4. Лимиты приёма (снимки, измерения) и запрет загрузок, когда на диске мало места.
5. Задание, не выполненное за сутки, не исчезает молча: помечается ошибкой, аудио удаляется,
   невыполненное обезличивание переводит расшифровку в «ошибку».

    python tests/test_audit_quota.py
"""
import http.cookiejar
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PORT = 18990
FAILS = []
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"


def check(c, m):
    print(("OK    " if c else "FAIL  ") + m)
    if not c:
        FAILS.append(m)


class Client:
    def __init__(self, port):
        self.port = port
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, tok=None, raw=None, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), method=method, data=data, headers=h)
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        try:
            with self.op.open(req, timeout=10) as r:
                raw = r.read()
                try:
                    return r.status, json.loads(raw.decode() or "null")
                except ValueError:
                    return r.status, raw
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "null")


def start(tmp, port, extra=None):
    env = dict(os.environ, DATA_DIR=tmp, PORT=str(port), ADMIN_PASSWORD="test-pass-1234", **(extra or {}))
    env.pop("MARS_SERVICES", None)
    p = subprocess.Popen([sys.executable, str(ROOT / "gateway" / "mars.py")], cwd=str(ROOT), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    c = Client(port)
    for _ in range(40):
        try:
            c.call("GET", "/health")
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return p, c


def stop(p):
    p.terminate()
    p.wait(10)


def part_main(tmp):
    p, admin = start(tmp, PORT, {"APPT_PHOTOS": "2", "APPT_MEASUREMENTS": "3"})
    try:
        admin.call("POST", "/auth/login", {"login": "admin", "password": "test-pass-1234"})
        admin.call("POST", "/doctors", {"login": "doc_a", "name": "Врач А", "password": "password-doc_a"})
        a = Client(PORT)
        a.call("POST", "/auth/login", {"login": "doc_a", "password": "password-doc_a"})
        s, ap = a.call("POST", "/appointments", {"full_name": "Аудитов Пётр"})
        s, rm = a.call("POST", "/rooms", {"appointment_id": ap["id"]})
        code, tok = rm["room"], rm["token"]
        s, pj = a.call("POST", "/rooms/%s/join" % code, {"consent": True})
        pat = pj["token"]
        db = sqlite3.connect(str(Path(tmp) / "mars.sqlite3"))

        print("--- аудит: кто именно ---")
        a.call("GET", "/rooms/%s/protocol" % code, tok=tok)
        a.call("GET", "/patients?q=" + urllib.parse.quote("Аудитов"))
        a.call("GET", "/patients/%d/measurements" % ap["patient_id"])
        a.call("POST", "/rooms/%s/render-conclusion" % code, {"conclusion": {"diagnosis": "x"}}, tok=tok)
        rows = db.execute("SELECT actor, action, detail FROM audit WHERE actor LIKE 'doctor%'").fetchall()
        acts = {r[1] for r in rows if r[0] == "doctor:doc_a"}
        check({"protocol_viewed", "patient_search", "patient_measurements_viewed", "conclusion_printed"} <= acts,
              "просмотры карточки, поиск, история и печать записаны с логином врача")
        check(not any(r[0] == "doctor" for r in rows), "в журнале нет безымянного «doctor»")
        check(all("Аудитов" not in (r[2] or "") for r in rows), "текст поиска (фамилия) в журнал не попал")

        print("--- отчёт и измерения ---")
        rep = {"kind": "report", "patient": "Аудитов Пётр", "transcript": {"segments": [{"role": "patient", "t": 0, "text": "Меня зовут [ФИО_ПАЦИЕНТА]"}]}}
        s, _ = a.call("POST", "/rooms/%s/measurements" % code, rep, tok=tok)
        body = json.loads(db.execute("SELECT body FROM measurements WHERE kind='report'").fetchone()[0])
        check(s == 200 and body["transcript"] == {"included": True, "segments": 1} and "Меня зовут" not in json.dumps(body, ensure_ascii=False),
              "сохранённый отчёт хранит отметку о расшифровке, но не её текст")
        sym = {"kind": "symmetry", "pose_ok": True, "rows": [], "samples": 20}
        s, _ = a.call("POST", "/rooms/%s/measurements" % code, sym, tok=pat)
        body = json.loads(db.execute("SELECT body FROM measurements WHERE kind='symmetry'").fetchone()[0])
        check(body.get("submitted_by") == "patient" and body.get("verified") is False,
              "у измерения записано, что его прислал пациент и что сервер его не проверял")

        print("--- лимиты приёма ---")
        s1, _ = a.call("POST", "/rooms/%s/measurements" % code, sym, tok=pat)
        s2, r = a.call("POST", "/rooms/%s/measurements" % code, sym, tok=pat)
        check(s1 == 200 and s2 == 413 and r.get("error") == "appointment_quota", "лимит измерений на приём (3) соблюдается")
        st = [a.call("POST", "/rooms/%s/frames" % code, tok=pat, raw=JPEG, headers={"Content-Type": "image/jpeg", "X-Pose": "frontal"})[0]
              for _ in range(3)]
        check(st == [200, 200, 413], "лимит снимков на приём (2) соблюдается (%s)" % st)

        print("--- ссылка врача и сессия ---")
        check(a.call("GET", "/rooms/%s/events?after=0" % code, tok=tok)[0] == 200, "ссылка врача работает с его сессией")
        other = Client(PORT)
        check(other.call("GET", "/rooms/%s/events?after=0" % code, tok=tok)[0] == 401, "та же ссылка без сессии врача не работает")
        a.call("POST", "/auth/logout", {})
        check(a.call("GET", "/rooms/%s/protocol" % code, tok=tok)[0] == 401, "после выхода врача его ссылка в комнату перестаёт работать")
        check(a.call("GET", "/rooms/%s/events?after=0" % code, tok=pat)[0] == 200, "ссылка пациента продолжает работать")
    finally:
        stop(p)


def part_disk(tmp):
    print("--- мало места на диске ---")
    p, admin = start(tmp, PORT + 1, {"MIN_FREE_MB": str(10 ** 9)})
    try:
        admin.call("POST", "/auth/login", {"login": "admin", "password": "test-pass-1234"})
        s, ap = admin.call("POST", "/appointments", {"full_name": "Дисков"})
        s, rm = admin.call("POST", "/rooms", {"appointment_id": ap["id"]})
        s, r = admin.call("POST", "/rooms/%s/frames" % rm["room"], tok=rm["token"], raw=JPEG,
                          headers={"Content-Type": "image/jpeg", "X-Pose": "frontal"})
        check(s == 507 and r.get("error") == "disk_full", "при нехватке места снимок не принимается (507)")
    finally:
        stop(p)


def part_expiry(tmp):
    print("--- задания старше суток ---")
    os.environ["DATA_DIR"] = tmp
    os.environ.pop("MARS_SERVICES", None)
    spec = importlib.util.spec_from_file_location("mars_gateway", str(ROOT / "gateway" / "mars.py"))
    gw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gw)
    gw.DB = gw.LocalDB(Path(tmp) / "mars.sqlite3")
    old = int(time.time()) - 2 * 86400
    audio = Path(tmp) / "audio"
    audio.mkdir(exist_ok=True)
    (audio / "old_chunk.webm").write_bytes(b"x" * 200)
    db = sqlite3.connect(str(Path(tmp) / "mars.sqlite3"))
    db.execute("INSERT INTO jobs(appt_id,kind,key,created,updated,input) VALUES(77,'transcribe','old-transcribe',?,?,?)",
               (old, old, json.dumps({"audio": "audio/old_chunk.webm"})))
    db.execute("INSERT INTO jobs(appt_id,kind,key,created,updated,input) VALUES(77,'deidentify','old-deid',?,?,'{}')", (old, old))
    db.execute("INSERT INTO transcripts(appt_id,status,created,updated) VALUES(77,'queued',?,?)", (old, old))
    db.commit()
    gw.jobs_cleanup()
    rows = dict(db.execute("SELECT kind, status FROM jobs WHERE appt_id=77").fetchall())
    check(rows == {"transcribe": "error", "deidentify": "error"}, "невыполненные задания помечены ошибкой, а не удалены (%s)" % rows)
    check(not (audio / "old_chunk.webm").exists(), "аудио просроченного куска удалено")
    st = db.execute("SELECT status, error FROM transcripts WHERE appt_id=77").fetchone()
    check(st[0] == "error" and "expired" in st[1], "расшифровка, так и не обезличенная, переведена в «ошибку»")
    inp = db.execute("SELECT input FROM jobs WHERE key='old-transcribe'").fetchone()[0]
    check(inp == "{}", "входные данные просроченного задания стёрты")


def main():
    tmp = tempfile.mkdtemp(prefix="mars_aq_")
    part_main(tmp)
    part_disk(tempfile.mkdtemp(prefix="mars_aq2_"))
    part_expiry(tmp)
    print("\nИтог: %s" % ("всё прошло" if not FAILS else "ошибок: %d" % len(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
