#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Запуск всех сервисов M.A.R.S. одной командой.

    python run_all.py                       все сервисы
    python run_all.py --only gateway,vision только перечисленные
    python run_all.py --without speech,assistant   без тяжёлых (Whisper, Ollama)

Упавший сервис перезапускается через несколько секунд, остальные продолжают работать.
Ctrl+C останавливает всё.

Интерпретатор: по умолчанию Python 3.14t без GIL из .python314t/ (если скачан), speech — из speech/.venv314t,
и расшифровка разных пар идёт потоками. Иначе — тот Python, которым запущен run_all, и процессы.
MARS_PYTHON=путь задаёт интерпретатор явно, SPEECH_VENV=.venv — прежнее окружение speech (Python 3.12).
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
PY314T = ROOT / ".python314t" / "python" / "bin" / "python3.14t"
SECONDARY = ["database", "vision", "reports", "speech", "assistant"]  # database первым: gateway ждёт его при запуске


def base_python(env):
    """Интерпретатор для gateway и сервисов без своего окружения."""
    if env.get("MARS_PYTHON"):
        return env["MARS_PYTHON"]
    return str(PY314T) if PY314T.exists() else sys.executable


def speech_venv(env, base):
    """Окружение speech: явно заданное, иначе .venv314t вместе с Python 3.14t, иначе прежнее .venv."""
    if env.get("SPEECH_VENV"):
        return env["SPEECH_VENV"]
    return ".venv314t" if base == str(PY314T) and (ROOT / "speech" / ".venv314t" / "bin" / "python").exists() else ".venv"


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

    os.umask(0o077)  # дочерние сервисы создают файлы пациентов только для владельца
    env = dict(os.environ)
    env.setdefault("DATA_DIR", str(Path(env.get("DATA_DIR", "mars_data")).resolve()))
    env.setdefault("SERVICE_SECRET", secrets.token_hex(32))
    env["MARS_SERVICES"] = ",".join(secondary)
    env["PYTHONUNBUFFERED"] = "1"

    base = base_python(env)
    sp_venv = speech_venv(env, base)
    # на 3.14t PyAV включает GIL при импорте; PYTHON_GIL=0 держит его выключенным (SPEECH_NOGIL=0 — не держать)
    nogil = env.get("SPEECH_NOGIL", "1" if sp_venv == ".venv314t" else "0") == "1"
    print("Python: %s; speech: speech/%s%s" % (base, sp_venv, ", PYTHON_GIL=0" if nogil else ""), flush=True)

    def cmd(n):
        if n == "gateway":
            return [base, str(ROOT / "gateway" / "mars.py")]
        # у тяжёлого сервиса может быть своё окружение (speech: faster-whisper)
        venv = ROOT / n / (sp_venv if n == "speech" else ".venv") / "bin" / "python"
        return [str(venv) if venv.exists() else base, "-m", n + ".main"]

    procs = {}
    started = {}

    def start(n):
        e = env
        if n == "speech" and nogil:
            e = dict(env, PYTHON_GIL="0")
        procs[n] = subprocess.Popen(cmd(n), cwd=str(ROOT), env=e)
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
