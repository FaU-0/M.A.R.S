# -*- coding: utf-8 -*-
"""Метрики лица по точкам MediaPipe. Перенос формул из браузера (gateway/src/d_face1.js, d_face3.js)
один к одному: одинаковые точки дают одинаковые числа (проверяет tests/test_vision.py).

Кадр (frame) - словарь:
    lm   {индекс: [x, y, z]} или список точек [[x, y, z], ...], координаты нормированы 0..1 как в MediaPipe
    mat  матрица преобразования лица, 16 чисел (facialTransformationMatrixes)
    bs   {имя blendshape: коэффициент 0..1}
    vw, vh  размер кадра в пикселях
    multi   на кадре было больше одного лица
"""
import math
import time

# Индексы точек MediaPipe Face Mesh (как LM в d_face1.js)
RC, LC, REI, LEI, REO, LEO, RM, LMO, RB, LB, NOSE, CHIN, TOP = 468, 473, 133, 362, 33, 263, 61, 291, 105, 334, 1, 152, 10
RIRIS, LIRIS = (469, 470, 471, 472), (474, 475, 476, 477)

SYM_IDX = (468, 473, 133, 362, 33, 263, 61, 291, 105, 334, 1, 152, 234, 454)
SYM_NEED = 12
PAIRS = [
    ("eo", "Внешние уголки глаз: расстояние до средней линии", "eoR", "eoL"),
    ("ei", "Внутренние уголки глаз: расстояние до средней линии", "eiR", "eiL"),
    ("mouth", "Уголки рта: расстояние до средней линии", "mR", "mL"),
    ("brow", "Высота бровей над линией глаз", "bR", "bL"),
    ("mcor", "Высота уголков рта относительно линии глаз", "cR", "cL"),
    ("half", "Ширина половин лица", "wR", "wL"),
]
SINGLES = [
    ("tilt", "Наклон линии глаз", "deg"),
    ("nose", "Смещение кончика носа от средней линии", "ipd"),
    ("chin", "Смещение подбородка от средней линии", "ipd"),
]

MOB_TASKS = {
    "smile": {"label": "Улыбка", "r": "mouthSmileRight", "l": "mouthSmileLeft"},
    "brow": {"label": "Подъём бровей", "r": "browOuterUpRight", "l": "browOuterUpLeft"},
    "eye": {"label": "Закрытие глаз", "r": "eyeBlinkRight", "l": "eyeBlinkLeft"},
    "cheek": {"label": "Надувание щёк", "single": "cheekPuff"},
    "mouth": {"label": "Открытие рта", "single": "jawOpen"},
    "lips": {"label": "Поджатие губ", "r": "mouthPressRight", "l": "mouthPressLeft"},
}
MOB_ORDER = ["smile", "brow", "eye", "cheek", "mouth", "lips"]
MOB_MIN_FRAMES = 10

POSE_YAW = {"frontal": 0, "left45": 45, "right45": -45, "left90": 90, "right90": -90}
CMP_RIGID = (33, 133, 362, 263, 6, 1, 168, 10)
CMP_POINTS = [(105, "Правая бровь"), (334, "Левая бровь"), (33, "Правый внешний угол глаза"), (263, "Левый внешний угол глаза"),
              (1, "Кончик носа"), (61, "Правый угол рта"), (291, "Левый угол рта"), (152, "Подбородок")]

CARD_W, CARD_H, IRIS_MM = 85.6, 53.98, 11.7


