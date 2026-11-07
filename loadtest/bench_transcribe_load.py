# -*- coding: utf-8 -*-
"""Расшифровка под растущей нагрузкой: без параллельности (Python 3.12, 1 исполнитель) и с потоками
(Python 3.14t без GIL, 2 исполнителя). Только сервис speech, остальная система не запущена.

Ступени: 1, 2, 4, 8 пар. Каждая сторона пары (врач и пациент) отправляет кусок 10 с речи раз в 10 с,
как браузер; куски одной пары расшифровываются по порядку, как в gateway. Речь идёт --speak секунд,
потом очередь дорабатывается до конца.

Метрики по ступени: отставание текста (от отправки куска до готового текста), сколько ждать конца
очереди после окончания речи, кусков в минуту, сбои, совпадение текста с расшифровкой без нагрузки.

    python3 loadtest/bench_transcribe_load.py --steps 1,2,4,8 --speak 60
"""
import argparse
import base64
import json
import os
import queue
import statistics
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
sys.path.insert(0, str(ROOT / "loadtest"))
from shared import security  # noqa: E402
from bench_transcribe import wer  # noqa: E402

LT = ROOT / "loadtest"
MODELS = str(Path.home() / ".cache" / "mars_models")
PORT, SECRET, CHUNK_S = 18804, "bench-load-secret", 10
VARIANTS = [
    ("без параллельности", "Python 3.12, 1 исполнитель", ROOT / "speech" / ".venv" / "bin" / "python", {"SPEECH_WORKERS": "1"}),
    ("с потоками", "Python 3.14t без GIL, 2 исполнителя", ROOT / "speech" / ".venv314t" / "bin" / "python", {"SPEECH_WORKERS": "2", "PYTHON_GIL": "0"}),
]


def call(path, body=None, timeout=600):
    raw = json.dumps(body).encode() if body is not None else b""
    method = "POST" if body is not None else "GET"
    h = {"Content-Type": "application/json"}
    h.update(security.sign(SECRET, method, path, raw))
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=raw or None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:  # noqa: BLE001  обрыв или таймаут
        return -1, None


def step(n_pairs, chunks, speak_s, audio):
    """Одна ступень: n пар говорят speak_s секунд, затем очередь дорабатывается."""
    rows, lock = [], threading.Lock()
    t0 = time.time()
    cycles = int(speak_s // CHUNK_S)

    def pair(i):
        q = queue.Queue()

        def producer():
            for c in range(cycles):
                for role in ("doctor", "patient"):
                    path = chunks[role][(c + i) % len(chunks[role])]
                    q.put((role, path, time.time()))
                time.sleep(max(0.0, t0 + (c + 1) * CHUNK_S + i * 0.7 - time.time()))
            q.put(None)

        threading.Thread(target=producer, daemon=True).start()
        while True:  # куски пары по порядку: следующий уходит после готовности предыдущего
            item = q.get()
            if item is None:
                return
            role, path, sent = item
            s, d = call("/v1/transcribe", {"audio_b64": audio[path]})
            done = time.time()
            with lock:
                rows.append({"pair": i, "role": role, "chunk": Path(path).name, "sent": sent, "done": done, "ok": s == 200,
                             "text": " ".join(x["text"] for x in d["segments"]) if s == 200 and d else ""})

    ths = [threading.Thread(target=pair, args=(i,)) for i in range(n_pairs)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return rows, t0, t0 + cycles * CHUNK_S


def run_variant(py, env_extra, steps, speak_s, chunks, audio):
    tmp = tempfile.mkdtemp(prefix="mars_bench_load_")
    env = dict(os.environ, DATA_DIR=tmp, MODELS_DIR=MODELS, SERVICE_SECRET=SECRET, MARS_SPEECH_PORT=str(PORT), PYTHONUNBUFFERED="1", **env_extra)
    p = subprocess.Popen([str(py), "-m", "speech.main"], cwd=str(ROOT), env=env, stdout=open(Path(tmp) / "speech.log", "w"), stderr=subprocess.STDOUT)
    try:
        ready = None
        for _ in range(180):
            s, ready = call("/ready", timeout=3)
            if s == 200:
                break
            time.sleep(1)
        out = []
        for n in steps:
            rows, t0, t_speak_end = step(n, chunks, speak_s, audio)
            out.append({"pairs": n, "rows": rows, "t0": t0, "speak_end": t_speak_end})
            print("  %d пар: %d кусков, конец очереди через %.0f с после речи" % (n, len(rows), max(r["done"] for r in rows) - t_speak_end), flush=True)
        return ready, out
    finally:
        p.terminate()
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()


def pct(v, q):
    v = sorted(v)
    k = (len(v) - 1) * q / 100
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--steps", default="1,2,4,8")
    ap.add_argument("--speak", type=float, default=60, help="сколько секунд говорят пары на каждой ступени")
    a = ap.parse_args()
    steps = [int(x) for x in a.steps.split(",")]
    idx = json.loads((LT / "audio" / "index.json").read_text(encoding="utf-8"))
    chunks = {r: [str(LT / "audio" / r / Path(x[0]).name) for x in idx[r]] for r in ("doctor", "patient")}
    audio = {p: base64.b64encode(Path(p).read_bytes()).decode() for r in chunks for p in chunks[r]}
    results = []
    for name, desc, py, env in VARIANTS:
        print("%s (%s)" % (name, desc), flush=True)
        ready, steps_out = run_variant(py, env, steps, a.speak, chunks, audio)
        results.append({"variant": name, "desc": desc, "ready": ready, "steps": steps_out})
    # эталон качества: текст каждого куска при 1 паре без параллельности
    ref = {r["chunk"] + r["role"]: r["text"] for r in results[0]["steps"][0]["rows"]}
    summary = []
    for res in results:
        for st in res["steps"]:
            rows = st["rows"]
            lag = [r["done"] - r["sent"] for r in rows if r["ok"]]
            span = max(r["done"] for r in rows) - st["t0"]
            same = [r for r in rows if r["chunk"] + r["role"] in ref]
            summary.append({
                "variant": res["variant"], "pairs": st["pairs"], "chunks": len(rows), "fails": sum(1 for r in rows if not r["ok"]),
                "lag_p50_s": round(pct(lag, 50), 1), "lag_p95_s": round(pct(lag, 95), 1), "lag_max_s": round(max(lag), 1),
                "tail_after_speech_s": round(max(r["done"] for r in rows) - st["speak_end"], 1),
                "chunks_per_min": round(len(rows) / span * 60, 1),
                "text_same_pct": round(100 * sum(1 for r in same if r["text"] == ref[r["chunk"] + r["role"]]) / max(1, len(same)), 1),
                "wer_vs_unloaded_pct": round(100 * statistics.mean(wer(ref[r["chunk"] + r["role"]], r["text"]) for r in same), 1) if same else None,
            })
    (LT / "results_bench").mkdir(exist_ok=True)
    (LT / "results_bench" / "transcribe_load.json").write_text(json.dumps({"speak_s": a.speak, "summary": summary,
                                                                            "variants": [{k: v for k, v in r.items() if k != "steps"} for r in results]},
                                                                           ensure_ascii=False, indent=1), encoding="utf-8")
    for s in summary:
        print(json.dumps(s, ensure_ascii=False))


if __name__ == "__main__":
    main()
