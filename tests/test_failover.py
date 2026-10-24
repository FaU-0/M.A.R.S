# -*- coding: utf-8 -*-
"""Этап 5: полный набор сервисов, каждый останавливается по очереди. Проверяется таблица
«Что работает, когда сервис упал» (README, раздел «Архитектура»):

    vision     звонок и чат работают; расчёт ставится в очередь и выполняется после возвращения
    reports    чат работает; заключение 503 degraded с Retry-After (браузер печатает сам)
    speech     чат работает; куски речи принимаются и ждут на диске, потом догружаются
    assistant  чат и расшифровка работают; обезличивание ждёт, необезличенный текст не подтверждается
    database   сигнализация звонка идёт; остальное 503 degraded; после возвращения или перезапуска всё работает
    gateway    вторичные сервисы живы; после перезапуска gateway (run_all) очередь на диске дорабатывается

Сервис останавливается сигналом SIGSTOP (завис, порт занят, но не отвечает) и продолжает по SIGCONT.
gateway убивается SIGKILL: run_all.py поднимает его сам. Видеозвонок идёт напрямую между браузерами,
его этот тест не проверяет.

    python tests/test_failover.py
Нужны speech/.venv (faster-whisper) и запущенная Ollama с моделью OLLAMA_MODEL (по умолчанию qwen2.5:3b).
"""
import http.cookiejar
import json
import os
import signal
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

BASE = 19000
PORTS = {"gateway": BASE, "vision": BASE + 1, "reports": BASE + 2, "speech": BASE + 3, "assistant": BASE + 4, "database": BASE + 5}
FAILS = []


def check(c, m):
    print(("OK    " if c else "FAIL  ") + m)
    if not c:
        FAILS.append(m)


class Gw:
    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, tok=None, raw=None, headers=None, port=BASE, timeout=15):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method, data=data, headers=h)
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        try:
            with self.op.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "null"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "null"), dict(e.headers)

    def up(self, port=BASE):
        try:
            return self.call("GET", "/health", port=port, timeout=2)[0] == 200
        except Exception:  # noqa: BLE001
            return False


def until(fn, secs, step=0.5):
    t = time.time()
    while time.time() - t < secs:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def pids(pattern):
    return [int(x) for x in subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()]


class Session:
    """Приём: врач и пациент в комнате, пациент дал согласие на расшифровку речи."""

    def __init__(self, g, d):
        self.g, self.d = g, d
        g.call("POST", "/auth/login", {"login": "admin", "password": "test-pass-1234"})
        s, ap, _ = g.call("POST", "/appointments", {"full_name": d["known"]["patient"]["full_name"]})
        self.appt = ap["id"]
        self.open_room()

    def open_room(self):
        s, rm, _ = self.g.call("POST", "/rooms", {"appointment_id": self.appt})
        self.code, self.doc = rm["room"], rm["token"]
        s, pj, _ = self.g.call("POST", "/rooms/%s/join" % self.code, {"consent": True})
        self.pat = pj.get("token")
        self.ev("control", {"action": "speech_consent", "granted": True}, self.pat)

    def ev(self, typ, data, tok):
        return self.g.call("POST", "/rooms/%s/events" % self.code, {"type": typ, "data": data}, tok=tok)[0]

    def chat_ok(self, text):
        """Пациент пишет в чат, врач видит сообщение."""
        if self.ev("chat", {"text": text}, self.pat) not in (200, 202):
            return False
        s, evs, _ = self.g.call("GET", "/rooms/%s/events?after=0" % self.code, tok=self.doc)
        return s == 200 and any(e.get("type") == "chat" and e["data"].get("text") == text for e in evs.get("events", []))

    def audio(self, path, key):
        return self.g.call("POST", "/rooms/%s/audio" % self.code, tok=self.pat, raw=Path(path).read_bytes(),
                           headers={"Content-Type": "audio/webm", "X-Chunk-Key": key, "X-Seq": "0", "X-Duration-Ms": "10000"})

    def tstatus(self):
        return self.g.call("GET", "/rooms/%s/transcript/status" % self.code, tok=self.doc)[1]

    def deid(self):
        return (self.g.call("GET", "/rooms/%s/transcript" % self.code, tok=self.doc)[1] or {}).get("deid") or {}


def status_of(g):
    try:
        return g.call("GET", "/status", timeout=5)[1]
    except Exception:  # noqa: BLE001
        return None


