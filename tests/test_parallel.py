# -*- coding: utf-8 -*-
"""Расшифровка разных пар врач-пациент идёт параллельно: на Python без GIL (3.14t) — потоками, иначе — процессами.

1. Выбор режима (shared/parallel.py) на разных интерпретаторах: 3.12, 3.14, 3.14t, 3.14t после импорта PyAV.
2. Сервис speech с SPEECH_WORKERS=2 в обоих режимах: два куска от разных пар расшифровываются одновременно
   разными исполнителями (разные потоки или разные процессы), и общее время меньше суммы.

    python tests/test_parallel.py
Нужны speech/.venv (Python 3.12, faster-whisper) и, для режима потоков, speech/.venv314t (Python 3.14t).
Модель Whisper small берётся из ~/.cache/mars_models (как в tests/test_speech.py).
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shared import security  # noqa: E402

MODELS = str(Path.home() / ".cache" / "mars_models")
PORT = 18603
SECRET = "test-parallel-secret"
FAILS = []
PY312 = ROOT / "speech" / ".venv" / "bin" / "python"
PY314T_VENV = ROOT / "speech" / ".venv314t" / "bin" / "python"
PY314T = ROOT / ".python314t" / "python" / "bin" / "python3.14t"
PY314 = Path("/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14")


def check(c, m):
    print(("OK    " if c else "FAIL  ") + m)
    if not c:
        FAILS.append(m)


def mode_of(py, code="", env=None):
    src = "import sys; sys.path.insert(0, %r); %s; from shared import parallel; import json; print(json.dumps(parallel.describe()))" % (str(ROOT), code or "pass")
    p = subprocess.run([str(py), "-c", src], capture_output=True, text=True, env=dict(os.environ, **(env or {})))
    return json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else {"error": p.stderr[-300:]}


def part_modes():
    print("--- выбор режима по интерпретатору ---")
    d = mode_of(sys.executable)
    check(d.get("mode") == "processes", "Python %s с GIL: процессы" % d.get("python"))
    if PY314.exists():
        d = mode_of(PY314)
        check(d.get("mode") == "processes" and d.get("gil_enabled"), "Python %s (обычная сборка, с GIL): процессы" % d.get("python"))
    if PY314T.exists():
        d = mode_of(PY314T)
        check(d.get("mode") == "threads" and d.get("free_threaded_build") and not d.get("gil_enabled"),
              "Python %s без GIL: потоки" % d.get("python"))
    if PY314T_VENV.exists():
        d = mode_of(PY314T_VENV, "import faster_whisper")
        check(d.get("mode") == "processes" and d.get("gil_enabled"),
              "3.14t после импорта faster-whisper: PyAV включил GIL, значит процессы")
        d = mode_of(PY314T_VENV, "import faster_whisper", {"PYTHON_GIL": "0"})
        check(d.get("mode") == "threads" and not d.get("gil_enabled"), "3.14t с PYTHON_GIL=0: GIL выключен, потоки")


def call(method, path, body=None, timeout=180):
    raw = json.dumps(body).encode() if body is not None else b""
    h = {"Content-Type": "application/json"}
    h.update(security.sign(SECRET, method, path, raw))
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=raw or None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def part_service(py, label, want, env_extra):
    print("--- speech, %s ---" % label)
    tmp = tempfile.mkdtemp(prefix="mars_par_")
    env = dict(os.environ, DATA_DIR=tmp, MODELS_DIR=MODELS, SERVICE_SECRET=SECRET, MARS_SPEECH_PORT=str(PORT),
               SPEECH_WORKERS="2", PYTHONUNBUFFERED="1", **env_extra)
    log = open(Path(tmp) / "speech.log", "w")
    p = subprocess.Popen([str(py), "-m", "speech.main"], cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        ready = None
        for _ in range(180):
            try:
                s, ready = call("GET", "/ready", timeout=3)
                if s == 200:
                    break
            except Exception:  # noqa: BLE001  сервис ещё стартует
                pass
            time.sleep(1)
        check(ready and ready.get("parallel") == want and ready.get("workers") == 2,
              "сервис готов: режим %s, исполнителей 2 (%s)" % (ready and ready.get("parallel"), ready and ready.get("python")))
        # два куска от разных пар: врач первой пары и пациент второй
        chunks = [ROOT / "loadtest" / "audio" / "doctor" / "chunk00.webm", ROOT / "loadtest" / "audio" / "patient" / "chunk00.webm"]
        out = [None, None]
        t0 = time.time()

        def go(i):
            out[i] = call("POST", "/v1/transcribe", {"audio_b64": base64.b64encode(chunks[i].read_bytes()).decode()})

        ths = [threading.Thread(target=go, args=(i,)) for i in range(2)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        wall = time.time() - t0
        ok = all(o and o[0] == 200 for o in out)
        check(ok and all(o[1]["segments"] for o in out), "оба куска расшифрованы, текст не пустой")
        if ok:
            w = [o[1]["worker"] for o in out]
            el = [o[1]["elapsed"] for o in out]
            distinct = (w[0]["thread"] != w[1]["thread"]) if want == "threads" else (w[0]["pid"] != w[1]["pid"])
            check(distinct, "разные пары у разных исполнителей: %s" % [("pid %d" % x["pid"], x["thread"]) for x in w])
            check(wall < sum(el) * 0.9, "куски шли одновременно: вместе %.1f с, по отдельности %.1f + %.1f с" % (wall, el[0], el[1]))
            print("  текст: %s" % out[0][1]["segments"][0]["text"][:70])
    finally:
        p.terminate()
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()


def main():
    part_modes()
    if PY312.exists():
        part_service(PY312, "Python 3.12, с GIL", "processes", {})
    if PY314T_VENV.exists():
        part_service(PY314T_VENV, "Python 3.14t, PYTHON_GIL=0", "threads", {"PYTHON_GIL": "0"})
    print("\nИтог: %s" % ("всё прошло" if not FAILS else "ошибок: %d" % len(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
