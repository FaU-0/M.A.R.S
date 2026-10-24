# -*- coding: utf-8 -*-
"""Этап 2: сервис vision.

1. Совпадение с браузером: одни и те же точки считаются кодом браузера (node) и сервисом.
   Допуск 1e-9 (задан заранее): формулы перенесены один к одному, расхождение только в округлении.
2. Через gateway: задание в очереди, результат; при остановленном vision задание ждёт и выполняется после.
3. Без vision (одиночный режим): gateway сразу отвечает 503, браузер считает сам.

    python tests/test_vision.py
"""
import http.cookiejar
import json
import math
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from vision import metrics  # noqa: E402

TOL = 1e-9
FAILS = []
BS_NAMES = ["mouthSmileRight", "mouthSmileLeft", "browOuterUpRight", "browOuterUpLeft", "eyeBlinkRight", "eyeBlinkLeft",
            "cheekPuff", "jawOpen", "mouthPressRight", "mouthPressLeft"]


def check(c, m):
    print(("OK    " if c else "FAIL  ") + m)
    if not c:
        FAILS.append(m)


# --- синтетические кадры ---
def matrix(yaw, pitch, roll, s=1.0):
    """Матрица поворота в порядке браузера (по столбцам), с масштабом."""
    y, p, r = (math.radians(a) for a in (yaw, pitch, roll))
    Ry = [[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]]
    Rx = [[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]]
    Rz = [[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0], [0, 0, 1]]

    def mul(A, B):
        return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

    R = mul(mul(Ry, Rx), Rz)
    cols = []
    for j in range(3):
        cols += [R[0][j] * s, R[1][j] * s, R[2][j] * s, 0.0]
    return cols + [1.0, 2.0, -40.0, 1.0]  # последний столбец: перенос


def base_face(rng):
    """478 точек: лицо примерно в центре кадра, правый глаз слева на изображении."""
    pts = [[0.5 + rng.uniform(-0.15, 0.15), 0.5 + rng.uniform(-0.2, 0.2), rng.uniform(-0.05, 0.05)] for _ in range(478)]
    fixed = {468: (0.42, 0.42), 473: (0.58, 0.42), 133: (0.46, 0.42), 362: (0.54, 0.42), 33: (0.38, 0.42), 263: (0.62, 0.42),
             61: (0.44, 0.62), 291: (0.56, 0.62), 105: (0.41, 0.36), 334: (0.59, 0.36), 1: (0.5, 0.52), 152: (0.5, 0.75),
             234: (0.32, 0.5), 454: (0.68, 0.5), 10: (0.5, 0.22), 6: (0.5, 0.42), 168: (0.5, 0.39)}
    for i, (x, y) in fixed.items():
        pts[i] = [x, y, rng.uniform(-0.03, 0.03)]
    for c, base in ((468, 469), (473, 474)):  # радужка вокруг центра зрачка
        for k in range(4):
            a = k * math.pi / 2
            pts[base + k] = [pts[c][0] + 0.012 * math.cos(a), pts[c][1] + 0.012 * math.sin(a), pts[c][2]]
    return pts


def frame(rng, face, angles, jitter=0.002, multi=False):
    lm = [[p[0] + rng.gauss(0, jitter), p[1] + rng.gauss(0, jitter), p[2] + rng.gauss(0, jitter)] for p in face]
    return {"lm": lm, "mat": matrix(*angles, s=rng.uniform(0.9, 1.1)), "vw": 1280, "vh": 720, "multi": multi,
            "bs": {n: rng.random() for n in BS_NAMES}}


def angles(rng, spread):
    return (rng.uniform(-spread, spread), rng.uniform(-spread, spread), rng.uniform(-spread, spread))


def build_cases(seed=7):
    rng = random.Random(seed)
    cal_card = {"source": "card", "label": "по банковской карте 85,6 мм", "approx": False, "mm_per_px": 0.21, "ipd_mm": 62.0, "ts": 1}
    cases = {"sym": [], "mob": [], "cmp": [], "iris": [], "card": []}
    for i in range(10):
        face = base_face(rng)
        tol = rng.choice([3.0, 5.0, 8.0])
        fr = [frame(rng, face, angles(rng, tol * 1.12)) for _ in range(rng.randint(20, 60))]
        cases["sym"].append({"frames": fr, "tol": tol, "cal": cal_card if i % 2 else None})
    for i, t in enumerate(metrics.MOB_ORDER * 2):
        face = base_face(rng)
        fr = [frame(rng, face, angles(rng, 14)) for _ in range(rng.randint(15, 100))]
        cases["mob"].append({"task": t, "frames": fr, "tol": 5.0})
    poses = list(metrics.POSE_YAW)
    for i in range(15):
        face = base_face(rng)
        pose = poses[i % len(poses)]
        tgt = metrics.POSE_YAW[pose]
        tol = 5.0
        A = frame(rng, face, (tgt + rng.uniform(-5.6, 5.6), rng.uniform(-5.3, 5.3), rng.uniform(-5.3, 5.3)), multi=i % 4 == 0)
        B = frame(rng, face, (tgt + rng.uniform(-5.6, 5.6), rng.uniform(-5.3, 5.3), rng.uniform(-5.3, 5.3)), jitter=0.006)
        cases["cmp"].append({"A": A, "B": B, "pose": pose, "tol": tol, "cal": cal_card if i % 3 == 0 else None})
    for i in range(4):
        face = base_face(rng)
        cases["iris"].append([frame(rng, face, angles(rng, 3)) for _ in range(rng.randint(12, 30))])
    for i in range(6):
        x, y, w, h = rng.uniform(100, 400), rng.uniform(100, 300), rng.uniform(150, 400), rng.uniform(90, 250)
        pts = [{"x": x, "y": y}, {"x": x + w, "y": y + rng.uniform(-5, 5)}, {"x": x + w, "y": y + h}, {"x": x + rng.uniform(-5, 5), "y": y + h}]
        cases["card"].append({"pts": pts, "ipd": rng.uniform(80, 220)})
    return cases


