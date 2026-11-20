# -*- coding: utf-8 -*-
"""Опорная линия глаз: центры зрачков (как сейчас) против уголков глаз — на симметричном фантоме.

Гипотеза (из metrology/REPORT.md): основную часть смещения нуля даёт ошибка центров зрачков по вертикали.
Сравниваются три опорные линии, масштаб у всех одинаковый — межзрачковое расстояние (МЗР):
  pupils  — центры зрачков 468/473 (приложение сейчас);
  inner   — внутренние уголки глаз 133/362;
  corners — середины глаз по уголкам: (33+133)/2 и (263+362)/2.
Критерии (заданы до запуска): смещение нуля между людьми (SD), повторяемость (SD внутри человека)
и доля увиденного при подъёме брови и угла рта на 3 мм. Лучшая линия — с меньшим смещением нуля
без потери повторяемости и чувствительности.

    vision/.venv/bin/python -m metrology.baseline
Результат: metrology/results/baseline.json
"""
from __future__ import annotations

import json
import math
import random
import statistics as st

import numpy as np

from metrology import common as C, phantom as PH
from vision import metrics

BASELINES = {"pupils": ((468,), (473,)), "inner": ((133,), (362,)), "corners": ((33, 133), (263, 362))}
KEYS = ("brow", "mcor", "mouth", "eo")
REPEATS = 8
LEVEL_MM = 3.0


def sym_variant(frame: dict, base: str) -> dict | None:
    """Как metrics.sym_one, но с другой опорной линией; масштаб — МЗР по зрачкам."""
    f = metrics.Frame(frame)
    if not metrics.in_tol(metrics.euler(f), C.POSE_TOL) or not f.has(metrics.SYM_IDX):
        return None
    P = metrics.derot(f)
    ipd = math.hypot(P[473][0] - P[468][0], P[473][1] - P[468][1])
    ri, li = BASELINES[base]
    R = np.mean([P[i] for i in ri], axis=0)
    L = np.mean([P[i] for i in li], axis=0)
    u = (L - R) / np.linalg.norm(L - R)
    n = np.array([-u[1], u[0]])
    M = (R + L) / 2
    along = lambda i: float(np.dot(np.array(P[i]) - M, u)) / ipd  # noqa: E731
    down = lambda i: float(np.dot(np.array(P[i]) - M, n)) / ipd  # noqa: E731
    return {"brow": -down(105) + down(334), "mcor": -down(61) + down(291),
            "mouth": -along(61) - along(291), "eo": -along(33) - along(263)}


def measure_all(img, rng) -> dict | None:
    """Одно измерение для всех опорных линий на одних и тех же кадрах (честное сравнение)."""
    acc = {b: [] for b in BASELINES}
    tries = 0
    while len(acc["pupils"]) < C.FRAMES_PER_MEASUREMENT and tries < 3 * C.FRAMES_PER_MEASUREMENT:
        tries += 1
        im, q = PH.jitter(img, rng)
        fr = C.frame_of(im, q)
        if not fr:
            continue
        vals = {b: sym_variant(fr, b) for b in BASELINES}
        if any(v is None for v in vals.values()):
            continue
        for b, v in vals.items():
            acc[b].append(v)
    if len(acc["pupils"]) < C.FRAMES_PER_MEASUREMENT:
        return None
    return {b: {k: st.median(s[k] for s in acc[b]) for k in KEYS} for b in BASELINES}


def main() -> None:
    C.need_detector()
    rng = random.Random(20261121)
    src = C.ROOT / "datasets" / "meei" / "Normals"
    out = {"baselines": list(BASELINES), "subjects": []}
    for d in sorted(p for p in src.iterdir() if p.is_dir()):
        ph = PH.symmetric_phantom(C.read(C.photo(d, 1)))
        if ph is None:
            continue
        img, ipd_px = ph
        fr = C.frame_of(img)
        if not fr or C.sym(fr) is None:
            print(d.name, "вне допуска позы")
            continue
        P = PH.landmarks_px(fr)
        rec = {"subject": d.name, "zero": [], "induced": {}}
        for _ in range(REPEATS):
            m = measure_all(img, rng)
            if m:
                rec["zero"].append(m)
        for reg, spec in PH.REGIONS.items():
            center = P[list(spec["points"])].mean(axis=0)
            sigma = (spec["sigma"][0] * ipd_px, spec["sigma"][1] * ipd_px)
            d_px = LEVEL_MM / PH.IPD_MM * ipd_px
            warped = PH.warp(img, center, spec["dir"], d_px, sigma)
            rec["induced"][reg] = {"truth": PH.truth(P, center, spec["dir"], d_px, sigma, ipd_px, spec["metric"]),
                                   "metric": spec["metric"], "measured": [m for m in (measure_all(warped, rng) for _ in range(REPEATS)) if m]}
        out["subjects"].append(rec)
        print(d.name, "готово", flush=True)
    summary = {}
    subs = [s for s in out["subjects"] if s["zero"]]
    for b in BASELINES:
        row = {}
        for k in KEYS:
            bias = [st.mean(m[b][k] for m in s["zero"]) * PH.IPD_MM for s in subs]
            within = [st.stdev([m[b][k] * PH.IPD_MM for m in s["zero"]]) for s in subs]
            row[k] = {"bias_mean": st.mean(bias), "bias_sd": st.stdev(bias), "sd_within": math.sqrt(st.mean(v * v for v in within))}
        for reg in PH.REGIONS:
            seen = []
            for s in subs:
                ind = s["induced"][reg]
                z = st.mean(m[b][ind["metric"]] for m in s["zero"])
                seen += [(m[b][ind["metric"]] - z) / ind["truth"] for m in ind["measured"]]
            row["seen_" + reg] = st.mean(seen)
        summary[b] = row
    out["summary"] = summary
    (C.ROOT / "metrology" / "results" / "baseline.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
