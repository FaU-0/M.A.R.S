# -*- coding: utf-8 -*-
"""Нагрузочный тест M.A.R.S.: виртуальные пары «врач + пациент» проходят полный приём через API gateway.

Стенд поднимается заново (run_all.py, порты 18000+, временный DATA_DIR), рабочие данные mars_data не трогаются.
Ступени нагрузки идут подряд на одном стенде: 1, 2, 4, 8, 16 пар (параметр --steps).

    python3 loadtest/run.py --steps 1,2,4,8,16 --duration 120 --drain 60

Сырые результаты: loadtest/results/*.csv и steps.json. Графики и отчёт строит loadtest/report.py.
Видео и звук звонка идут между браузерами напрямую (WebRTC) и сервер не нагружают, поэтому здесь их нет.
"""
import argparse
import csv
import http.cookiejar
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
LT = ROOT / "loadtest"
sys.path.insert(0, str(ROOT / "tests"))

BASE = 18000
SERVICES = ("vision", "reports", "speech", "assistant", "database")
ADMIN_PW = "test-pass-1234"
CHUNK_S = 10          # браузер отправляет кусок речи каждые 10 с (e_speech.js CHUNK)
DOCTOR_EVERY_S = 30   # карточка, заметки и чат врача
POLL_WAIT_S = 20      # длинный опрос сигнализации, как в браузере (до 20 с)
MAX_DOCTORS = 8       # вход ограничен 8 попытками с одного IP (ip_limit «login»), см. отчёт

LOCK = threading.Lock()
REQS, SIGNALS, SENDS, MARKS = [], [], [], []
STEP = {"n": 0}


def label_of(method, path):
    p = path.split("?")[0]
    p = re.sub(r"/rooms/[A-Za-z0-9]{4,12}", "/rooms/{c}", p)
    p = re.sub(r"/(jobs|files|photos|patients)/\d+", r"/\1/{id}", p)
    return "%s %s" % (method, p)


class Client:
    """HTTP-клиент одной стороны: cookie врача или токен комнаты, каждый запрос записывается."""

    def __init__(self, port, tok=None):
        self.port, self.tok = port, tok
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def with_token(self, tok):
        """Тот же вход (cookie), но с токеном комнаты: ссылка врача действует только вместе с его сессией."""
        c = Client(self.port, tok)
        c.op = self.op
        return c

    def call(self, method, path, body=None, raw=None, headers=None, timeout=60, record=True):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), method=method, data=data, headers=h)
        if self.tok:
            req.add_header("Authorization", "Bearer " + self.tok)
        t0 = time.perf_counter()
        status, out = 0, None
        try:
            with self.op.open(req, timeout=timeout) as r:
                status, txt = r.status, r.read().decode() or "null"
        except urllib.error.HTTPError as e:
            status, txt = e.code, e.read().decode() or "null"
        except Exception as e:  # noqa: BLE001  обрыв, таймаут, отказ соединения
            status, txt = -1, json.dumps({"error": type(e).__name__})
        ms = (time.perf_counter() - t0) * 1000
        try:
            out = json.loads(txt)
        except ValueError:
            out = None
        degraded = isinstance(out, dict) and out.get("degraded") is True
        if record:
            with LOCK:
                REQS.append((STEP["n"], label_of(method, path), status, round(ms, 1), round(time.time(), 3), int(degraded)))
        return status, out


