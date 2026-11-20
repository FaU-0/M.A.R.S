# -*- coding: utf-8 -*-
"""Клиническая состоятельность: метрики M.A.R.S. против оценок врачей в MEEI Facial Palsy Standard Set.

Набор (Greene et al., Laryngoscope 2020): 10 здоровых и 51 пациент (вялый и синкинетический паралич),
8 снимков движений, категория тяжести по шкале eFACE (врач) и сторона паралича. Числовых оценок HB
и Sunnybrook по отдельным пациентам в открытой таблице нет — сравнение идёт с категорией по eFACE.

Снимки: 1 покой, 2 брови вверх, 3 мягкое закрытие глаз, 4 сильное закрытие, 5 мягкая улыбка,
6 широкая улыбка, 7 трубочка, 8 нижние зубы. Уменьшаются до 1920 пикселей (кадр камеры Full HD).

План анализа (зафиксирован до первого запуска):
  Движения: брови (2 против 1), сильное закрытие глаз (4 против 1), широкая улыбка (6 против 1).
  Амплитуда стороны E — смещение точек от покоя, в долях межкантального расстояния (МКР) кадра:
    брови — подъём центра брови над линией внутренних уголков глаз;
    глаза — уменьшение раскрытия глазной щели (верхнее и нижнее веко);
    улыбка — длина вектора смещения угла рта.
  Асимметрия движения A = 1 − min(E_п, E_л) / max(E_п, E_л): 0 — симметрично, 1 — одна сторона неподвижна.
  Индекс динамической асимметрии ИДА = среднее A по трём движениям.
  Слабая сторона: знак суммы (E_п − E_л) / max(E_п, E_л) по трём движениям.
  Основные показатели:
    1) совпадение слабой стороны с записанной стороной паралича (доля, 95 % ДИ Уилсона);
    2) ρ Спирмена ИДА с тяжестью: норма 0, near normal 1, mild 2, moderate 3, severe 4, complete 5
       (отдельно вялый паралич и синкинезия; здоровые входят в обе группы как 0), 95 % ДИ бутстрепом;
    3) AUC ИДА: здоровые против всех пациентов, 95 % ДИ бутстрепом.
  Дополнительно: каждое движение отдельно; статическая асимметрия в покое (метрики приложения).
  Снимок исключается, если лицо не найдено или поза вне 10° (как допуск подвижности в приложении).

    vision/.venv/bin/python -m metrology.meei
Результат: metrology/results/meei.json (только числа, без снимков).
"""
from __future__ import annotations

import json
import math
import random
import re
import time
import zipfile
from pathlib import Path

import numpy as np

from metrology import common as C

DATA = C.ROOT / "datasets" / "meei"
OUT = C.ROOT / "metrology" / "results"
POSE_TOL_DYN = 10.0
SEVERITY = {"normal": 0, "near normal": 1, "mild": 2, "moderate": 3, "severe": 4, "complete": 5}
# стороны в индексах MediaPipe, как в приложении (vision/metrics.py): R — правая сторона лица пациента
BROW = {"R": (70, 63, 105, 66, 107), "L": (300, 293, 334, 296, 336)}
LID = {"R": (159, 145), "L": (386, 374)}
CORNER = {"R": 61, "L": 291}
CANTHI = (133, 362)
MOVES = {"brow": 2, "eye": 4, "smile": 6}
BOOT = 2000


def table() -> list[dict]:
    """SCBS Standard set with data.xlsx без сторонних библиотек: категория, тяжесть, номер, сторона."""
    z = zipfile.ZipFile(DATA / "SCBS Standard set with data.xlsx")
    ss = [re.sub(r"<[^>]+>", "", x) for x in re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode(), re.S)]
    rows = []
    for r in re.findall(r"<row[^>]*>(.*?)</row>", z.read("xl/worksheets/sheet1.xml").decode(), re.S):
        cells = {}
        for col, attrs, v in re.findall(r'<c r="([A-Z]+)\d+"([^>]*)>(?:<f>.*?</f>)?<v>([^<]*)</v>', r):
            cells[col] = ss[int(v)].strip() if 't="s"' in attrs else v.strip()
        rows.append(cells)
    head = rows[0]
    keys = {head[c]: c for c in head}
    out = []
    for r in rows[1:]:
        get = lambda name: r.get(keys[name], "")  # noqa: E731
        out.append({"category": get("Category"), "sub": get("Sub-category"), "n": get("#"), "side": get("Side of Paralysis")})
    return out


