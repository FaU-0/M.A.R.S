#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Запуск всех сервисов M.A.R.S. одной командой.

    python run_all.py                       все сервисы
    python run_all.py --only gateway,vision только перечисленные
    python run_all.py --without speech,assistant   без тяжёлых (Whisper, Ollama)

Упавший сервис перезапускается через несколько секунд, остальные продолжают работать.
Ctrl+C останавливает всё.
"""
import argparse
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SECONDARY = ["database", "vision", "reports", "speech", "assistant"]  # database первым: gateway ждёт его при запуске


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="через запятую: gateway,database,vision,reports,speech,assistant")
    ap.add_argument("--without", default="", help="сервисы, которые не запускать")
    a = ap.parse_args()

    names = ["gateway"] + SECONDARY
    if a.only:
        names = [n for n in names if n in {x.strip() for x in a.only.split(",")}]
    skip = {x.strip() for x in a.without.split(",") if x.strip()}
    names = [n for n in names if n not in skip]
    secondary = [n for n in names if n != "gateway"]

    env = dict(os.environ)
    env.setdefault("DATA_DIR", str(Path(env.get("DATA_DIR", "mars_data")).resolve()))
    env.setdefault("SERVICE_SECRET", secrets.token_hex(32))
    env["MARS_SERVICES"] = ",".join(secondary)
    env["PYTHONUNBUFFERED"] = "1"

    def cmd(n):
        if n == "gateway":
            return [sys.executable, str(ROOT / "gateway" / "mars.py")]
        venv = ROOT / n / ".venv" / "bin" / "python"  # у тяжёлого сервиса может быть своё окружение (speech: faster-whisper)
        return [str(venv) if venv.exists() else sys.executable, "-m", n + ".main"]

    procs = {}
    started = {}

    def start(n):
        procs[n] = subprocess.Popen(cmd(n), cwd=str(ROOT), env=env)
        started[n] = time.time()

    def on_term(*_):  # SIGTERM тоже останавливает дочерние сервисы, иначе они остаются сиротами
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)

    for n in secondary:  # сначала вторичные, gateway последним
        start(n)
    if "gateway" in names:
        start("gateway")
    print("Запущено: %s. Ctrl+C останавливает всё." % ", ".join(names))
    try:
        while True:
            time.sleep(2)
            for n, p in list(procs.items()):
                code = p.poll()
                if code is not None:
                    wait = 3 if time.time() - started[n] > 30 else 10
                    print("[run_all] %s завершился (код %s), перезапуск через %d с" % (n, code, wait))
                    time.sleep(wait)
                    start(n)
    except KeyboardInterrupt:
        print("\nОстановка...")
    finally:
        for p in procs.values():
            try:
                p.terminate()
            except OSError:
                pass
        for p in procs.values():
            try:
                p.wait(8)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