# ---------------- стенд ----------------
def start_stack(data_dir, env_extra):
    env = dict(os.environ, DATA_DIR=str(data_dir), PORT=str(BASE), ADMIN_PASSWORD=ADMIN_PW)
    for i, n in enumerate(SERVICES, 1):
        env["MARS_%s_PORT" % n.upper()] = str(BASE + i)
    env.update(env_extra)
    log = open(data_dir / "run_all.log", "w")
    p = subprocess.Popen([sys.executable, str(ROOT / "run_all.py")], cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    c = Client(BASE)
    t = time.time()
    while time.time() - t < 240:  # speech грузит модель Whisper в фоне: ждём, пока /status скажет ok
        time.sleep(2)
        s, st = c.call("GET", "/status", record=False, timeout=5)
        if s == 200 and isinstance(st, dict) and st.get("status") == "ok" and st.get("services"):
            return p, round(time.time() - t, 1)
    raise SystemExit("стенд не поднялся за 240 с, см. %s" % (data_dir / "run_all.log"))


def service_procs(stack):
    """Процессы стенда по командной строке (run_all запускает gateway/mars.py и python -m <сервис>.main) и Ollama."""
    out = {}
    for p in psutil.Process(stack.pid).children(recursive=True):
        try:
            cl = " ".join(p.cmdline())
        except psutil.Error:
            continue
        if "gateway/mars.py" in cl:
            out["gateway"] = p
        for n in SERVICES:
            if "%s.main" % n in cl:
                # процессы пула (режим «процессы» в speech) считаются вместе с сервисом
                out[n] = [p] + p.children(recursive=True)
    out["run_all"] = psutil.Process(stack.pid)
    # Ollama не входит в run_all: это отдельные процессы ollama serve и llama-server
    out["ollama"] = [p for p in psutil.process_iter(["name", "cmdline"])
                     if (p.info["name"] or "").lower().startswith("ollama") or "llama-server" in " ".join(p.info["cmdline"] or [])]
    out["generator"] = psutil.Process(os.getpid())
    return out


def sampler(stop, stack, data_dir, rows, qrows):
    procs = service_procs(stack)
    flat = lambda: [(k, p) for k, v in procs.items() for p in (v if isinstance(v, list) else [v])]  # noqa: E731
    for _, p in flat():
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    psutil.cpu_percent(None)
    db = data_dir / "mars.sqlite3"
    tick = 0
    while not stop.wait(2.0):
        t = round(time.time(), 1)
        agg = {}
        for k, p in flat():
            try:
                c, m = p.cpu_percent(None), p.memory_info().rss / 2 ** 20
            except psutil.Error:
                continue
            a = agg.setdefault(k, [0.0, 0.0])
            a[0] += c
            a[1] += m
        vm = psutil.virtual_memory()
        sizes = sum(f.stat().st_size for f in data_dir.rglob("*") if f.is_file()) / 2 ** 20 if tick % 5 == 0 else None
        dbs = sum((data_dir / n).stat().st_size for n in ("mars.sqlite3", "mars.sqlite3-wal") if (data_dir / n).exists()) / 2 ** 20
        rows.append({"t": t, "step": STEP["n"], "system_cpu": psutil.cpu_percent(None), "avail_mb": round(vm.available / 2 ** 20),
                     "db_mb": round(dbs, 2), "data_mb": round(sizes, 2) if sizes is not None else "",
                     **{"%s_cpu" % k: round(v[0], 1) for k, v in agg.items()}, **{"%s_mb" % k: round(v[1], 1) for k, v in agg.items()}})
        if tick % 2 == 0 and db.exists():  # очередь заданий по сервисам: база открывается только на чтение
            try:
                con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=1)
                for kind, status, n in con.execute("SELECT kind, status, COUNT(*) FROM jobs GROUP BY kind, status"):
                    qrows.append({"t": t, "step": STEP["n"], "kind": kind, "status": status, "n": n})
                con.close()
            except sqlite3.Error:
                pass
        tick += 1


# ---------------- одна консультация ----------------
def poller(cl, code, me, stop):
    """Длинный опрос сигнализации, как в браузере. Для своих меток lt_sent от другой стороны пишет задержку доставки."""
    last = 0
    while not stop.is_set():
        s, out = cl.call("GET", "/rooms/%s/events?after=%d&wait=%d" % (code, last, POLL_WAIT_S), timeout=POLL_WAIT_S + 30)
        if s != 200 or not isinstance(out, dict):
            time.sleep(1)
            continue
        now = time.time()
        for e in out.get("events") or []:
            last = max(last, e["id"])
            d = e.get("data") if isinstance(e.get("data"), dict) else {}
            if e.get("from") != me and "lt_sent" in d:
                with LOCK:
                    SIGNALS.append((STEP["n"], e["type"], round((now - d["lt_sent"]) * 1000, 1)))
        last = max(last, out.get("last") or last)


