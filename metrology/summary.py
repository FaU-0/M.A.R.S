# -*- coding: utf-8 -*-
"""Сводные таблицы по результатам metrology/phantom.py и metrology/meei.py (Markdown в stdout).

    python3 metrology/summary.py
Миллиметры — доли МЗР × 63 мм (средний МЗР взрослого).
"""
from __future__ import annotations

import json
import math
import statistics as st
from pathlib import Path

RES = Path(__file__).resolve().parent / "results"
IPD_MM = 63.0
LABELS = {"eo": "Внешние уголки глаз", "ei": "Внутренние уголки глаз", "mouth": "Уголки рта (до средней линии)",
          "brow": "Высота бровей", "mcor": "Высота уголков рта", "half": "Ширина половин лица",
          "nose": "Кончик носа", "chin": "Подбородок", "tilt": "Наклон линии глаз"}
CONDS = {"dark": "темно (−45 %)", "bright": "пересвет (+35 %)", "noise": "сильный шум", "jpeg": "сжатие JPEG 35",
         "lowres": "МЗР 60 пикселей", "roll": "наклон 6°"}


def mm(v: float, key: str) -> float:
    return v if key == "tilt" else v * IPD_MM


def fmt(v: float, nd: int = 2) -> str:
    return ("%." + str(nd) + "f") % v if v == v else "—"


def sdv(xs: list[float]) -> float:
    return st.stdev(xs) if len(xs) > 1 else float("nan")


