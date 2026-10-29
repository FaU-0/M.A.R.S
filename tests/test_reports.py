# -*- coding: utf-8 -*-
"""Этап 1: заключение строит сервис reports; при его остановке gateway отвечает 503 (браузер печатает сам).
    python tests/test_reports.py
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
BASE = 18100
FAILS = []


def check(c, m):
    print(("OK    " if c else "FAIL  ") + m)
    if not c:
        FAILS.append(m)


def main():
    tmp = tempfile.mkdtemp(prefix="mars_r_")
    env = dict(os.environ, DATA_DIR=tmp, PORT=str(BASE), ADMIN_PASSWORD="test-pass-1234")
    for i, n in enumerate(("vision", "reports", "speech", "assistant", "database"), 1):
        env["MARS_%s_PORT" % n.upper()] = str(BASE + i)
    p = subprocess.Popen([sys.executable, str(ROOT / "run_all.py"), "--without", "vision,speech,assistant"], env=env,
                         cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def call(method, path, body=None, token=None):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (BASE, path), method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"})
            if token:
                req.add_header("Authorization", "Bearer " + token)
            try:
                with op.open(req, timeout=10) as r:
                    return r.status, json.loads(r.read().decode() or "null"), dict(r.headers)
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode() or "null"), dict(e.headers)

        for _ in range(40):
            try:
                call("GET", "/health")
                break
            except Exception:
                time.sleep(0.5)
        s, d, _ = call("POST", "/auth/login", {"login": "admin", "password": "test-pass-1234"})
        check(s == 200, "вход врача")
        s, d, _ = call("POST", "/rooms", {})
        check(s == 201, "комната создана")
        code, tok = d["room"], d["token"]
        payload = {"center": "Центр", "doctor": "Иванов", "patient": "<b>Пётр</b>", "date": "2026-10-24",
                   "conclusion": {"complaints": "кашель", "diagnosis": "ОРВИ", "icd": "J06.9", "recommendations": "покой"}}
        time.sleep(1)
        s, d, _ = call("POST", "/rooms/%s/render-conclusion" % code, payload, tok)
        check(s == 200 and "html" in d, "reports отдал HTML заключения")
        h = d.get("html", "")
        check("ОРВИ" in h and "Код МКБ-10</h2><p class=\"pre\">J06.9" in h and "кашель" in h, "в HTML есть диагноз, код МКБ-10 отдельной строкой и жалобы")
        s, d, _ = call("POST", "/rooms/%s/render-conclusion" % code, dict(payload, conclusion=dict(payload["conclusion"], diagnosis="Д" * 7900)), tok)
        h2 = d.get("html", "") if isinstance(d, dict) else ""
        check(s == 200 and "J06.9" in h2 and "[текст сокращён]" not in h2, "длинный диагноз (7900 знаков) печатается целиком и не вытесняет код МКБ-10")
        check("<b>Пётр</b>" not in h and "&lt;b&gt;" in h, "пользовательский текст экранирован")
        s, d, _ = call("POST", "/rooms/%s/render-conclusion" % code, dict(payload, conclusion={"icd": "xx"}), tok)
        check(s == 400, "неверный код МКБ отклонён на gateway (400)")

        pids = subprocess.run(["pgrep", "-f", "reports.main"], capture_output=True, text=True).stdout.split()
        for pid in pids:
            os.kill(int(pid), signal.SIGSTOP)
        t = time.time()
        s, d, hd = call("POST", "/rooms/%s/render-conclusion" % code, payload, tok)
        check(s == 503 and d.get("degraded") is True and d.get("service") == "reports", "reports остановлен: gateway отвечает 503 degraded")
        check("Retry-After" in hd, "в ответе есть Retry-After")
        check(time.time() - t < 6, "ответ быстрее таймаута (%.1f c)" % (time.time() - t))
        for _ in range(2):  # вместе с первым это три ошибки подряд
            call("POST", "/rooms/%s/render-conclusion" % code, payload, tok)
        t = time.time()
        s, d, _ = call("POST", "/rooms/%s/render-conclusion" % code, payload, tok)
        check(s == 503 and time.time() - t < 1.5, "выключатель цепи открыт: ответ мгновенный")
        s, _, _ = call("GET", "/health")
        check(s == 200, "gateway жив")
        for pid in pids:
            os.kill(int(pid), signal.SIGCONT)
        time.sleep(6)
        s, d, _ = call("POST", "/rooms/%s/render-conclusion" % code, payload, tok)
        check(s == 200, "reports возобновлён: работа восстановилась сама")
    finally:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
    print("\nИтог: %s" % ("всё прошло" if not FAILS else "ошибок: %d" % len(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