def main():
    tmp = tempfile.mkdtemp(prefix="mars_f_")
    venv = ROOT / "speech" / ".venv" / "bin" / "python"
    p = subprocess.run([str(venv), "-c", "import sys; sys.path.insert(0, 'tests'); import audio_tools as a, json; "
                        "print(json.dumps(a.chunks(sys.argv[1], sys.argv[2])))",
                        "Здравствуйте, доктор. Третий день болит голова, температура тридцать семь и пять.", tmp + "/audio_src"],
                       cwd=str(ROOT), capture_output=True, text=True)
    if p.returncode:
        check(False, "тестовая речь: " + p.stderr[-300:])
        return finish()
    chunk = json.loads(p.stdout.strip().splitlines()[-1])[0][0]

    env = dict(os.environ, DATA_DIR=tmp, PORT=str(BASE), ADMIN_PASSWORD="test-pass-1234",
               MODELS_DIR=str(Path.home() / ".cache" / "mars_models"))
    for n, port in PORTS.items():
        if n != "gateway":
            env["MARS_%s_PORT" % n.upper()] = str(port)
    proc = subprocess.Popen([sys.executable, str(ROOT / "run_all.py")], env=env, cwd=str(ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stopped = []
    try:
        g = Gw()
        st = until(lambda: (status_of(g) or {}).get("status") == "ok" and status_of(g), 180, 1)
        check(bool(st) and len(st["services"]) == 5, "все 6 сервисов запущены, /status = ok")
        if not st:
            print("  /status:", status_of(g))
            return
        d = deid_dialogs.make()[2]
        S = Session(g, d)
        check(S.chat_ok("до отказов"), "чат работает")
        s, _, _ = S.audio(chunk, "base0001-0")
        check(until(lambda: S.tstatus().get("done") == 1, 90), "кусок речи расшифрован до отказов")

        def stop(name):
            ps = pids("%s.main" % name)
            for x in ps:
                os.kill(x, signal.SIGSTOP)
            stopped.extend(ps)
            return ps

        def cont(ps):
            for x in ps:
                os.kill(x, signal.SIGCONT)
                stopped.remove(x)

        def degraded(name):
            return until(lambda: (lambda s: s and s["status"] == "degraded" and s["services"][name]["up"] is False)(status_of(g)), 20, 1)

        def ok_again():
            return until(lambda: (status_of(g) or {}).get("status") == "ok", 60, 1)

        # ---- vision
        print("--- vision остановлен ---")
        ps = stop("vision")
        check(degraded("vision"), "vision: /status = degraded, vision недоступен")
        check(S.chat_ok("при отказе vision"), "vision: чат работает")
        sys.path.insert(0, str(ROOT))
        import test_vision
        sym = test_vision.build_cases(5)["sym"][0]
        s, j, _ = g.call("POST", "/rooms/%s/analyze" % S.code, {"kind": "symmetry", "key": "fo-sym-0001",
                         "input": {"frames": sym["frames"], "tolerance": sym["tol"]}}, tok=S.pat)
        check(s == 202, "vision: расчёт принят в очередь (браузер пока считает сам)")
        cont(ps)
        jd = until(lambda: (lambda r: r if r.get("status") in ("done", "error") else None)(
            g.call("GET", "/rooms/%s/jobs/%d" % (S.code, j["id"]), tok=S.pat)[1]), 90, 1)
        check(jd and jd["status"] == "done", "vision вернулся: расчёт из очереди выполнен")
        check(ok_again(), "vision вернулся: /status = ok")

        # ---- reports
        print("--- reports остановлен ---")
        ps = stop("reports")
        check(degraded("reports"), "reports: /status = degraded")
        check(S.chat_ok("при отказе reports"), "reports: чат работает")
        payload = {"center": "Центр", "doctor": "Врач", "patient": "Пациент", "date": "2026-10-24",
                   "conclusion": {"complaints": "головная боль", "diagnosis": "ОРВИ", "icd": "J06.9"}}
        t = time.time()
        s, r, hd = g.call("POST", "/rooms/%s/render-conclusion" % S.code, payload, tok=S.doc)
        check(s == 503 and r.get("degraded") is True and "Retry-After" in hd and time.time() - t < 6,
              "reports: заключение 503 degraded с Retry-After за %.1f с (браузер печатает сам)" % (time.time() - t))
        cont(ps)
        check(until(lambda: g.call("POST", "/rooms/%s/render-conclusion" % S.code, payload, tok=S.doc)[0] == 200, 40, 2),
              "reports вернулся: заключение снова строит сервис")
        check(ok_again(), "reports вернулся: /status = ok")

        # ---- speech
        print("--- speech остановлен ---")
        ps = stop("speech")
        check(degraded("speech"), "speech: /status = degraded")
        check(S.chat_ok("при отказе speech"), "speech: чат работает")
        s, _, _ = S.audio(chunk, "spdown01-0")
        check(s == 202, "speech: кусок речи принят")
        time.sleep(3)
        st = S.tstatus()
        check(st.get("queued") == 1 and st.get("service_up") is False, "speech: кусок ждёт на диске, врач видит, что сервис недоступен")
        cont(ps)
        check(until(lambda: S.tstatus().get("queued") == 0 and S.tstatus().get("done") == 2, 200, 1), "speech вернулся: кусок догрузился сам")
        check(ok_again(), "speech вернулся: /status = ok")

        # ---- assistant
        print("--- assistant остановлен ---")
        ps = stop("assistant")
        check(degraded("assistant"), "assistant: /status = degraded")
        check(S.chat_ok("при отказе assistant"), "assistant: чат работает")
        s, _, _ = S.audio(chunk, "asdown01-0")
        check(s == 202 and until(lambda: S.tstatus().get("done") == 3, 90), "assistant: расшифровка речи работает")
        s, r, _ = g.call("POST", "/rooms/%s/transcript/deidentify" % S.code, {}, tok=S.doc)
        check(s == 202 and r["deid"]["status"] == "queued", "assistant: обезличивание поставлено в очередь")
        s, _, _ = g.call("POST", "/rooms/%s/transcript/edit" % S.code, {"op": "confirm"}, tok=S.doc)
        check(s == 409, "assistant: необезличенный текст нельзя подтвердить для отчёта")
        s, o, _ = g.call("GET", "/rooms/%s/transcript/original" % S.code, tok=S.doc)
        check(s == 200 and len(o["segments"]) >= 3, "assistant: исходная расшифровка сохранена")
        cont(ps)
        check(until(lambda: S.deid().get("status") == "done", 300, 2), "assistant вернулся: обезличивание выполнено")
        check(ok_again(), "assistant вернулся: /status = ok")

        # ---- database
        print("--- database остановлен ---")
        ps = stop("database")
        check(degraded("database"), "database: /status = degraded")
        check(g.up(), "database: gateway отвечает")
        t = time.time()
        sig = S.ev("ice", {"candidate": "test-candidate"}, S.doc)
        s, evs, _ = g.call("GET", "/rooms/%s/events?after=0" % S.code, tok=S.pat)
        got = s == 200 and any(e.get("type") == "ice" and e["data"].get("candidate") == "test-candidate" for e in evs.get("events", []))
        check(sig in (200, 202) and got and time.time() - t < 3, "database: сигнализация звонка проходит (идущий звонок не обрывается)")
        t = time.time()
        s, r, hd = g.call("POST", "/rooms/%s/events" % S.code, {"type": "chat", "data": {"text": "при отказе database"}}, tok=S.pat)
        check(s == 503 and r.get("service") == "database" and r.get("degraded") is True and "Retry-After" in hd and time.time() - t < 7,
              "database: чат отвечает 503 degraded за %.1f с, а не зависает" % (time.time() - t))
        t = time.time()
        s, _, _ = g.call("GET", "/rooms/%s/transcript/status" % S.code, tok=S.doc)
        check(s == 503 and time.time() - t < 2, "database: выключатель цепи открыт, следующий запрос сразу 503 (%.1f с)" % (time.time() - t))
        cont(ps)
        check(until(lambda: S.chat_ok("после возвращения database"), 30, 1), "database вернулся: чат снова работает")
        check(ok_again(), "database вернулся: /status = ok")
        for x in pids("database.main"):
            os.kill(x, signal.SIGKILL)
        check(until(lambda: S.chat_ok("после перезапуска database"), 60, 1), "database убит: run_all поднял его, чат работает")
        check(ok_again(), "database перезапущен: /status = ok")

        # ---- gateway
        print("--- gateway упал ---")
        ps_sp = stop("speech")  # чтобы кусок точно остался в очереди на диске, когда gateway упадёт
        s, _, _ = S.audio(chunk, "gwdown01-0")
        check(s == 202, "gateway: перед падением кусок речи поставлен в очередь")
        gw = pids("gateway/mars.py")
        for x in gw:
            os.kill(x, signal.SIGKILL)
        check(until(lambda: not g.up(), 5, 0.2), "gateway остановлен")
        alive = {n: g.up(PORTS[n]) for n in ("database", "vision", "reports", "assistant")}
        check(all(alive.values()), "gateway: вторичные сервисы живы %s" % alive)
        cont(ps_sp)
        check(until(lambda: g.up(), 30, 0.5), "gateway: run_all поднял его заново")
        g.call("POST", "/auth/login", {"login": "admin", "password": "test-pass-1234"})
        S.open_room()  # комнаты живут в памяти gateway: врач открывает приём заново
        check(S.chat_ok("после перезапуска gateway"), "gateway вернулся: чат работает")
        check(until(lambda: S.tstatus().get("done") == 4 and S.tstatus().get("queued") == 0, 200, 1),
              "gateway вернулся: кусок из очереди на диске расшифрован")
        st = S.tstatus()
        s, o, _ = g.call("GET", "/rooms/%s/transcript/original" % S.code, tok=S.doc)
        check(s == 200 and len(o["segments"]) >= 4 and S.deid().get("status") == "done",
              "gateway вернулся: расшифровка и обезличенный текст на месте")
        check(ok_again(), "gateway вернулся: /status = ok")
    finally:
        for x in list(stopped):
            try:
                os.kill(x, signal.SIGCONT)
            except OSError:
                pass
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