def consult(i, doc, audio, frames, duration, info):
    ev = lambda cl, typ, data: cl.call("POST", "/rooms/%s/events" % code, {"type": typ, "data": data})  # noqa: E731
    s, ap = doc.call("POST", "/appointments", {"full_name": "Нагрузочный Пациент %d-%d" % (STEP["n"], i)})
    if s != 201:
        info["error"] = "appointments %s" % s
        return
    s, rm = doc.call("POST", "/rooms", {"appointment_id": ap["id"]})
    if s != 201:
        info["error"] = "rooms %s %s" % (s, rm)
        return
    code = rm["room"]
    dcl, pcl = doc.with_token(rm["token"]), Client(BASE)
    pcl.call("GET", "/rooms/%s/info" % code)
    s, pj = pcl.call("POST", "/rooms/%s/join" % code, {"consent": True})
    if s != 200:
        info["error"] = "join %s" % s
        return
    pcl.tok = pj["token"]
    info.update(appt=ap["id"], code=code)
    stop = threading.Event()
    pollers = [threading.Thread(target=poller, args=(c, code, me, stop), daemon=True) for c, me in ((dcl, "doctor"), (pcl, "patient"))]
    for t in pollers:
        t.start()
    for c in (dcl, pcl):
        c.call("GET", "/rooms/%s/ice-config" % code)
    ev(pcl, "control", {"action": "face_consent", "granted": True})
    ev(dcl, "control", {"action": "speech_request"})
    ev(pcl, "control", {"action": "speech_consent", "granted": True})
    ev(dcl, "offer", {"sdp": "v=0 нагрузочный тест", "lt_sent": time.time()})
    ev(pcl, "answer", {"sdp": "v=0 нагрузочный тест", "lt_sent": time.time()})
    for k in range(3):
        ev(dcl, "ice", {"candidate": "candidate:%d" % k, "lt_sent": time.time()})
        ev(pcl, "ice", {"candidate": "candidate:%d" % k, "lt_sent": time.time()})
    dcl.call("POST", "/rooms/%s/protocol" % code, {"location": {"address": "г. Москва, ул. Тестовая, д. 1", "phone": "+7 900 000-00-00"}})
    ev(dcl, "control", {"action": "start"})

    t_start, t_end = time.time(), time.time() + duration
    sides = [(dcl, "doctor"), (pcl, "patient")]
    seq = {"doctor": 0, "patient": 0}

    def speak(cl, role):
        chunks = audio[role]
        while not stop.is_set() and time.time() < t_end:
            n = seq[role]
            path, dur = chunks[(n + i) % len(chunks)]
            key = "lt-%s-%s-%d" % (code, role[0], n)
            sent = time.time()
            s, _ = cl.call("POST", "/rooms/%s/audio" % code, raw=Path(path).read_bytes(),
                           headers={"Content-Type": "audio/webm", "X-Chunk-Key": key, "X-Seq": str(n), "X-Duration-Ms": str(int(dur * 1000))})
            with LOCK:
                SENDS.append((STEP["n"], code, key, role, round(sent, 3), s, round(dur, 2)))
            seq[role] += 1
            stop.wait(max(0.0, CHUNK_S - (time.time() - sent)))

    talkers = [threading.Thread(target=speak, args=sd, daemon=True) for sd in sides]
    for t in talkers:
        t.start()

    analyzed = False
    k = 0
    while time.time() < t_end:
        stop.wait(min(DOCTOR_EVERY_S, max(0.1, t_end - time.time())))
        if time.time() >= t_end:
            break
        k += 1
        ev(dcl, "chat", {"text": "Сообщение врача %d" % k, "lt_sent": time.time()})
        ev(dcl, "ehr", {"card": {"full_name": "Нагрузочный Пациент", "complaints": "асимметрия лица, %d" % k}})
        dcl.call("POST", "/rooms/%s/protocol" % code, {"notes": "Заметка врача %d" % k})
        if not analyzed and time.time() - t_start > duration / 2:
            analyzed = True
            t0 = time.time()
            s, j = pcl.call("POST", "/rooms/%s/analyze" % code, {"kind": "symmetry", "key": "lt-sym-%s" % code,
                                                                 "input": {"frames": frames["frames"], "tolerance": frames["tol"]}})
            if s == 202 and isinstance(j, dict):
                for _ in range(120):
                    s2, jd = pcl.call("GET", "/rooms/%s/jobs/%d" % (code, j["id"]))
                    if isinstance(jd, dict) and jd.get("status") in ("done", "error"):
                        info["analyze_s"] = round(time.time() - t0, 2)
                        info["analyze_status"] = jd.get("status")
                        break
                    time.sleep(1)
    for t in talkers:
        t.join(timeout=CHUNK_S + 30)
    dcl.call("POST", "/rooms/%s/protocol" % code, {"conclusion": {"complaints": "асимметрия лица", "diagnosis": "Нейропатия лицевого нерва",
                                                                    "icd": "G51.0", "recommendations": "наблюдение, упражнения"}})
    ev(dcl, "control", {"action": "end"})
    info["ended"] = round(time.time(), 3)
    dcl.call("POST", "/rooms/%s/render-conclusion" % code, {"center": "Медицинский центр", "doctor": "Врач", "patient": "Нагрузочный Пациент",
                                                            "date": "2026-11-07", "conclusion": {"diagnosis": "Нейропатия лицевого нерва", "icd": "G51.0"}})
    stop.set()
    info["chunks"] = dict(seq)


