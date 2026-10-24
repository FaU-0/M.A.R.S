# -*- coding: utf-8 -*-
"""Этап 3: speech (Whisper), куски речи, очередь, согласие пациента.

Речь синтезируется голосом macOS (say -v Milena) и режется на куски webm/opus по 10 с, как в браузере.
Модель берётся из ~/.cache/mars_models (скачивается при первом запуске).

    python tests/test_speech.py
"""
import http.cookiejar
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / "speech" / ".venv" / "bin" / "python"
BASE = 18500
MODELS = str(Path.home() / ".cache" / "mars_models")
TEXT = ("Здравствуйте, доктор. У меня третий день болит голова и немного кружится. Температура тридцать семь и пять. "
        "Я принимаю парацетамол два раза в день. Аллергии на лекарства у меня нет. Скажите, пожалуйста, нужно ли сдать анализ крови?")
FAILS = []


def check(c, m):
    print(("OK    " if c else "FAIL  ") + m)
    if not c:
        FAILS.append(m)


class Gw:
    def __init__(self, base):
        self.base = base
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, tok=None, raw=None, headers=None, timeout=15):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.base, path), method=method, data=data, headers=h)
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        try:
            with self.op.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "null")


def main():
    tmp = tempfile.mkdtemp(prefix="mars_s_")
    audio_dir = Path(tmp) / "test_audio"
    p = subprocess.run([str(VENV), "-c", "import sys; sys.path.insert(0, 'tests'); import audio_tools as a, json; "
                        "c = a.chunks(sys.argv[1], sys.argv[2]); a.silence(sys.argv[2] + '/silence.webm'); print(json.dumps(c))",
                        TEXT, str(audio_dir)], cwd=str(ROOT), capture_output=True, text=True)
    check(p.returncode == 0, "тестовая речь синтезирована и нарезана" + ("" if p.returncode == 0 else ": " + p.stderr[-300:]))
    if p.returncode:
        return finish()
    chunks = json.loads(p.stdout.strip().splitlines()[-1])
    total = sum(d for _, d in chunks)

    env = dict(os.environ, DATA_DIR=tmp, PORT=str(BASE), ADMIN_PASSWORD="test-pass-1234", MODELS_DIR=MODELS)
    for i, n in enumerate(("vision", "reports", "speech", "assistant", "database"), 1):
        env["MARS_%s_PORT" % n.upper()] = str(BASE + i)
    proc = subprocess.Popen([sys.executable, str(ROOT / "run_all.py"), "--without", "vision,reports,assistant"], env=env, cwd=str(ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        g = Gw(BASE)
        t = time.time()
        st = None
        while time.time() - t < 600:  # первый запуск может скачивать модель
            try:
                s, st = g.call("GET", "/status", timeout=3)
                if st["services"].get("speech", {}).get("up"):
                    break
            except Exception:
                pass
            time.sleep(1)
        check(st and st["services"]["speech"]["up"], "speech готов (модель загружена) за %.0f с" % (time.time() - t))
        g.call("POST", "/auth/login", {"login": "admin", "password": "test-pass-1234"})
        s, d = g.call("POST", "/rooms", {})
        code, doc = d["room"], d["token"]
        s, d = g.call("POST", "/rooms/%s/join" % code, {"consent": True})
        pat = d.get("token")
        check(s == 200 and pat, "пациент вошёл в комнату")

        def up(path, tok, key, seq, dur=10000):
            # куски уходят пачкой, как после обрыва связи: порядок держится на смещении от начала записи
            return g.call("POST", "/rooms/%s/audio" % code, tok=tok, raw=Path(path).read_bytes(),
                          headers={"Content-Type": "audio/webm", "X-Chunk-Key": key, "X-Seq": str(seq), "X-Duration-Ms": str(dur),
                                   "X-Offset-Ms": str(seq * 10000)})

        s, d = up(chunks[0][0], pat, "sess0001-0", 0)
        check(s == 403 and d.get("error") == "no_speech_consent", "без согласия пациента кусок не принимается (403)")
        s, _ = g.call("POST", "/rooms/%s/events" % code, {"type": "control", "data": {"action": "speech_consent", "granted": True}}, tok=doc)
        check(s == 403, "дать согласие за пациента врач не может")
        s, _ = g.call("POST", "/rooms/%s/events" % code, {"type": "control", "data": {"action": "speech_consent", "granted": True}}, tok=pat)
        check(s in (200, 202), "пациент дал согласие на расшифровку")
        s, pr = g.call("GET", "/rooms/%s/protocol" % code, tok=pat)
        check(pr.get("speech", {}).get("consent") is True and pr["speech"].get("active") is True, "состояние расшифровки в протоколе приёма")

        t_up = time.time()
        jobs = []
        for k, (path, dur) in enumerate(chunks):
            s, d = up(path, pat, "sess0001-%d" % k, k, int(dur * 1000))
            jobs.append(d.get("id"))
            time.sleep(0.2)
        s, d = up(str(audio_dir / "silence.webm"), doc, "sess0002-0", 0)
        check(s == 202, "кусок врача (тишина) принят")
        s, d = up(chunks[0][0], pat, "sess0001-0", 0)
        check(d.get("id") == jobs[0], "повтор куска с тем же ключом не создаёт дубль")
        s, _ = g.call("GET", "/rooms/%s/transcript" % code, tok=pat)
        check(s == 403, "пациенту расшифровка не выдаётся")

        st = {}
        t = time.time()
        while time.time() - t < 180:
            s, st = g.call("GET", "/rooms/%s/transcript/status" % code, tok=doc)
            if st.get("done", 0) + st.get("error", 0) >= len(chunks) + 1:
                break
            time.sleep(0.5)
        took = time.time() - t_up
        check(st.get("done") == len(chunks) + 1, "все куски расшифрованы: %s из %s" % (st.get("done"), st.get("chunks")))
        s, tr = g.call("GET", "/rooms/%s/transcript/original" % code, tok=doc)  # исходник; без /original - обезличенная (этап 4)
        text = " ".join(x["text"] for x in tr.get("segments", []))
        roles = {x["role"] for x in tr.get("segments", [])}
        check("голова" in text and "анализ" in text and "доктор" in text.lower(), "текст похож на сказанное")
        check(roles == {"patient"}, "тишина у врача не превратилась в текст, речь подписана как пациента")
        ts = [x["t"] for x in tr.get("segments", [])]
        check(ts == sorted(ts) and text.rstrip().endswith("крови?"), "сегменты идут по времени, даже если куски пришли пачкой")
        print("  расшифровка: " + text)
        db = sqlite3.connect(str(Path(tmp) / "mars.sqlite3"))
        res = [json.loads(r[0]) for r in db.execute("SELECT result FROM jobs WHERE kind='transcribe' AND status='done'")]
        el = sum(r.get("elapsed") or 0 for r in res)
        print("  скорость: %.1f с аудио за %.1f с работы модели (%.0f с на минуту речи), от загрузки до текста %.1f с, модель %s"
              % (total, el, el / total * 60, took, res[0].get("model") if res else "?"))
        check(not list((Path(tmp) / "audio").iterdir()), "аудио удалено после расшифровки")
        check(db.execute("SELECT COUNT(*) FROM audit WHERE action='transcript_original_viewed'").fetchone()[0] >= 1,
              "просмотр исходной расшифровки записан в журнал аудита")

        bad = Path(tmp) / "bad.webm"
        bad.write_bytes(os.urandom(4000))
        s, d = up(str(bad), pat, "sess0003-0", 0)
        t = time.time()
        while time.time() - t < 30:
            s, j = g.call("GET", "/rooms/%s/jobs/%d" % (code, d["id"]), tok=pat)
            if j.get("status") in ("done", "error"):
                break
            time.sleep(0.3)
        check(j.get("status") == "error" and j.get("error", {}).get("error") == "bad_audio", "битый кусок: ошибка bad_audio, не повтор без конца")
        check(not list((Path(tmp) / "audio").iterdir()), "битый кусок удалён")

        pids = subprocess.run(["pgrep", "-f", "speech.main"], capture_output=True, text=True).stdout.split()
        pids = [x for x in pids if x]
        for pid in pids:
            os.kill(int(pid), signal.SIGSTOP)
        t = time.time()
        s, d = up(chunks[0][0], pat, "sess0004-0", 0)
        check(s == 202 and time.time() - t < 2, "speech остановлен: кусок принят сразу")
        s, _ = g.call("GET", "/health")
        check(s == 200, "speech остановлен: gateway отвечает")
        time.sleep(4)
        s, st = g.call("GET", "/rooms/%s/transcript/status" % code, tok=doc)
        check(st.get("queued", 0) >= 1 and len(list((Path(tmp) / "audio").iterdir())) == 1, "speech остановлен: кусок ждёт в очереди на диске")
        for pid in pids:
            os.kill(int(pid), signal.SIGCONT)
        t = time.time()
        while time.time() - t < 200:
            s, st = g.call("GET", "/rooms/%s/transcript/status" % code, tok=doc)
            if not st.get("queued"):
                break
            time.sleep(1)
        check(not st.get("queued") and st.get("done") == len(chunks) + 2, "speech возобновлён: кусок догрузился сам (%.0f с)" % (time.time() - t))

        s, _ = g.call("POST", "/rooms/%s/events" % code, {"type": "control", "data": {"action": "speech_stop"}}, tok=doc)
        s, d = up(chunks[0][0], pat, "sess0005-0", 0)
        check(s == 403, "врач остановил расшифровку: куски больше не принимаются")
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
    finish()


def finish():
    print("\nИтог: %s" % ("всё прошло" if not FAILS else "ошибок: %d" % len(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
