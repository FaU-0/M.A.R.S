# -*- coding: utf-8 -*-
"""Подвижность так, как её считает приложение (коэффициенты blendshapes MediaPipe), на наборе MEEI.

Вкладка «Подвижность» берёт максимум коэффициента для правой и левой стороны за задание и показывает
соотношение min/max (vision/metrics.py: mob_task). Здесь на каждое задание — один снимок набора:
  брови — снимок 2 (browOuterUp), улыбка — снимок 6 (mouthSmile), глаза — снимок 4 (eyeBlink).
Проверяется (задано до запуска):
  1) сторона: у слабой стороны коэффициент меньше; совпадение с записанной стороной паралича.
     Если совпадение заметно меньше 50 %, приложение путает правую и левую сторону коэффициентов;
  2) AUC «здоровые против пациентов» и ρ Спирмена с тяжестью по eFACE для асимметрии 1 − min/max;
  3) закрытие глаз — три способа: eyeBlink при мягком (снимок 3) и сильном (снимок 4) закрытии,
     доля закрытия глазной щели 1 − раскрытие(закрыто)/раскрытие(покой) при мягком закрытии.

    vision/.venv/bin/python -m metrology.meei_app
Результат: metrology/results/meei_app.json
"""
from __future__ import annotations

import json

import numpy as np

from metrology import common as C, meei

TASKS = {"brow": (2, "browOuterUpRight", "browOuterUpLeft"),
         "smile": (6, "mouthSmileRight", "mouthSmileLeft"),
         "eye_forceful": (4, "eyeBlinkRight", "eyeBlinkLeft"),
         "eye_gentle": (3, "eyeBlinkRight", "eyeBlinkLeft")}


def lid_closure(rest: dict, act: dict, side: str) -> float:
    def ap(fr):
        P = np.array([[p[0] * fr["vw"], p[1] * fr["vh"]] for p in fr["lm"]])
        up, lo = meei.LID[side]
        ic = np.linalg.norm(P[133] - P[362])
        return (P[lo][1] - P[up][1]) / ic
    a0 = ap(rest)
    return 1 - ap(act) / a0 if a0 > 1e-6 else float("nan")


def main() -> None:
    C.need_detector()
    rows = [r for r in meei.table() if r["category"]]
    subjects = []
    for r in rows:
        d = meei.folder(r)
        frames = {}
        for k in (1, 2, 3, 4, 6):
            p = C.photo(d, k)
            fr = C.frame_of(C.read(p)) if p else None
            e = C.pose(fr) if fr else None
            frames[k] = fr if e and max(abs(e["yaw"]), abs(e["pitch"]), abs(e["roll"])) <= meei.POSE_TOL_DYN else None
        rec = {"id": d.name, "category": r["category"].lower(), "side": r["side"],
               "severity": 0 if r["category"].lower() == "normal" else meei.SEVERITY.get(r["sub"].lower()), "tasks": {}}
        for t, (k, rb, lb) in TASKS.items():
            fr = frames[k]
            if not fr or rb not in fr["bs"]:
                continue
            R, L = fr["bs"][rb], fr["bs"][lb]
            hi = max(R, L)
            rec["tasks"][t] = {"R": R, "L": L, "A": 1 - min(R, L) / hi if hi > 0.05 else None}
        if frames[1] and frames[3]:
            R, L = lid_closure(frames[1], frames[3], "R"), lid_closure(frames[1], frames[3], "L")
            hi = max(R, L)
            rec["tasks"]["eye_lids_gentle"] = {"R": R, "L": L, "A": 1 - max(0.0, min(R, L)) / hi if hi > 0.05 else None}
        subjects.append(rec)
        print(d.name, {t: round(v["A"], 2) if v["A"] is not None else None for t, v in rec["tasks"].items()}, flush=True)
    summary = {}
    for t in list(TASKS) + ["eye_lids_gentle"]:
        S = [s for s in subjects if t in s["tasks"] and s["tasks"][t]["A"] is not None]
        nm = [s["tasks"][t]["A"] for s in S if s["category"] == "normal"]
        pt = [s["tasks"][t]["A"] for s in S if s["category"] != "normal"]
        sided = [s for s in S if s["category"] != "normal" and s["side"] in ("Right", "Left")]
        agree = sum((s["tasks"][t]["R"] < s["tasks"][t]["L"]) == (s["side"] == "Right") for s in sided)
        fl = [s for s in S if s["category"] in ("normal", "flaccid") and s["severity"] is not None]
        sy = [s for s in S if s["category"] in ("normal", "synkinetic") and s["severity"] is not None]
        summary[t] = {"n_normal": len(nm), "n_patients": len(pt),
                      "auc": meei.auc(nm, pt) if nm and pt else None, "ci95": meei.boot(meei.auc, nm, pt) if nm and pt else None,
                      "side_agree": agree, "side_n": len(sided), "side_ci95": meei.wilson(agree, len(sided)),
                      "rho_flaccid": meei.spearman([s["tasks"][t]["A"] for s in fl], [s["severity"] for s in fl]) if len(fl) > 2 else None,
                      "rho_synkinetic": meei.spearman([s["tasks"][t]["A"] for s in sy], [s["severity"] for s in sy]) if len(sy) > 2 else None}
    (C.ROOT / "metrology" / "results" / "meei_app.json").write_text(
        json.dumps({"plan": __doc__.strip(), "summary": summary, "subjects": subjects}, ensure_ascii=False, indent=1, default=float))
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=float))


if __name__ == "__main__":
    main()