def pending_work(db, appts):
    """Сколько кусков речи и обезличиваний ещё не готово по приёмам ступени."""
    if not appts:
        return 0
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
    q = "SELECT COUNT(*) FROM jobs WHERE appt_id IN (%s) AND status='queued'" % ",".join("?" * len(appts))
    n = con.execute(q, appts).fetchone()[0]
    con.close()
    return n


def run_step(n, docs, audio, frames, duration, drain, db):
    STEP["n"] = n
    infos = [dict(pair=i) for i in range(n)]
    stagger = min(20.0, duration / 4)
    ths = []
    t0 = time.time()
    MARKS.append({"step": n, "event": "start", "t": t0})
    for i in range(n):
        th = threading.Thread(target=lambda i=i: (time.sleep(random.uniform(0, stagger)), consult(i, docs[i % len(docs)], audio, frames, duration, infos[i])),
                              daemon=True)
        th.start()
        ths.append(th)
    for th in ths:
        th.join()
    t_load_end = time.time()
    appts = [x["appt"] for x in infos if "appt" in x]
    while time.time() - t_load_end < drain and pending_work(db, appts):
        time.sleep(2)
    t_end = time.time()
    left = pending_work(db, appts)
    MARKS.append({"step": n, "event": "end", "t": t_end})
    return {"step": n, "pairs": n, "t_start": t0, "t_load_end": t_load_end, "t_end": t_end, "pending_after_drain": left,
            "infos": infos, "errors": [x["error"] for x in infos if "error" in x]}