def slope(xs: list[float], ys: list[float]) -> tuple[float, float]:
    mx, my = st.mean(xs), st.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    ss_res = sum((y - (my + b * (x - mx))) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    return b, 1 - ss_res / ss_tot if ss_tot else float("nan")


def phantom() -> None:
    d = json.loads((RES / "phantom.json").read_text())
    subs = [s for s in d["subjects"] if "zero" in s and s["zero"]]
    print("## Цифровой фантом: %d добровольцев, %d измерений на условие, %d кадров на измерение\n"
          % (len(subs), d["repeats"], d["frames_per_measurement"]))
    print("| Метрика | Смещение нуля, мм (среднее ± SD между людьми) | Повторяемость SD, мм | MDC95, мм |")
    print("|---|---|---|---|")
    keys = list(subs[0]["zero"][0].keys())
    for k in keys:
        bias = [st.mean(mm(m[k], k) for m in s["zero"]) for s in subs]
        within = [sdv([mm(m[k], k) for m in s["zero"]]) for s in subs if len(s["zero"]) > 1]
        sd_w = math.sqrt(st.mean(v * v for v in within))  # объединённое внутрисубъектное SD
        unit = "°" if k == "tilt" else ""
        print("| %s%s | %s ± %s | %s | %s |" % (LABELS.get(k, k), " (градусы)" if unit else "", fmt(st.mean(bias)),
                                             fmt(sdv(bias)), fmt(sd_w), fmt(1.96 * math.sqrt(2) * sd_w)))
    print("\n### Заданная асимметрия: измерено против истины\n")
    print("| Область | Задано, мм | Истинное изменение, мм | Измеренное изменение, мм (среднее ± SD) | Доля увиденного |")
    print("|---|---|---|---|---|")
    for reg in ("brow", "mouth"):
        metric = "brow" if reg == "brow" else "mcor"
        xs, ys = [], []
        for lvl in d["levels_mm"]:
            tr, ch = [], []
            for s in subs:
                z = st.mean(m[metric] for m in s["zero"])
                rows = [r for r in s["induced"].get(reg, []) if r["mm"] == lvl]
                if not rows:
                    continue
                tr.append(rows[0]["truth"] * IPD_MM)
                for r in rows:
                    ch.append((r["measured"] - z) * IPD_MM)
                    xs.append(rows[0]["truth"] * IPD_MM)
                    ys.append((r["measured"] - z) * IPD_MM)
            print("| %s | %s | %s | %s ± %s | %s %% |" % ("Бровь" if reg == "brow" else "Угол рта", fmt(lvl, 1), fmt(st.mean(tr)),
                                                    fmt(st.mean(ch)), fmt(sdv(ch)), fmt(100 * st.mean(ch) / st.mean(tr), 0)))
        b, r2 = slope(xs, ys)
        print("| %s: наклон | | | %s (R² = %s) | |" % ("Бровь" if reg == "brow" else "Угол рта", fmt(b), fmt(r2)))
    print("\n### Плохие условия съёмки: отклонение от нормальных условий на симметричном фантоме, мм\n")
    print("| Условие | Не измерено, % | Высота бровей | Высота уголков рта | Уголки рта до средней линии |")
    print("|---|---|---|---|---|")
    for c in d["subjects"][0]["conditions"]:
        fail, dev = 0, {"brow": [], "mcor": [], "mouth": []}
        total = 0
        for s in subs:
            z = {k: st.mean(m[k] for m in s["zero"]) for k in dev}
            for m in s["conditions"].get(c, []):
                total += 1
                if m is None:
                    fail += 1
                    continue
                for k in dev:
                    dev[k].append(abs(m[k] - z[k]) * IPD_MM)
        cell = lambda k: fmt(st.mean(dev[k])) if dev[k] else "—"  # noqa: E731
        print("| %s | %s | %s | %s | %s |" % (CONDS.get(c, c), fmt(100 * fail / max(total, 1), 0), cell("brow"), cell("mcor"), cell("mouth")))


def meei() -> None:
    d = json.loads((RES / "meei.json").read_text())
    s = d["summary"]
    print("\n## MEEI Facial Palsy Standard Set: %d человек, в анализе %d\n" % (s["subjects"], s["analysed"]))
    sa = s["side_agreement"]
    print("- Сторона паралича определена верно: %d из %d (%s %%, 95 %% ДИ %s–%s %%)"
          % (sa["agree"], sa["n"], fmt(100 * sa["share"], 0), fmt(100 * sa["ci95"][0], 0), fmt(100 * sa["ci95"][1], 0)))
    a = s["auc_normal_vs_patients"]
    print("- AUC «здоровые против пациентов» по ИДА: %s (95 %% ДИ %s–%s), %d здоровых, %d пациентов"
          % (fmt(a["auc"]), fmt(a["ci95"][0]), fmt(a["ci95"][1]), a["n_normal"], a["n_patients"]))
    for cat, v in s["severity"].items():
        print("- ρ Спирмена ИДА с тяжестью по eFACE, %s: %s (95 %% ДИ %s–%s), n = %d"
              % ("вялый паралич" if cat == "flaccid" else "синкинезия", fmt(v["rho"]), fmt(v["ci95"][0]), fmt(v["ci95"][1]), v["n"]))
    print("\n| Движение | AUC (95 % ДИ) | Сторона верно | ρ с тяжестью, вялый |")
    print("|---|---|---|---|")
    names = {"brow": "Брови вверх", "eye": "Сильное закрытие глаз", "smile": "Широкая улыбка"}
    for mv, v in s["per_move"].items():
        print("| %s | %s (%s–%s) | %s %% из %d | %s |" % (names[mv], fmt(v["auc"]), fmt(v["ci95"][0]), fmt(v["ci95"][1]),
                                                     fmt(100 * v["side_agreement"], 0), v["n_sided"], fmt(v["rho_flaccid"])))
    print("\n| Статика в покое (метрика приложения) | AUC (95 % ДИ) |")
    print("|---|---|")
    for k, v in s["static_rest_auc"].items():
        print("| %s | %s (%s–%s) |" % (LABELS.get(k, k), fmt(v["auc"]), fmt(v["ci95"][0]), fmt(v["ci95"][1])))


if __name__ == "__main__":
    if (RES / "phantom.json").exists():
        phantom()
    if (RES / "meei.json").exists():
        meei()
