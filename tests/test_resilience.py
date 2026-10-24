# -*- coding: utf-8 -*-
"""Этап 0: запуск через run_all, /status, остановка каждого сервиса по очереди.

    python tests/test_resilience.py
Порты сдвинуты на 18000+, данные во временной папке, боевые не затрагиваются.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = 18000
PORTS = {"gateway": BASE, "vision": BASE + 1, "reports": BASE + 2, "speech": BASE + 3, "assistant": BASE + 4, "database": BASE + 5}
FAILS = []


def check(cond, msg):
    print(("OK    " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def get(path, port=BASE, timeout=5):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def wait_status(want, timeout=40):
    t = time.time()
    last = None
    while time.time() - t < timeout:
        try:
            _, st = get("/status")
            last = st
            if st["status"] == want:
                return st
        except Exception:
            pass
        time.sleep(1)
    return last


def main():
    tmp = tempfile.mkdtemp(prefix="mars_t_")
    env = dict(os.environ, DATA_DIR=tmp, PORT=str(BASE), AUTH_REQUIRED="True", ADMIN_PASSWORD="test-pass-1234",
               MODELS_DIR=str(Path.home() / ".cache" / "mars_models"))  # speech грузит модель Whisper из общего кэша
    for n, p in PORTS.items():
        if n != "gateway":
            env["MARS_%s_PORT" % n.upper()] = str(p)
    p = subprocess.Popen([sys.executable, str(ROOT / "run_all.py")], env=env, cwd=str(ROOT),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        st = wait_status("ok", 120)
        check(st and st["status"] == "ok" and len(st["services"]) == 5, "все 5 вторичных сервисов готовы, /status = ok")
        s, _ = get("/health")
        check(s == 200, "gateway /health отвечает")
        for n in ("database", "vision", "reports", "speech", "assistant"):
            s, r = get("/ready", PORTS[n])
            check(s == 200 and r["ready"], "%s /ready" % n)
        # подпись: без неё вызов закрыт
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/v1/x" % PORTS["vision"], timeout=3)
            check(False, "вызов без подписи отклонён")
        except urllib.error.HTTPError as e:
            check(e.code == 401, "вызов без подписи отклонён (401)")
        # клиент с выключателем
        os.environ.update(env)
        from shared.client import ServiceClient
        c = ServiceClient("vision", probe_every=1.0)
        st_, d = c.call("GET", "/ready")
        check(st_ == 200, "подписанный вызов проходит")

        # останавливаем каждый вторичный сервис: gateway жив, /status = degraded
        for n in ("database", "vision", "reports", "speech", "assistant"):
            pids = subprocess.run(["pgrep", "-f", "%s.main" % n], capture_output=True, text=True).stdout.split()
            for pid in pids:
                os.kill(int(pid), signal.SIGSTOP)  # остановка без перезапуска run_all
            time.sleep(0.5)
            st = wait_status("degraded", 15)
            check(st and st["services"][n]["up"] is False and st["status"] == "degraded", "%s остановлен: /status показывает degraded" % n)
            others_up = all(v["up"] for k, v in st["services"].items() if k != n)
            check(others_up, "%s остановлен: остальные сервисы живы" % n)
            s, _ = get("/health")
            check(s == 200, "%s остановлен: gateway отвечает" % n)
            for pid in pids:
                os.kill(int(pid), signal.SIGCONT)
            st = wait_status("ok", 30)
            check(st and st["status"] == "ok", "%s возобновлён: /status снова ok" % n)
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