def export_jobs(db, path):
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT id, appt_id, kind, key, status, attempts, created, updated, result, error FROM jobs")]
    con.close()
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "appt_id", "kind", "key", "status", "attempts", "created", "updated", "result", "error"])
        w.writeheader()
        w.writerows(rows)


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--steps", default="1,2,4,8,16", help="числа пар по ступеням")
    ap.add_argument("--duration", type=float, default=120, help="длительность приёма, с (по умолчанию 120)")
    ap.add_argument("--drain", type=float, default=60, help="сколько ждать разбора очереди после ступени, с")
    ap.add_argument("--whisper-threads", default="", help="WHISPER_THREADS для speech (пусто — по числу ядер)")
    ap.add_argument("--speech-workers", default="", help="SPEECH_WORKERS: сколько пар расшифровывается одновременно")
    ap.add_argument("--speech-venv", default="", help="окружение speech: .venv (по умолчанию) или .venv314t (Python 3.14t)")
    ap.add_argument("--nogil", action="store_true", help="для .venv314t: PYTHON_GIL=0, чтобы PyAV не включал GIL")
    ap.add_argument("--out", default=str(LT / "results"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    random.seed(7)
    idx = json.loads((LT / "audio" / "index.json").read_text(encoding="utf-8"))
    audio = {r: [(str(LT / "audio" / r / Path(p).name), d) for p, d in idx[r]] for r in ("doctor", "patient")}
    import test_vision  # кадры лица с позой в допуске, как в tests/test_failover.py
    frames = test_vision.build_cases(5)["sym"][0]

    data_dir = Path(tempfile.mkdtemp(prefix="mars_load_"))
    env = {"WHISPER_THREADS": a.whisper_threads} if a.whisper_threads else {}
    if a.speech_workers:
        env["SPEECH_WORKERS"] = a.speech_workers
    if a.speech_venv:
        env["SPEECH_VENV"] = a.speech_venv
    if a.nogil:
        env["SPEECH_NOGIL"] = "1"
    stack, boot_s = start_stack(data_dir, env)
    stop = threading.Event()
    res_rows, q_rows = [], []
    smp = threading.Thread(target=sampler, args=(stop, stack, data_dir, res_rows, q_rows), daemon=True)
    smp.start()
    steps = []
    try:
        admin = Client(BASE)
        admin.call("POST", "/auth/login", {"login": "admin", "password": ADMIN_PW})
        docs = [admin]
        for k in range(1, MAX_DOCTORS):
            admin.call("POST", "/doctors", {"login": "load_doc%d" % k, "name": "Врач нагрузки %d" % k, "password": "load-pass-%04d" % k})
            d = Client(BASE)
            s, _ = d.call("POST", "/auth/login", {"login": "load_doc%d" % k, "password": "load-pass-%04d" % k})
            if s == 200:
                docs.append(d)
        print("стенд поднят за %.0f с, врачей: %d, DATA_DIR %s" % (boot_s, len(docs), data_dir), flush=True)
        # прогрев: короткий приём, чтобы модели Whisper и Ollama были загружены до замеров
        STEP["n"] = 0
        warm = {}
        consult(0, docs[0], audio, frames, 25, warm)
        db = data_dir / "mars.sqlite3"
        t = time.time()
        while time.time() - t < 240 and pending_work(db, [warm.get("appt")] if warm.get("appt") else []):
            time.sleep(2)
        print("прогрев готов за %.0f с" % (time.time() - t), flush=True)
        for n in [int(x) for x in a.steps.split(",")]:
            r = run_step(n, docs, audio, frames, a.duration, a.drain, db)
            steps.append(r)
            print("ступень %2d пар: %.0f с, ошибок сценария %d, не разобрано в очереди %d" % (
                n, r["t_end"] - r["t_start"], len(r["errors"]), r["pending_after_drain"]), flush=True)
    finally:
        stop.set()
        time.sleep(2.5)
        db = data_dir / "mars.sqlite3"
        if db.exists():
            export_jobs(db, out / "jobs.csv")
        write_csv(out / "requests.csv", ["step", "endpoint", "status", "ms", "t", "degraded"], REQS)
        write_csv(out / "signaling.csv", ["step", "type", "delivery_ms"], SIGNALS)
        write_csv(out / "audio_sends.csv", ["step", "room", "key", "role", "sent", "status", "dur_s"], SENDS)
        keys = sorted({k for r in res_rows for k in r})
        with open(out / "resources.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(res_rows)
        write_csv(out / "queue.csv", ["t", "step", "kind", "status", "n"], [(r["t"], r["step"], r["kind"], r["status"], r["n"]) for r in q_rows])
        meta = {"duration_s": a.duration, "drain_s": a.drain, "boot_s": boot_s, "doctors": MAX_DOCTORS, "data_dir": str(data_dir),
                "cpu_count": psutil.cpu_count(), "cpu_count_physical": psutil.cpu_count(logical=False),
                "ram_gb": round(psutil.virtual_memory().total / 2 ** 30, 1), "whisper_threads": a.whisper_threads or "auto",
                "speech_workers": a.speech_workers or "2 (по умолчанию)", "speech_venv": a.speech_venv or ".venv", "nogil": a.nogil, "steps": steps}
        (out / "steps.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        stack.terminate()
        try:
            stack.wait(15)
        except subprocess.TimeoutExpired:
            stack.kill()
        print("результаты: %s" % out, flush=True)


if __name__ == "__main__":
    main()