def _norm(text: str) -> str:
    return re.sub(r"[\s_-]", "", text.lower())


def folder(row: dict) -> Path | None:
    """Папка человека: <Категория>/<Тяжесть><Категория>/<имя с номером>. Названия в наборе записаны по-разному
    («Complete Synkinetic», «NearNormalSynkinetic», «Synkinetic_Mild1»), поэтому сравниваются без пробелов и регистра."""
    cat = row["category"].lower()
    top = {"normal": "Normals", "flaccid": "Flaccid", "synkinetic": "Synkinetic"}.get(cat)
    if not top or not row["n"]:
        return None
    n = str(int(float(row["n"])))
    base = DATA / top
    if cat != "normal":
        subs = [d for d in base.iterdir() if d.is_dir() and _norm(d.name).startswith(_norm(row["sub"]))]
        if len(subs) != 1:
            return None
        base = subs[0]
    cands = [d for d in base.iterdir() if d.is_dir() and re.fullmatch(r"\D*%s" % n, d.name)]
    return cands[0] if len(cands) == 1 else None


def face_frame(frame: dict) -> dict | None:
    """Точки в системе координат лица: начало — середина внутренних уголков глаз, ось x — к левому уголку,
    единица — межкантальное расстояние. None, если поза вне допуска."""
    e = C.pose(frame)
    if not e or max(abs(e["yaw"]), abs(e["pitch"]), abs(e["roll"])) > POSE_TOL_DYN:
        return None
    P = np.array([[p[0] * frame["vw"], p[1] * frame["vh"]] for p in frame["lm"]])
    a, b = P[CANTHI[0]], P[CANTHI[1]]
    icd = np.linalg.norm(b - a)
    u = (b - a) / icd
    n = np.array([-u[1], u[0]])  # вниз по лицу
    m = (a + b) / 2
    return {"pts": np.stack([(P - m) @ u, (P - m) @ n], axis=1) / icd, "pose": e}


def amplitude(move: str, rest: np.ndarray, act: np.ndarray, side: str) -> float:
    if move == "brow":
        idx = list(BROW[side])
        return float(-(act[idx, 1].mean() - rest[idx, 1].mean()))  # подъём = уменьшение y
    if move == "eye":
        up, lo = LID[side]
        return float((rest[lo, 1] - rest[up, 1]) - (act[lo, 1] - act[up, 1]))
    i = CORNER[side]
    return float(np.linalg.norm(act[i] - rest[i]))


def subject(path: Path) -> dict:
    fr, raw = {}, {}
    for k in (1, *MOVES.values()):
        p = C.photo(path, k)
        raw[k] = C.frame_of(C.read(p)) if p else None
        fr[k] = face_frame(raw[k]) if raw[k] else None
    out = {"rest_static": None, "moves": {}, "excluded": [k for k, v in fr.items() if v is None]}
    s = C.sym(raw[1]) if raw[1] else None
    if s:
        out["rest_static"] = C.diffs(s)
    if fr[1] is None:
        return out
    for move, k in MOVES.items():
        if fr[k] is None:
            continue
        eR = amplitude(move, fr[1]["pts"], fr[k]["pts"], "R")
        eL = amplitude(move, fr[1]["pts"], fr[k]["pts"], "L")
        hi = max(eR, eL)
        if hi <= 1e-6:
            out["moves"][move] = {"R": eR, "L": eL, "A": 1.0, "lean": 0.0}
            continue
        out["moves"][move] = {"R": eR, "L": eL, "A": 1 - max(0.0, min(eR, eL)) / hi, "lean": (eR - eL) / hi}
    if len(out["moves"]) == len(MOVES):
        out["dai"] = float(np.mean([m["A"] for m in out["moves"].values()]))
        lean = sum(m["lean"] for m in out["moves"].values())
        out["weak_side"] = "Right" if lean < 0 else "Left"
    return out


def ranks(x: list[float]) -> np.ndarray:
    x = np.asarray(x, float)
    order = x.argsort()
    r = np.empty(len(x))
    r[order] = np.arange(len(x))
    for v in np.unique(x):  # средние ранги для повторов
        idx = x == v
        r[idx] = r[idx].mean()
    return r


def spearman(x, y) -> float:
    rx, ry = ranks(x), ranks(y)
    return float(np.corrcoef(rx, ry)[0, 1])


def auc(neg, pos) -> float:
    """AUC = доля пар (пациент, здоровый), где у пациента индекс больше; ничьи — половина."""
    s = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return s / (len(pos) * len(neg))


