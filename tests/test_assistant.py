# -*- coding: utf-8 -*-
"""Этап 4: обезличивание расшифровки (assistant + Ollama), вкладка «Расшифровка», правки врача.

1. Критерий этапа на 20 синтетических диалогах (tests/deid_dialogs.py) и 10 отложенных с незнакомыми
   именами и местами, с настоящей Ollama. Пороги заданы до прогона: структурированные данные >= 95%,
   имена и места >= 90%, медицинские слова сохранены >= 95%, лишних замен не больше одной на пять диалогов.
2. Через gateway: «Завершить приём» запускает обезличивание, оригиналы не выходят наружу,
   правки врача, подтверждение, журнал аудита.
3. Ollama недоступна: расшифровка сохранена, задание ждёт, в отчёт текст не попадает.

    python tests/test_assistant.py            всё
    python tests/test_assistant.py --quick    без прогона 20 диалогов
Нужна запущенная Ollama с моделью OLLAMA_MODEL (по умолчанию qwen2.5:3b).
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
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import deid_dialogs  # noqa: E402

THRESHOLDS = {"structured": 0.95, "entity": 0.90, "keep": 0.95}
FAILS = []


def check(c, m):
    print(("OK    " if c else "FAIL  ") + m)
    if not c:
        FAILS.append(m)


def part_quality():
    import eval_deid
    for title, dialogs in (("20 синтетических диалогов", deid_dialogs.make()),
                           ("10 отложенных диалогов (незнакомые имена и места)", deid_dialogs.make_holdout())):
        print("--- %s ---" % title)
        r = eval_deid.evaluate(llm=True, dialogs=dialogs)
        eval_deid.show(r)
        for k, name in (("structured", "структурированные данные"), ("entity", "имена, места, организации"), ("keep", "медицинские слова сохранены")):
            check(r[k] >= THRESHOLDS[k], "%s: %.1f%% (порог %.0f%%)" % (name, 100 * r[k], 100 * THRESHOLDS[k]))
        limit = len(dialogs) // 5
        check(len(r["extra"]) <= limit, "лишних замен: %d (порог %d, одна на пять диалогов)" % (len(r["extra"]), limit))


class Gw:
    def __init__(self, base):
        self.base = base
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, tok=None, timeout=20):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.base, path), method=method,
                                     data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        try:
            with self.op.open(req, timeout=timeout) as r:
                raw = r.read().decode()
                return r.status, json.loads(raw or "null"), raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            return e.code, json.loads(raw or "null"), raw


def start(base, without, extra=None):
    tmp = tempfile.mkdtemp(prefix="mars_a_")
    env = dict(os.environ, DATA_DIR=tmp, PORT=str(base), ADMIN_PASSWORD="test-pass-1234",
               MODELS_DIR=str(Path.home() / ".cache" / "mars_models"), **(extra or {}))
    for i, n in enumerate(("vision", "reports", "speech", "assistant", "database"), 1):
        env["MARS_%s_PORT" % n.upper()] = str(base + i)
    p = subprocess.Popen([sys.executable, str(ROOT / "run_all.py"), "--without", without], env=env, cwd=str(ROOT),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p, tmp


def stop(p):
    p.terminate()
    try:
        p.wait(15)
    except subprocess.TimeoutExpired:
        p.kill()


def setup_room(g, tmp, d):
    """Приём с карточкой пациента и именем врача из диалога; расшифровка записывается в базу,
    как будто speech уже расшифровал куски (речь проверяет test_speech.py)."""
    for _ in range(60):
        try:
            g.call("GET", "/health", timeout=2)
            break
        except Exception:
            time.sleep(0.5)
    g.call("POST", "/auth/login", {"login": "admin", "password": "test-pass-1234"})
    s, ap, _ = g.call("POST", "/appointments", {"full_name": d["known"]["patient"]["full_name"]})
    s, rm, _ = g.call("POST", "/rooms", {"appointment_id": ap["id"]})
    code, doc = rm["room"], rm["token"]
    s, pj, _ = g.call("POST", "/rooms/%s/join" % code, {"consent": True})
    db = sqlite3.connect(str(Path(tmp) / "mars.sqlite3"), timeout=10)
    db.execute("UPDATE doctors SET name=?", (d["known"]["doctor"]["name"],))
    t0 = int(time.time() * 1000)
    for k, sg in enumerate(d["segments"]):
        db.execute("INSERT INTO speech_segments(appt_id,role,chunk_t,seq,start,end,text,created) VALUES(?,?,?,?,?,?,?,?)",
                   (ap["id"], sg["role"], t0 + sg["t"], k, 0.0, 5.0, sg["text"], int(time.time())))
    db.execute("INSERT INTO jobs(appt_id,kind,key,status,created,updated,input) VALUES(?,?,?,?,?,?,?)",
               (ap["id"], "transcribe", "p-test-0001", "done", int(time.time()), int(time.time()), "{}"))
    db.commit()
    return code, doc, pj["token"], db


def wait_deid(g, code, doc, secs):
    t = time.time()
    d = {}
    while time.time() - t < secs:
        s, r, _ = g.call("GET", "/rooms/%s/transcript" % code, tok=doc)
        d = r.get("deid") or {}
        if d.get("status") in ("done", "error") or (d.get("status") == "queued" and d.get("waiting")):
            break
        time.sleep(1)
    return d, time.time() - t


def part_gateway():
    print("--- через gateway ---")
    d = deid_dialogs.make()[0]
    p, tmp = start(18700, "vision,reports,speech")
    try:
        g = Gw(18700)
        code, doc, pat, db = setup_room(g, tmp, d)
        t = time.time()
        while time.time() - t < 30:
            s, st, _ = g.call("GET", "/status")
            if st["services"].get("assistant", {}).get("up"):
                break
            time.sleep(1)
        check(st["services"]["assistant"]["up"], "assistant готов (Ollama и модель на месте)")
        s, r, _ = g.call("GET", "/rooms/%s/transcript" % code, tok=doc)
        check(r["deid"]["status"] == "none", "до завершения приёма обезличенной расшифровки нет")
        s, _, _ = g.call("GET", "/rooms/%s/transcript" % code, tok=pat)
        check(s == 403, "пациенту расшифровка не выдаётся")
        s, _, _ = g.call("POST", "/rooms/%s/events" % code, {"type": "control", "data": {"action": "end"}}, tok=doc)
        dd, took = wait_deid(g, code, doc, 600)
        check(dd.get("status") == "done", "«Завершить приём» запустил обезличивание: готово за %.0f с" % took)
        s, r, raw = g.call("GET", "/rooms/%s/transcript" % code, tok=doc)
        text = "\n".join(x["text"] for x in r["deid"]["segments"])
        print("  " + text.replace("\n", "\n  "))
        full = d["known"]["patient"]["full_name"].split()
        leaks = [w for w in full + d["known"]["doctor"]["name"].split() if w in raw]
        check(not leaks, "в ответе нет ФИО пациента и врача%s" % (": " + ", ".join(leaks) if leaks else ""))
        check("[ФИО_ПАЦИЕНТА]" in text and "[ВРАЧ]" in text, "метки [ФИО_ПАЦИЕНТА] и [ВРАЧ] на месте")
        check(all("originals" not in x for x in r["deid"].get("replacements", [])), "список замен без оригиналов")
        check(r["deid"]["confirmed"] is False, "результат не подтверждён, пока врач не проверил")

        labels = [x["label"] for x in r["deid"]["replacements"]]
        lab = next((x for x in labels if x.startswith("[ИМЯ_")), labels[0])
        s, r, _ = g.call("POST", "/rooms/%s/transcript/edit" % code, {"op": "undo", "label": lab}, tok=doc)
        check(s == 200 and lab not in "\n".join(x["text"] for x in r["deid"]["segments"]), "«Отменить замену» %s вернула оригинал" % lab)
        drug = d["keep"][-2]
        s, r, _ = g.call("POST", "/rooms/%s/transcript/edit" % code, {"op": "add", "text": drug, "type": "OTHER"}, tok=doc)
        t2 = "\n".join(x["text"] for x in r["deid"]["segments"])
        check(s == 200 and drug not in t2 and "[СКРЫТО_1]" in t2, "«Добавить замену» скрыла «%s» во всём тексте" % drug)
        s, r, _ = g.call("POST", "/rooms/%s/transcript/edit" % code, {"op": "confirm"}, tok=doc)
        check(r["deid"]["confirmed"] is True, "врач подтвердил текст для отчёта")
        s, r, _ = g.call("POST", "/rooms/%s/transcript/edit" % code, {"op": "add", "text": "скорую", "type": "OTHER"}, tok=doc)
        check(r["deid"]["confirmed"] is False, "новая правка снимает подтверждение")
        s, o, raw = g.call("GET", "/rooms/%s/transcript/original" % code, tok=doc)
        check(s == 200 and full[0] in raw, "«Показать оригинал» отдаёт исходник врачу")
        acts = [a for (a,) in db.execute("SELECT action FROM audit WHERE action LIKE 'transcript%'")]
        check({"transcript_original_viewed", "transcript_replacement_undone", "transcript_replacement_added", "transcript_confirmed"} <= set(acts),
              "просмотр оригинала и правки записаны в журнал аудита")
        s, r, _ = g.call("POST", "/rooms/%s/transcript/deidentify" % code, {}, tok=doc)
        check(s == 202 and r["deid"]["status"] == "queued", "«Обезличить повторно» ставит задание в очередь")
        dd, took = wait_deid(g, code, doc, 600)
        check(dd.get("status") == "done" and "[СКРЫТО_1]" not in "\n".join(x["text"] for x in dd["segments"]),
              "повторное обезличивание готово, ручные правки сброшены (%.0f с)" % took)
    finally:
        stop(p)

    print("--- Ollama недоступна ---")
    p, tmp = start(18800, "vision,reports,speech", {"OLLAMA_URL": "http://127.0.0.1:9"})
    try:
        g = Gw(18800)
        code, doc, pat, db = setup_room(g, tmp, d)
        time.sleep(3)
        s, st, _ = g.call("GET", "/status")
        check(st["services"]["assistant"]["up"] is False and st["status"] == "degraded", "/status показывает assistant недоступным")
        s, _, _ = g.call("POST", "/rooms/%s/events" % code, {"type": "control", "data": {"action": "end"}}, tok=doc)
        dd, took = wait_deid(g, code, doc, 30)
        check(dd.get("status") == "queued" and dd.get("waiting") == "assistant", "обезличивание ждёт в очереди, врач видит, что сервис недоступен")
        s, o, raw = g.call("GET", "/rooms/%s/transcript/original" % code, tok=doc)
        check(s == 200 and len(o["segments"]) == len(d["segments"]), "исходная расшифровка сохранена")
        s, r, _ = g.call("POST", "/rooms/%s/transcript/edit" % code, {"op": "confirm"}, tok=doc)
        check(s == 409, "необезличенный текст нельзя подтвердить для отчёта")
        s, _, _ = g.call("GET", "/health")
        check(s == 200, "gateway отвечает")
    finally:
        stop(p)


def main():
    if "--quick" not in sys.argv:
        part_quality()
    part_gateway()
    print("\nИтог: %s" % ("всё прошло" if not FAILS else "ошибок: %d" % len(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
