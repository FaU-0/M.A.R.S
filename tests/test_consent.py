# -*- coding: utf-8 -*-
"""Согласие на запись речи, завершение приёма и журнал assistant (без Whisper и Ollama, одиночный режим gateway).

1. Отзыв согласия удаляет куски речи, которые ещё не расшифрованы, вместе с аудиофайлами.
2. После завершения приёма запись речи не включается и куски не принимаются.
3. Экстренное завершение выключает запись так же, как обычное.
4. Журнал assistant не содержит ответа модели (в нём имена и места из расшифровки).

    python tests/test_consent.py
"""
import http.cookiejar
import io
import json
import logging
import os
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
PORT = 18980
FAILS = []
AUDIO = b"\x1a\x45\xdf\xa3" + b"\x00" * 400  # заголовок WebM; speech в одиночном режиме не запущен, куски ждут в очереди


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
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "null")


def part_gateway(tmp):
    env = dict(os.environ, DATA_DIR=tmp, PORT=str(PORT), ADMIN_PASSWORD="test-pass-1234")
    env.pop("MARS_SERVICES", None)
    p = subprocess.Popen([sys.executable, str(ROOT / "gateway" / "mars.py")], cwd=str(ROOT), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        c = Client()
        for _ in range(40):
            try:
                c.call("GET", "/health")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        c.call("POST", "/auth/login", {"login": "admin", "password": "test-pass-1234"})
        db = sqlite3.connect(str(Path(tmp) / "mars.sqlite3"))

        def room():
            s, ap = c.call("POST", "/appointments", {"full_name": "Пациент Согласий"})
            s, rm = c.call("POST", "/rooms", {"appointment_id": ap["id"]})
            s, pj = c.call("POST", "/rooms/%s/join" % rm["room"], {"consent": True})
            return ap["id"], rm["room"], rm["token"], pj["token"]

        def ev(code, tok, data):
            return c.call("POST", "/rooms/%s/events" % code, {"type": "control", "data": data}, tok=tok)

        def audio(code, tok, key):
            return c.call("POST", "/rooms/%s/audio" % code, tok=tok, raw=AUDIO,
                          headers={"Content-Type": "audio/webm", "X-Chunk-Key": key, "X-Seq": "0", "X-Duration-Ms": "10000"})

        audio_dir = Path(tmp) / "audio"
        print("--- отзыв согласия ---")
        aid, code, doc, pat = room()
        ev(code, pat, {"action": "speech_consent", "granted": True})
        s1, _ = audio(code, pat, "rev00001-0")
        s2, _ = audio(code, doc, "rev00002-0")
        check(s1 == s2 == 202 and len(list(audio_dir.iterdir())) == 2, "два куска речи приняты и ждут в очереди")
        s, _ = ev(code, pat, {"action": "speech_consent", "granted": False})
        rows = dict(db.execute("SELECT status, COUNT(*) FROM jobs WHERE appt_id=? AND kind='transcribe' GROUP BY status", (aid,)).fetchall())
        check(rows == {"cancelled": 2}, "после отзыва согласия куски отменены, а не расшифрованы позже (%s)" % rows)
        check(not list(audio_dir.iterdir()), "аудио отменённых кусков удалено")
        s, st = c.call("GET", "/rooms/%s/transcript/status" % code, tok=doc)
        check(st.get("cancelled") == 2 and st.get("queued") == 0, "врач видит, что куски отменены")
        s, _ = audio(code, pat, "rev00003-0")
        check(s == 403, "после отзыва новые куски не принимаются")
        audit = db.execute("SELECT detail FROM audit WHERE action='consent_speech' ORDER BY id DESC LIMIT 1").fetchone()[0]
        check("dropped=2" in audit, "в журнале аудита записано, сколько кусков удалено")

        print("--- после завершения приёма ---")
        aid, code, doc, pat = room()
        ev(code, pat, {"action": "speech_consent", "granted": True})
        ev(code, doc, {"action": "end"})
        s, r = audio(code, pat, "end00001-0")
        check(s in (403, 409), "кусок после завершения приёма не принимается (%d)" % s)
        s, r = ev(code, pat, {"action": "speech_consent", "granted": True})
        check(s == 409 and r.get("error") == "appointment_ended", "согласие после завершения приёма не включает запись (409)")
        s, r = ev(code, doc, {"action": "speech_request"})
        check(s == 409, "запрос записи после завершения приёма отклонён (409)")
        s, _ = ev(code, pat, {"action": "speech_consent", "granted": False})
        check(s in (200, 202), "отозвать согласие после завершения по-прежнему можно")

        print("--- экстренное завершение ---")
        aid, code, doc, pat = room()
        ev(code, pat, {"action": "speech_consent", "granted": True})
        s, _ = c.call("POST", "/rooms/%s/events" % code, {"type": "kill", "data": {}}, tok=doc)
        proto = json.loads(db.execute("SELECT protocol FROM appointments WHERE id=?", (aid,)).fetchone()[0])
        check(s == 202 and proto.get("speech", {}).get("active") is False, "экстренное завершение выключает запись речи")
    finally:
        p.terminate()
        p.wait(10)


def part_assistant_log():
    print("--- журнал assistant ---")
    from assistant import main as assistant
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    logging.getLogger("mars.assistant").addHandler(h)
    real = assistant.ollama
    assistant.ollama = lambda path, body=None, timeout=300: {"message": {"content": "Иван Петров работает в Сбербанке, живёт в Казани"}}
    try:
        items = assistant.find_entities("текст")
    finally:
        assistant.ollama = real
        logging.getLogger("mars.assistant").removeHandler(h)
    log = buf.getvalue()
    check(items == [] and "модель вернула не JSON" in log, "неверный ответ модели замечен и записан в журнал")
    check("Петров" not in log and "Казан" not in log, "в журнале нет имён и мест из ответа модели")


def main():
    tmp = tempfile.mkdtemp(prefix="mars_cons_")
    part_gateway(tmp)
    part_assistant_log()
    print("\nИтог: %s" % ("всё прошло" if not FAILS else "ошибок: %d" % len(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