def boot(fn, *groups, n=BOOT, seed=7) -> tuple[float, float]:
    rng = random.Random(seed)
    vals = []
    for _ in range(n):
        res = [[g[rng.randrange(len(g))] for _ in g] for g in groups]
        try:
            v = fn(*res)
        except (ValueError, ZeroDivisionError, FloatingPointError):
            continue
        if not math.isnan(v):
            vals.append(v)
    vals.sort()
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    p, z = k / n, 1.96
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return c - h, c + h


def corr_block(pairs: list[tuple[float, int]]) -> dict:
    x = [p[0] for p in pairs]
    y = [p[1] for p in pairs]
    rho = spearman(x, y)
    lo, hi = boot(lambda pp: spearman([a for a, _ in pp], [b for _, b in pp]), pairs)
    return {"n": len(pairs), "rho": rho, "ci95": [lo, hi]}


def main() -> None:
    C.need_detector()
    rows = table()
    subjects, t0 = [], time.time()
    for i, r in enumerate(rows, 1):
        p = folder(r)
        if p is None:
            print("нет папки для %s %s" % (r["category"], r["n"]))
            continue
        res = subject(p)
        res.update(id=p.name, category=r["category"].lower(), sub=(r["sub"] or "normal").lower(), side=r["side"])
        res["severity"] = 0 if res["category"] == "normal" else SEVERITY.get(res["sub"])
        subjects.append(res)
        print("%d/%d %s: ИДА %s, исключены снимки %s, %.0f с" % (i, len(rows), p.name, "%.2f" % res["dai"] if "dai" in res else "—",
                                                               res["excluded"] or "нет", time.time() - t0), flush=True)
    ok = [s for s in subjects if "dai" in s]
    norm = [s["dai"] for s in ok if s["category"] == "normal"]
    pats = [s["dai"] for s in ok if s["category"] != "normal"]
    sided = [s for s in ok if s["category"] != "normal" and s["side"] in ("Right", "Left")]
    agree = sum(s["weak_side"] == s["side"] for s in sided)
    summary = {
        "subjects": len(subjects), "analysed": len(ok),
        "side_agreement": {"n": len(sided), "agree": agree, "share": agree / len(sided) if sided else None,
                           "ci95": wilson(agree, len(sided))},
        "auc_normal_vs_patients": {"n_normal": len(norm), "n_patients": len(pats), "auc": auc(norm, pats),
                                   "ci95": boot(auc, norm, pats)},
        "severity": {},
        "per_move": {},
        "static_rest_auc": {},
    }
    for cat in ("flaccid", "synkinetic"):
        pairs = [(s["dai"], s["severity"]) for s in ok if s["category"] in ("normal", cat) and s["severity"] is not None]
        summary["severity"][cat] = corr_block(pairs)
    for move in MOVES:
        mo = [s for s in ok if move in s["moves"]]
        nm = [s["moves"][move]["A"] for s in mo if s["category"] == "normal"]
        pt = [s["moves"][move]["A"] for s in mo if s["category"] != "normal"]
        sd = [s for s in mo if s["category"] != "normal" and s["side"] in ("Right", "Left")]
        ag = sum((s["moves"][move]["lean"] < 0) == (s["side"] == "Right") for s in sd)
        summary["per_move"][move] = {"auc": auc(nm, pt), "ci95": boot(auc, nm, pt),
                                     "side_agreement": ag / len(sd) if sd else None, "n_sided": len(sd),
                                     "rho_flaccid": spearman([s["moves"][move]["A"] for s in mo if s["category"] in ("normal", "flaccid")],
                                                             [s["severity"] for s in mo if s["category"] in ("normal", "flaccid")])}
    st = [s for s in subjects if s["rest_static"]]
    for key in ("brow", "mcor", "mouth", "eo", "tilt"):
        nm = [abs(s["rest_static"][key]) for s in st if s["category"] == "normal"]
        pt = [abs(s["rest_static"][key]) for s in st if s["category"] != "normal"]
        summary["static_rest_auc"][key] = {"auc": auc(nm, pt), "ci95": boot(auc, nm, pt), "n_normal": len(nm), "n_patients": len(pt)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "meei.json").write_text(json.dumps({"plan": __doc__.strip(), "summary": summary, "subjects": subjects},
                                              ensure_ascii=False, indent=1, default=float))
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=float))


if __name__ == "__main__":
    main()