class MetricsError(ValueError):
    """Данных недостаточно для расчёта. code уходит в поле error ответа."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def deg(r):
    return r * 180 / math.pi


def clamp(v, a, b):
    return max(a, min(b, v))


def median(arr):
    if not arr:
        return None
    s = sorted(arr)
    m = len(s) >> 1
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def now():
    return int(time.time())


# --- кадр ---
class Frame:
    def __init__(self, d):
        if not isinstance(d, dict):
            raise MetricsError("bad_frame", "Кадр должен быть объектом")
        raw = d.get("lm") or {}
        if isinstance(raw, list):
            raw = dict(enumerate(raw))
        self.lm = {}
        for k, p in raw.items():
            try:
                self.lm[int(k)] = (float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0)
            except (TypeError, ValueError, IndexError):
                raise MetricsError("bad_frame", "Некорректная точка %s" % k)
        mat = d.get("mat")
        self.mat = [float(x) for x in mat] if isinstance(mat, list) and len(mat) == 16 else None
        bs = d.get("bs") or {}
        self.bs = {str(k): float(v) for k, v in bs.items()} if isinstance(bs, dict) else {}
        try:
            self.vw, self.vh = float(d.get("vw") or 0), float(d.get("vh") or 0)
        except (TypeError, ValueError):
            raise MetricsError("bad_frame", "Некорректный размер кадра")
        self.multi = bool(d.get("multi"))

    def has(self, idx):
        return all(i in self.lm for i in idx)

    def px(self, i):
        p = self.lm[i]
        return p[0] * self.vw, p[1] * self.vh


def frames(lst, limit=600):
    if not isinstance(lst, list) or not lst:
        raise MetricsError("no_frames", "Нет кадров")
    if len(lst) > limit:
        raise MetricsError("too_many_frames", "Слишком много кадров")
    return [Frame(x) for x in lst]


# --- поза (FaceMesh.Pose) ---
def rot(mat):
    sx = math.hypot(mat[0], mat[1], mat[2]) or 1
    sy = math.hypot(mat[4], mat[5], mat[6]) or 1
    sz = math.hypot(mat[8], mat[9], mat[10]) or 1
    return [[mat[0] / sx, mat[4] / sy, mat[8] / sz], [mat[1] / sx, mat[5] / sy, mat[9] / sz], [mat[2] / sx, mat[6] / sy, mat[10] / sz]]


def euler(f):
    """Углы позы. Знак yaw уточняется по положению носа на этом кадре
    (в браузере то же самое накапливается по многим кадрам). Допуск позы от знака не зависит."""
    if f.mat is None:
        return None
    R = rot(f.mat)
    yaw = deg(math.atan2(R[0][2], R[2][2]))
    pitch = deg(math.asin(clamp(-R[1][2], -1, 1)))
    roll = deg(math.atan2(R[1][0], R[1][1]))
    if f.has((LEO, REO, NOSE)):
        wid = f.lm[LEO][0] - f.lm[REO][0]
        if abs(wid) > 1e-3:
            yl = (f.lm[NOSE][0] - (f.lm[REO][0] + f.lm[LEO][0]) / 2) / wid
            if abs(yl) > 0.12 and abs(yaw) > 10 and math.copysign(1, yl) != math.copysign(1, yaw):
                yaw = -yaw
    return {"yaw": yaw, "pitch": pitch, "roll": roll}


def in_tol(e, tol):
    return bool(e) and abs(e["yaw"]) <= tol and abs(e["pitch"]) <= tol and abs(e["roll"]) <= tol


# --- симметрия (FaceMesh.Symmetry) ---
def derot(f):
    R = rot(f.mat)
    A = [[R[0][0], -R[0][1], -R[0][2]], [-R[1][0], R[1][1], R[1][2]], [-R[2][0], R[2][1], R[2][2]]]
    pts = [(i, f.lm[i][0] * f.vw, f.lm[i][1] * f.vh, f.lm[i][2] * f.vw) for i in SYM_IDX]
    n = len(pts)
    cx, cy, cz = 0.0, 0.0, 0.0
    for _, x, y, z in pts:  # тот же порядок сложения, что и в браузере
        cx += x / n
        cy += y / n
        cz += z / n
    P = {}
    for i, x, y, z in pts:
        dx, dy, dz = x - cx, y - cy, z - cz
        P[i] = (A[0][0] * dx + A[1][0] * dy + A[2][0] * dz, A[0][1] * dx + A[1][1] * dy + A[2][1] * dz)
    return P


def sym_one(f):
    """Метрики одного кадра в долях межзрачкового расстояния (МЗР). None, если кадр не годится."""
    if f.mat is None or not f.has(SYM_IDX):
        return None
    P = derot(f)
    R, L = P[468], P[473]
    ex, ey = L[0] - R[0], L[1] - R[1]
    ipd = math.hypot(ex, ey)
    if ipd < 5:
        return None
    ux, uy = ex / ipd, ey / ipd
    nx, ny = -uy, ux
    Mx, My = (R[0] + L[0]) / 2, (R[1] + L[1]) / 2

    def along(i):
        return ((P[i][0] - Mx) * ux + (P[i][1] - My) * uy) / ipd

    def down(i):
        return ((P[i][0] - Mx) * nx + (P[i][1] - My) * ny) / ipd

    return {"ipd": ipd, "tilt": deg(math.atan2(ey, ex)), "nose": along(1), "chin": along(152),
            "eoR": -along(33), "eoL": along(263), "eiR": -along(133), "eiL": along(362),
            "mR": -along(61), "mL": along(291), "bR": -down(105), "bL": -down(334),
            "cR": -down(61), "cL": -down(291), "wR": -along(234), "wL": along(454)}


def sym_build(samples, cal, tol):
    def med(k):
        return median([s[k] for s in samples])

    k = cal.get("ipd_mm") if isinstance(cal, dict) else None
    rows = []
    for id_, label, a, b in PAIRS:
        r, l = med(a), med(b)
        o = {"id": id_, "label": label, "right": r, "left": l, "diff": r - l}
        if k:
            o["right_mm"], o["left_mm"], o["diff_mm"] = r * k, l * k, (r - l) * k
        rows.append(o)
    singles = []
    for id_, label, unit in SINGLES:
        s = {"id": id_, "label": label, "value": med(id_), "unit": unit}
        if k and unit == "ipd":
            s["value_mm"] = s["value"] * k
        singles.append(s)
    out = {"kind": "symmetry", "pose_ok": True, "samples": len(samples), "ipd_px": med("ipd"), "tolerance": tol,
           "rows": rows, "singles": singles, "ts": now()}
    if k:
        out["calibration"] = cal
    return out


def symmetry(frame_list, tol=5.0, cal=None):
    """Симметрия по кадрам удержания позы. Кадры вне допуска отбрасываются."""
    S = []
    for f in frames(frame_list):
        if not in_tol(euler(f), tol):
            continue
        v = sym_one(f)
        if v:
            S.append(v)
    if len(S) < SYM_NEED:
        raise MetricsError("not_enough_frames", "Недостаточно кадров в допустимой позе (%d из %d)" % (len(S), SYM_NEED))
    return sym_build(S, cal, tol)


# --- подвижность (FaceMesh.Mobility) ---
def mob_task(task_id, frame_list, tol):
    """Максимум коэффициента blendshape по кадрам с позой в допуске."""
    t = MOB_TASKS.get(task_id)
    if t is None:
        raise MetricsError("bad_task", "Неизвестное задание: %s" % task_id)
    mr = ml = ms = 0.0
    n = 0
    for f in frames(frame_list):
        if not in_tol(euler(f), tol):
            continue
        n += 1
        if "single" in t:
            ms = max(ms, f.bs.get(t["single"], 0.0))
        else:
            mr = max(mr, f.bs.get(t["r"], 0.0))
            ml = max(ml, f.bs.get(t["l"], 0.0))
    if n < MOB_MIN_FRAMES:
        raise MetricsError("not_enough_frames", "Недостаточно кадров с лицом в допустимой позе: %s" % t["label"])
    o = {"id": task_id, "label": t["label"]}
    if "single" in t:
        o["single"] = ms
    else:
        o["right"], o["left"] = mr, ml
        o["ratio"] = min(mr, ml) / max(mr, ml) if max(mr, ml) > 0.05 else None
    return o


def mobility(tasks, tol=5.0):
    """tasks: [{id, frames}]. Допуск позы как в браузере: вдвое шире, но не меньше 10 градусов."""
    if not isinstance(tasks, list) or not tasks:
        raise MetricsError("no_tasks", "Нет заданий")
    mt = max(tol * 2, 10)
    out = [mob_task(str(t.get("id")), t.get("frames"), mt) for t in tasks if isinstance(t, dict)]
    return {"kind": "mobility", "pose_ok": True, "tolerance": mt, "tasks": out, "ts": now()}


# --- сравнение до и после (FaceMesh.Compare) ---
def procrustes(A, B):
    """Подобие «после» -> «до» по жёстким точкам (наименьшие квадраты, комплексная форма)."""
    pa = [A.px(i) for i in CMP_RIGID]
    pb = [B.px(i) for i in CMP_RIGID]
    n = len(pa)
    ca = (sum(p[0] for p in pa) / n, sum(p[1] for p in pa) / n)
    cb = (sum(p[0] for p in pb) / n, sum(p[1] for p in pb) / n)
    re = im = den = 0.0
    for i in range(n):
        ax, ay = pa[i][0] - ca[0], pa[i][1] - ca[1]
        bx, by = pb[i][0] - cb[0], pb[i][1] - cb[1]
        re += bx * ax + by * ay
        im += bx * ay - by * ax
        den += bx * bx + by * by
    if den == 0:
        raise MetricsError("degenerate", "Жёсткие точки совпадают, совмещение невозможно")
    c, d = re / den, im / den
    return {"c": c, "d": d, "tx": ca[0] - (c * cb[0] - d * cb[1]), "ty": ca[1] - (d * cb[0] + c * cb[1]),
            "scale": math.hypot(c, d), "angle": deg(math.atan2(d, c))}


def compare(before, after, pose="frontal", tol=5.0, cal=None):
    """before/after: кадры снимков. Возвращает то же, что считает браузер в Compare.calc."""
    A, B = Frame(before), Frame(after)
    if pose not in POSE_YAW:
        raise MetricsError("bad_pose", "Неизвестный ракурс")
    need = set(CMP_RIGID) | {i for i, _ in CMP_POINTS} | {TOP, CHIN} | ({RC, LC} if pose == "frontal" else set())
    if not A.has(need) or not B.has(need):
        raise MetricsError("face_not_found", "Лицо не найдено на " + ("обоих снимках" if not A.has(need) and not B.has(need)
                                                                      else ("снимке «до»" if not A.has(need) else "снимке «после»")))
    tgt = POSE_YAW[pose]
    mm = cal if pose == "frontal" and isinstance(cal, dict) and cal.get("ipd_mm") else None

    def chk(X):
        e = euler(X)
        if not e:
            return {"ok": False, "e": None}
        dy = abs(e["yaw"]) if pose == "frontal" else abs(abs(e["yaw"]) - abs(tgt))
        return {"ok": dy <= tol and abs(e["pitch"]) <= tol and abs(e["roll"]) <= tol, "e": e}

    ca, cb = chk(A), chk(B)
    pose_ok = ca["ok"] and cb["ok"] and A.mat is not None and B.mat is not None
    T = procrustes(A, B)

    def dist(X, i, j):
        (x1, y1), (x2, y2) = X.px(i), X.px(j)
        return math.hypot(x1 - x2, y1 - y2)

    norm_name = "ipd" if pose == "frontal" else "face_height"
    norm = dist(A, RC, LC) if norm_name == "ipd" else dist(A, TOP, CHIN)
    if norm <= 0:
        raise MetricsError("degenerate", "Нулевой масштаб нормировки")
    rows = []
    for i, label in CMP_POINTS:
        px, py = A.px(i)
        bx, by = B.px(i)
        qx, qy = T["c"] * bx - T["d"] * by + T["tx"], T["d"] * bx + T["c"] * by + T["ty"]
        dx, dy = (qx - px) / norm, (qy - py) / norm
        o = {"id": i, "label": label, "dx": dx, "dy": dy, "px": px, "py": py, "qx": qx, "qy": qy}
        if mm:
            o["dx_mm"], o["dy_mm"] = dx * mm["ipd_mm"], dy * mm["ipd_mm"]
        rows.append(o)
    return {"kind": "compare", "pose": pose, "tolerance": tol, "T": T, "rows": rows, "ca": ca, "cb": cb,
            "pose_ok": pose_ok, "norm": norm_name, "mm": mm, "multi": [A.multi, B.multi]}


# --- калибровка (FaceMesh.Calibrate) ---
def iris_px(f):
    if not f.has(RIRIS + LIRIS):
        return None

    def d(ids):
        best = 0.0
        for a in ids:
            for b in ids:
                (x1, y1), (x2, y2) = f.px(a), f.px(b)
                best = max(best, math.hypot(x1 - x2, y1 - y2))
        return best

    return (d(RIRIS) + d(LIRIS)) / 2


def ipd_px(f):
    v = sym_one(f)
    return v["ipd"] if v else None


def calibrate_iris(frame_list):
    S = []
    for f in frames(frame_list):
        iris, ipd = iris_px(f), ipd_px(f)
        if iris and ipd:
            S.append({"iris": iris, "ipd": ipd})
    if len(S) < SYM_NEED:
        raise MetricsError("not_enough_frames", "Недостаточно кадров с видимой радужкой")
    iris, ipd = median([s["iris"] for s in S]), median([s["ipd"] for s in S])
    mmpp = IRIS_MM / iris
    return {"source": "iris", "label": "по радужке (приблизительно, погрешность выше)", "approx": True,
            "mm_per_px": mmpp, "ipd_mm": ipd * mmpp, "ts": now()}


def calibrate_card(pts, ipd):
    """pts: 4 угла карты (левый верх, правый верх, правый низ, левый низ) в пикселях кадра; ipd: МЗР в пикселях."""
    try:
        P = [(float(p["x"]), float(p["y"])) for p in pts]
        ipd = float(ipd)
    except (TypeError, ValueError, KeyError):
        raise MetricsError("bad_card", "Нужны 4 угла карты и МЗР")
    if len(P) != 4:
        raise MetricsError("bad_card", "Нужны 4 угла карты")

    def dist(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    w = (dist(P[0], P[1]) + dist(P[3], P[2])) / 2
    hh = (dist(P[0], P[3]) + dist(P[1], P[2])) / 2
    if w < 20 or hh < 12:
        raise MetricsError("card_too_small", "Карта на кадре слишком мала")
    mw, mh = CARD_W / w, CARD_H / hh
    mmpp = (mw + mh) / 2
    return {"source": "card", "label": "по банковской карте 85,6 мм", "approx": False, "mm_per_px": mmpp,
            "ipd_mm": ipd * mmpp, "skew": abs(mw - mh) / mmpp, "ts": now()}
