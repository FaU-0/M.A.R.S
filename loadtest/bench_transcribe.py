# -*- coding: utf-8 -*-
"""Сравнение расшифровки: без параллельности (Python 3.12, 1 исполнитель — как было) и с потоками
(Python 3.14t без GIL, 2 исполнителя). Только сервис speech, без остальной нагрузки.

Две пары говорят одновременно: пара А — реплики врача (11 кусков), пара Б — реплики пациента (14 кусков).
Внутри пары куски идут по порядку, как в gateway. Каждый вариант повторяется 3 раза.
Качество — доля ошибочных слов (WER) против текста, из которого синтезирована речь.

    python3 loadtest/bench_transcribe.py
"""
import base64
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shared import security  # noqa: E402

LT = ROOT / "loadtest"
MODELS = str(Path.home() / ".cache" / "mars_models")
PORT, SECRET, REPEATS = 18803, "bench-secret", 3
VARIANTS = [
    ("без параллельности: Python 3.12, 1 исполнитель", ROOT / "speech" / ".venv" / "bin" / "python", {"SPEECH_WORKERS": "1"}),
    ("с потоками: Python 3.14t без GIL, 2 исполнителя", ROOT / "speech" / ".venv314t" / "bin" / "python", {"SPEECH_WORKERS": "2", "PYTHON_GIL": "0"}),
]


def norm(text):
    t = text.lower().replace("ё", "е")
    t = re.sub(r"(\d)[.,](\d)", r"\1\2", t)  # 37.5 и 37,5 — одно и то же
    return re.findall(r"[a-zа-я0-9]+", t)


def wer(ref, hyp):
    """Доля ошибочных слов: (замены + пропуски + вставки) / слов в эталоне, расстояние Левенштейна по словам."""
    r, h = norm(ref), norm(hyp)
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hw in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw))
        prev = cur
    return prev[-1] / max(1, len(r))


def call(path, body, timeout=300):
    raw = json.dumps(body).encode() if body is not None else b""
    h = {"Content-Type": "application/json"}
    h.update(security.sign(SECRET, "POST" if body is not None else "GET", path, raw))
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=raw or None, method="POST" if body is not None else "GET", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:  # noqa: BLE001  обрыв или таймаут
        return -1, None


def run_variant(py, env_extra, pairs):
    tmp = tempfile.mkdtemp(prefix="mars_bench_")
    env = dict(os.environ, DATA_DIR=tmp, MODELS_DIR=MODELS, SERVICE_SECRET=SECRET, MARS_SPEECH_PORT=str(PORT), PYTHONUNBUFFERED="1", **env_extra)
    p = subprocess.Popen([str(py), "-m", "speech.main"], cwd=str(ROOT), env=env, stdout=open(Path(tmp) / "speech.log", "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(180):
            s, ready = call("/ready", None, timeout=3)
            if s == 200:
                break
            time.sleep(1)
        runs = []
        for _ in range(REPEATS):
            out = {k: [] for k in pairs}
            lat, fails = [], [0]

            def pair(name):
                for path in pairs[name]:
                    t = time.time()
                    s, d = call("/v1/transcribe", {"audio_b64": base64.b64encode(Path(path).read_bytes()).decode()})
                    lat.append(time.time() - t)
                    if s == 200 and d:
                        out[name].append(" ".join(x["text"] for x in d["segments"]))
                    else:
                        fails[0] += 1
                        out[name].append("")

            t0 = time.time()
            ths = [threading.Thread(target=pair, args=(n,)) for n in pairs]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            runs.append({"wall_s": time.time() - t0, "lat": lat, "fails": fails[0], "text": {k: " ".join(v) for k, v in out.items()}})
        return ready, runs
    finally:
        p.terminate()
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()


def main():
    idx = json.loads((LT / "audio" / "index.json").read_text(encoding="utf-8"))
    pairs = {r: [str(LT / "audio" / r / Path(x[0]).name) for x in idx[r]] for r in ("doctor", "patient")}
    ref = {r: idx[r + "_text"] for r in ("doctor", "patient")}
    n_chunks = sum(len(v) for v in pairs.values())
    audio_s = sum(x[1] for r in ("doctor", "patient") for x in idx[r])
    res = []
    for label, py, env in VARIANTS:
        ready, runs = run_variant(py, env, pairs)
        walls = [r["wall_s"] for r in runs]
        lat = [x for r in runs for x in r["lat"]]
        w = [statistics.mean(wer(ref[k], r["text"][k]) for k in ref) for r in runs]
        res.append({"variant": label, "python": (ready or {}).get("python"), "parallel": (ready or {}).get("parallel"),
                    "workers": (ready or {}).get("workers"), "vad": (ready or {}).get("vad"), "chunks": n_chunks, "audio_s": round(audio_s, 1),
                    "wall_s": [round(x, 1) for x in walls], "wall_mean_s": round(statistics.mean(walls), 1),
                    "chunks_per_min": round(n_chunks / statistics.mean(walls) * 60, 1),
                    "chunk_latency_mean_s": round(statistics.mean(lat), 2), "fails": sum(r["fails"] for r in runs),
                    "wer_pct": [round(100 * x, 1) for x in w], "wer_mean_pct": round(100 * statistics.mean(w), 1),
                    "same_text_all_repeats": len({json.dumps(r["text"], ensure_ascii=False) for r in runs}) == 1})
        print(json.dumps(res[-1], ensure_ascii=False), flush=True)
    (LT / "results_bench").mkdir(exist_ok=True)
    (LT / "results_bench" / "transcribe_compare.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
