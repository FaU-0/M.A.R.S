# -*- coding: utf-8 -*-
"""Доступ врача к данным пациента: только через свой приём, администратор видит всех.

Врач А ведёт пациента, врач Б нет. Б не находит пациента в поиске, не видит его снимки и измерения,
не может записать его к себе на приём, чтобы получить доступ; чужой пациент неотличим от несуществующего.

    python tests/test_access.py
"""
import http.cookiejar
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
PORT = 18970
FAILS = []
Q = urllib.parse.quote("Секретов")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"


def check(c, m):
    print(("OK    " if c else "FAIL  ") + m)
    if not c:
        FAILS.append(m)


class Client:
    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, tok=None, raw=None, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), method=method, data=data, headers=h)
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        try:
            with self.op.open(req, timeout=10) as r:
                raw = r.read()
                try:
                    return r.status, json.loads(raw.decode() or "null")
                except ValueError:  # снимок: JPEG, а не JSON
                    return r.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw.decode() or "null")
            except ValueError:
                return e.code, None

    def login(self, login, pw):
        return self.call("POST", "/auth/login", {"login": login, "password": pw})[0]


def main():
    tmp = tempfile.mkdtemp(prefix="mars_acc_")
    env = dict(os.environ, DATA_DIR=tmp, PORT=str(PORT), ADMIN_PASSWORD="test-pass-1234")
    env.pop("MARS_SERVICES", None)
    p = subprocess.Popen([sys.executable, str(ROOT / "gateway" / "mars.py")], cwd=str(ROOT), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        admin = Client()
        for _ in range(40):
            try:
                admin.call("GET", "/health")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        admin.login("admin", "test-pass-1234")
        for login, name in (("doc_a", "Врач А"), ("doc_b", "Врач Б")):
            s, _ = admin.call("POST", "/doctors", {"login": login, "name": name, "password": "password-" + login})
            check(s == 201, "администратор создал врача %s" % login)
        a, b = Client(), Client()
        check(a.login("doc_a", "password-doc_a") == 200 and b.login("doc_b", "password-doc_b") == 200, "оба врача вошли")

        s, ap = a.call("POST", "/appointments", {"full_name": "Секретов Пётр Иванович"})
        pid = ap["patient_id"]
        s, rm = a.call("POST", "/rooms", {"appointment_id": ap["id"]})
        s, ph = a.call("POST", "/rooms/%s/frames" % rm["room"], tok=rm["token"], raw=JPEG,
                       headers={"Content-Type": "image/jpeg", "X-Pose": "frontal"})
        check(s == 200, "врач А сохранил снимок своего пациента")
        photo = ph["id"]

        s, r = a.call("GET", "/patients?q=" + Q)
        check(s == 200 and [x["id"] for x in r["patients"]] == [pid], "А находит своего пациента в поиске")
        check(a.call("GET", "/patients/%d/photos" % pid)[0] == 200 and a.call("GET", "/photos/%d" % photo)[0] == 200,
              "А видит снимки своего пациента")

        s, r = b.call("GET", "/patients?q=" + Q)
        check(s == 200 and r["patients"] == [], "Б не находит чужого пациента в поиске")
        s1, r1 = b.call("GET", "/patients/%d/photos" % pid)
        s2, _ = b.call("GET", "/patients/%d/measurements" % pid)
        s3, _ = b.call("GET", "/photos/%d" % photo)
        check(s1 == s2 == s3 == 404, "Б не видит снимки, измерения и файл снимка чужого пациента (404)")
        s4, r4 = b.call("GET", "/patients/999999/photos")
        check(s4 == 404 and r1 == r4, "чужой пациент неотличим от несуществующего")
        s, r = b.call("POST", "/appointments", {"patient_id": pid})
        check(s == 404, "Б не может записать чужого пациента к себе, чтобы получить доступ к истории")
        s, lst = b.call("GET", "/appointments")
        check(all(x.get("patient_id") != pid for x in lst["appointments"]), "приёма с чужим пациентом у Б не появилось")

        s, r = admin.call("GET", "/patients?q=" + Q)
        check(s == 200 and len(r["patients"]) == 1 and admin.call("GET", "/patients/%d/photos" % pid)[0] == 200,
              "администратор видит всех пациентов")
        s, ap2 = admin.call("POST", "/appointments", {"patient_id": pid})
        check(s == 201, "администратор может записать пациента на приём")

        db = sqlite3.connect(str(Path(tmp) / "mars.sqlite3"))
        n = db.execute("SELECT COUNT(*) FROM audit WHERE action='patient_access_denied' AND actor='doctor:doc_b'").fetchone()[0]
        check(n >= 4, "попытки Б записаны в журнал аудита (%d)" % n)

        # снимок в приёме без пациента (комната открыта без записи, карточки ещё нет)
        s, rm2 = a.call("POST", "/rooms", {})
        s, ph2 = a.call("POST", "/rooms/%s/frames" % rm2["room"], tok=rm2["token"], raw=JPEG,
                        headers={"Content-Type": "image/jpeg", "X-Pose": "frontal"})
        check(s == 200 and b.call("GET", "/photos/%d" % ph2["id"])[0] == 404,
              "снимок приёма без пациента: чужой врач не видит его по номеру (404)")
        check(a.call("GET", "/photos/%d" % ph2["id"])[0] == 200, "снимок приёма без пациента: врач приёма видит")
        s, _ = a.call("POST", "/rooms/%s/protocol" % rm2["room"], {"card": {"full_name": "Позднев Карточкин"}}, tok=rm2["token"])
        pid2 = db.execute("SELECT patient_id FROM photos WHERE id=?", (ph2["id"],)).fetchone()[0]
        s, r = a.call("GET", "/patients/%d/photos" % pid2) if pid2 else (0, {})
        check(pid2 and any(x["id"] == ph2["id"] for x in r.get("photos", [])),
              "после создания карточки снимок привязан к пациенту и виден в его истории")

        # неназначенный приём (появляется в режиме без входа или если врача удалили)
        db.execute("INSERT INTO patients(full_name, created) VALUES('Ничейный Пациент', 1)")
        npid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO appointments(code, patient_id, doctor_id, scheduled, status, created) VALUES('NULLDR', ?, NULL, ?, 'planned', 1)",
                   (npid, int(time.time())))
        db.commit()
        naid = db.execute("SELECT id FROM appointments WHERE code='NULLDR'").fetchone()[0]
        s, lst = b.call("GET", "/appointments")
        check(all(x["id"] != naid for x in lst["appointments"]), "неназначенный приём не виден обычному врачу в списке")
        s, r = b.call("POST", "/rooms", {"appointment_id": naid})
        check(s == 403, "обычный врач не может присвоить неназначенный приём и получить историю пациента (403)")
        check(b.call("GET", "/patients/%d/photos" % npid)[0] == 404, "история ничейного пациента врачу Б недоступна")
        s, r = admin.call("POST", "/rooms", {"appointment_id": naid})
        check(s == 201, "администратор открывает неназначенный приём")

        # права на данные пациентов
        mode = lambda x: oct(os.stat(x).st_mode & 0o777)
        photo_file = next((Path(tmp) / "photos").iterdir())
        check(mode(tmp) == "0o700" and mode(Path(tmp) / "mars.sqlite3") == "0o600" and mode(photo_file) == "0o600",
              "папка данных 700, база и снимки 600 (%s, %s, %s)" % (mode(tmp), mode(Path(tmp) / "mars.sqlite3"), mode(photo_file)))
    finally:
        p.terminate()
        p.wait(10)
    print("\nИтог: %s" % ("всё прошло" if not FAILS else "ошибок: %d" % len(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