def diff(a, b, path=""):
    """Список расхождений двух результатов (числа с допуском TOL; ts и знак yaw не сравниваются)."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a) | set(b):
            if k in ("ts", "source"):
                continue
            if k == "yaw" and isinstance(a.get(k), (int, float)) and isinstance(b.get(k), (int, float)):
                if abs(abs(a[k]) - abs(b[k])) > TOL:
                    out.append(path + ".yaw")
                continue
            if k not in a or k not in b:
                out.append("%s.%s: нет в одном из ответов" % (path, k))
                continue
            out += diff(a[k], b[k], path + "." + str(k))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return ["%s: длина %d != %d" % (path, len(a), len(b))]
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff(x, y, "%s[%d]" % (path, i))
    elif isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        if a != b:
            out.append("%s: %r != %r" % (path, a, b))
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if abs(a - b) > TOL * max(1.0, abs(a)):
            out.append("%s: %r != %r" % (path, a, b))
    elif a != b:
        out.append("%s: %r != %r" % (path, a, b))
    return out


def py_side(cases):
    def safe(fn):
        try:
            return fn()
        except metrics.MetricsError:
            return None

    return {
        "sym": [safe(lambda c=c: metrics.symmetry(c["frames"], c["tol"], c["cal"])) for c in cases["sym"]],
        "mob": [safe(lambda c=c: metrics.mob_task(c["task"], c["frames"], max(c["tol"] * 2, 10))) for c in cases["mob"]],
        "cmp": [safe(lambda c=c: metrics.compare(c["A"], c["B"], c["pose"], c["tol"], c["cal"])) for c in cases["cmp"]],
        "iris": [safe(lambda c=c: metrics.calibrate_iris(c)) for c in cases["iris"]],
        "card": [safe(lambda c=c: metrics.calibrate_card(c["pts"], c["ipd"])) for c in cases["card"]],
    }


def part_parity():
    print("--- совпадение с расчётом браузера ---")
    cases = build_cases()
    p = subprocess.run(["node", str(ROOT / "tests" / "js_face_harness.js")], input=json.dumps(cases), capture_output=True, text=True)
    if p.returncode != 0:
        check(False, "node: " + p.stderr[-500:])
        return
    js, py = json.loads(p.stdout), py_side(cases)
    names = {"sym": "симметрия", "mob": "подвижность", "cmp": "сравнение до и после", "iris": "калибровка по радужке", "card": "калибровка по карте"}
    for k, label in names.items():
        bad = []
        for i, (a, b) in enumerate(zip(js[k], py[k])):
            d = diff(a, b)
            if d:
                bad.append("случай %d: %s" % (i, "; ".join(d[:3])))
        computed = sum(1 for x in js[k] if x is not None)
        check(not bad, "%s: %d случаев (%d с результатом), совпадают%s" % (label, len(js[k]), computed, "" if not bad else ": " + bad[0]))
    # поза: часть кадров должна отбрасываться, иначе проверка допуска ничего не проверяет
    some_none = any(x is None for x in js["mob"]) or any(x is None for x in js["sym"])
    some_bad_pose = any(c and not c["pose_ok"] for c in js["cmp"]) and any(c and c["pose_ok"] for c in js["cmp"])
    check(some_bad_pose, "в наборе есть сравнения и в допуске позы, и вне его")
    print("  (кадров с недостаточной выборкой: %s)" % ("есть" if some_none else "нет"))


# --- через gateway ---
class Gw:
    def __init__(self, base):
        self.base = base
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.tok = None

    def call(self, method, path, body=None, timeout=10):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.base, path), method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        if self.tok and path.startswith("/rooms/"):
            req.add_header("Authorization", "Bearer " + self.tok)
        try:
            with self.op.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "null")

    def wait_up(self):
        for _ in range(60):
            try:
                self.call("GET", "/health", timeout=2)
                return True
            except Exception:
                time.sleep(0.5)
        return False

    def room(self):
        self.call("POST", "/auth/login", {"login": "admin", "password": "test-pass-1234"})
        s, d = self.call("POST", "/rooms", {})
        self.code, self.tok = d["room"], d["token"]
        return s


def start(base, without):
    tmp = tempfile.mkdtemp(prefix="mars_v_")
    env = dict(os.environ, DATA_DIR=tmp, PORT=str(base), ADMIN_PASSWORD="test-pass-1234")
    for i, n in enumerate(("vision", "reports", "speech", "assistant"), 1):
        env["MARS_%s_PORT" % n.upper()] = str(base + i)
    return subprocess.Popen([sys.executable, str(ROOT / "run_all.py"), "--without", without], env=env, cwd=str(ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(p):
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


def wait_job(g, jid, secs=10):
    t = time.time()
    while time.time() - t < secs:
        s, d = g.call("GET", "/rooms/%s/jobs/%d" % (g.code, jid))
        if d and d.get("status") in ("done", "error"):
            return d
        time.sleep(0.3)
    return d


def part_gateway():
    print("--- через gateway и очередь заданий ---")
    cases = build_cases(11)
    sym = next(c for c in cases["sym"] if py_side({"sym": [c], "mob": [], "cmp": [], "iris": [], "card": []})["sym"][0])
    want = metrics.symmetry(sym["frames"], sym["tol"], sym["cal"])
    inp = {"frames": sym["frames"], "tolerance": sym["tol"], "calibration": sym["cal"]}
    base = 18200
    p = start(base, "speech,assistant,reports")
    try:
        g = Gw(base)
        check(g.wait_up(), "gateway запущен")
        time.sleep(1.5)
        check(g.room() == 201, "комната создана")
        s, d = g.call("POST", "/rooms/%s/analyze" % g.code, {"kind": "symmetry", "key": "k-sym-0001", "input": inp})
        check(s == 202 and d.get("status") == "queued", "задание принято: 202")
        jid = d["id"]
        s, d2 = g.call("POST", "/rooms/%s/analyze" % g.code, {"kind": "symmetry", "key": "k-sym-0001", "input": inp})
        check(d2.get("id") == jid, "повтор с тем же ключом не создаёт дубль")
        d = wait_job(g, jid)
        check(d.get("status") == "done" and not diff(d.get("result"), want), "vision посчитал симметрию, результат как у метрик")
        s, d = g.call("POST", "/rooms/%s/analyze" % g.code, {"kind": "symmetry", "key": "k-sym-few1", "input": dict(inp, frames=inp["frames"][:3])})
        d = wait_job(g, d["id"])
        check(d.get("status") == "error" and d["error"].get("error") == "not_enough_frames", "мало кадров: задание с понятной ошибкой")
        s, _ = g.call("POST", "/rooms/%s/analyze" % g.code, {"kind": "nope", "key": "k-bad-0001", "input": {}})
        check(s == 400, "неизвестный вид анализа: 400")
        c = cases["cmp"][0]
        s, d = g.call("POST", "/rooms/%s/analyze" % g.code, {"kind": "compare", "key": "k-cmp-0001", "input": {
            "before": {"id": 1, "ts": 1, "frame": c["A"]}, "after": {"id": 2, "ts": 2, "frame": c["B"]}, "pose": c["pose"], "tolerance": c["tol"]}})
        d = wait_job(g, d["id"])
        check(d.get("status") == "done" and len(d["result"]["rows"]) == 8, "сравнение до и после через vision")

        pids = subprocess.run(["pgrep", "-f", "vision.main"], capture_output=True, text=True).stdout.split()
        for pid in pids:
            os.kill(int(pid), signal.SIGSTOP)
        t = time.time()
        s, d = g.call("POST", "/rooms/%s/analyze" % g.code, {"kind": "symmetry", "key": "k-sym-down1", "input": inp})
        check(s == 202 and time.time() - t < 2, "vision остановлен: задание всё равно принято сразу")
        jid = d["id"]
        time.sleep(12)
        s, d = g.call("GET", "/rooms/%s/jobs/%d" % (g.code, jid))
        check(d.get("status") == "queued" and d.get("attempts", 0) >= 1, "vision остановлен: задание ждёт в очереди (попыток: %s)" % d.get("attempts"))
        s, _ = g.call("GET", "/health")
        check(s == 200, "vision остановлен: gateway отвечает")
        s, st = g.call("GET", "/status")
        check(st["services"]["vision"]["up"] is False, "vision остановлен: /status показывает это")
        for pid in pids:
            os.kill(int(pid), signal.SIGCONT)
        d = wait_job(g, jid, 75)
        check(d.get("status") == "done", "vision возобновлён: задание из очереди выполнено само")
    finally:
        stop(p)

    print("--- одиночный режим без vision ---")
    base = 18300
    p = start(base, "vision,speech,assistant,reports")
    try:
        g = Gw(base)
        g.wait_up()
        time.sleep(1)
        g.room()
        s, d = g.call("POST", "/rooms/%s/analyze" % g.code, {"kind": "symmetry", "key": "k-sym-solo1", "input": inp})
        check(s == 503 and d.get("degraded") is True and d.get("service") == "vision", "vision не запущен: 503 degraded, браузер считает сам")
    finally:
        stop(p)


def main():
    part_parity()
    if "--parity" not in sys.argv:
        part_gateway()
    print("\nИтог: %s" % ("всё прошло" if not FAILS else "ошибок: %d" % len(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
