# -*- coding: utf-8 -*-
"""Графики документации из сохранённых результатов замеров (ничего не перемеряет).

    python3 docs/make_charts.py        → docs/img/*.png
Источники: loadtest/results*/summary.json, loadtest/results_bench/*.json, loadtest/results_llm*/,
metrology/results/*.json. Нужен matplotlib.
"""
from __future__ import annotations

import json
import statistics as st
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
IMG = ROOT / "docs" / "img"
LT = ROOT / "loadtest"
MR = ROOT / "metrology" / "results"
C1, C2, C3, GRID = "#1f6f78", "#c0622b", "#7a7a7a", "#e6e6e6"
IPD_MM = 63.0

plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                     "grid.color": GRID, "figure.dpi": 110, "savefig.bbox": "tight", "legend.frameon": False})


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def steps(d: str) -> list[dict]:
    s = load(LT / d / "summary.json")
    return s["steps"] if isinstance(s, dict) else s


def save(fig, name: str) -> None:
    IMG.mkdir(parents=True, exist_ok=True)
    fig.savefig(IMG / name)
    plt.close(fig)
    print("docs/img/" + name)


def perf_baseline() -> None:
    rows = steps("results")
    n = [r["pairs"] for r in rows]
    fig, ax = plt.subplots(1, 3, figsize=(12, 3.4))
    ax[0].plot(n, [r["api_p50_ms"] for r in rows], "o-", color=C1, label="p50")
    ax[0].plot(n, [r["api_p95_ms"] for r in rows], "s-", color=C2, label="p95")
    ax[0].axhline(500, ls="--", color=C3, lw=1, label="порог 500 мс")
    ax[0].set(title="Задержка API", xlabel="пар врач–пациент", ylabel="мс", xscale="log", xticks=n, xticklabels=n)
    ax[0].legend()
    ax[1].plot(n, [r["signal_p95_ms"] for r in rows], "o-", color=C1)
    ax[1].set(title="Сигнализация звонка, p95", xlabel="пар врач–пациент", ylabel="мс", xscale="log", xticks=n, xticklabels=n)
    lag = [(r["pairs"], r["lag_p95_s"]) for r in rows if r["lag_p95_s"] is not None]
    ax[2].plot([a for a, _ in lag], [b for _, b in lag], "o-", color=C2)
    ax[2].axhline(30, ls="--", color=C3, lw=1, label="порог 30 с")
    ax[2].set(title="Отставание расшифровки, p95", xlabel="пар врач–пациент", ylabel="с", xscale="log", xticks=n, xticklabels=n)
    ax[2].legend()
    fig.suptitle("Нагрузочный тест: полный приём, 1–16 пар (loadtest/results)", y=1.03)
    save(fig, "perf_baseline.png")


def perf_parallel() -> None:
    b = load(LT / "results_bench" / "transcribe_load.json")["summary"]
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.4))
    for var, col, mk in (("без параллельности", C3, "o"), ("с потоками", C1, "s")):
        rows = [r for r in b if r["variant"] == var]
        n = [r["pairs"] for r in rows]
        ax[0].plot(n, [r["lag_p95_s"] for r in rows], mk + "-", color=col, label=var)
        ax[1].plot(n, [r["chunks_per_min"] for r in rows], mk + "-", color=col, label=var)
    ax[0].axhline(30, ls="--", color=C2, lw=1, label="порог 30 с")
    ax[0].set(title="Отставание текста от речи, p95", xlabel="пар", ylabel="с", xticks=[1, 2, 4, 8])
    ax[1].set(title="Пропускная способность", xlabel="пар", ylabel="кусков по 10 с в минуту", xticks=[1, 2, 4, 8])
    ax[0].legend()
    fig.suptitle("Расшифровка речи: Python 3.12 (1 исполнитель) против Python 3.14t без GIL (2 потока)", y=1.03)
    save(fig, "perf_parallel.png")


def perf_llm() -> None:
    res = {p.stem: load(p) for p in sorted((LT / "results_llm").glob("*.json"))}
    order = [("ollama_qwen2.5-3b", "Ollama qwen2.5:3b\n(локально)", C3), ("gigachat_GigaChat-2", "GigaChat-2 Lite\n(облако)", C1),
             ("gigachat_GigaChat-2-Max", "GigaChat-2-Max\n(облако)", C2)]
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.4))
    names = [o[1] for o in order if o[0] in res]
    vals = [res[o[0]]["dialog_mean_s"] for o in order if o[0] in res]
    ent = [100 * st.mean(r["entity"] for r in res[o[0]]["runs"]) for o in order if o[0] in res]
    bars = ax[0].bar(names, vals, color=[o[2] for o in order if o[0] in res])
    ax[0].bar_label(bars, fmt="%.1f с")
    ax[0].set(title="Обезличивание одного диалога", ylabel="с")
    bars = ax[1].bar(names, ent, color=[o[2] for o in order if o[0] in res])
    ax[1].bar_label(bars, fmt="%.1f %%")
    ax[1].axhline(90, ls="--", color=C3, lw=1, label="порог проекта 90 %")
    ax[1].set(title="Найдено имён, мест, организаций", ylabel="%", ylim=(80, 100))
    ax[1].legend(loc="lower right")
    fig.suptitle("Нейросеть обезличивания: 30 синтетических диалогов (loadtest/results_llm)", y=1.03)
    save(fig, "perf_llm_deid.png")

    o, g = steps("results_llm_load/ollama"), steps("results_llm_load/gigachat")
    n = [r["pairs"] for r in o]
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.4))
    ax[0].plot(n, [r["deid_after_end_max_s"] for r in o], "o-", color=C3, label="Ollama")
    ax[0].plot(n, [r["deid_after_end_max_s"] for r in g], "s-", color=C1, label="GigaChat-2-Max")
    ax[0].axhline(300, ls="--", color=C2, lw=1, label="порог 300 с")
    ax[0].set(title="Готовый обезличенный текст после «Завершить»\n(худший приём ступени)", xlabel="пар", ylabel="с", xticks=n)
    ax[0].legend()
    ax[1].plot(n, [r["lag_p95_s"] for r in o], "o-", color=C3, label="Ollama")
    ax[1].plot(n, [r["lag_p95_s"] for r in g], "s-", color=C1, label="GigaChat-2-Max")
    ax[1].axhline(30, ls="--", color=C2, lw=1, label="порог 30 с")
    ax[1].set(title="Отставание расшифровки речи, p95", xlabel="пар", ylabel="с", xticks=n)
    ax[1].legend()
    fig.suptitle("Всё приложение под нагрузкой: локальная Ollama против облачного GigaChat (loadtest/results_llm_load)", y=1.1)
    save(fig, "perf_llm_app.png")


def metro_phantom() -> None:
    d = load(MR / "phantom.json")
    subs = [s for s in d["subjects"] if s.get("zero")]
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.8))
    for i, (reg, metric, title) in enumerate((("brow", "brow", "Бровь"), ("mouth", "mcor", "Угол рта"))):
        xs, ys = [], []
        for s in subs:
            z = st.mean(m[metric] for m in s["zero"])
            for r in s["induced"].get(reg, []):
                xs.append(r["truth"] * IPD_MM)
                ys.append((r["measured"] - z) * IPD_MM)
        ax[i].scatter(xs, ys, s=10, color=C1, alpha=0.5, label="измерения")
        lim = max(xs) * 1.05
        ax[i].plot([0, lim], [0, lim], "--", color=C3, lw=1, label="идеал: измерено = истина")
        k = sum(x * y for x, y in zip(xs, ys)) / sum(x * x for x in xs)
        ax[i].plot([0, lim], [0, k * lim], color=C2, lw=1.5, label="наклон через ноль: %.2f" % k)
        ax[i].set(title=title + ": заданная асимметрия", xlabel="истинное изменение, мм", ylabel="измеренное изменение, мм", xlim=(0, lim), ylim=(0, lim))
        ax[i].legend()
    fig.suptitle("Цифровой фантом: чувствительность (%d добровольцев MEEI, 8 измерений на уровень)" % len(subs), y=1.03)
    save(fig, "metro_phantom_linearity.png")

    keys = [("brow", "Высота бровей"), ("mcor", "Высота уголков рта"), ("mouth", "Уголки рта до ср. линии"), ("eo", "Внешние уголки глаз"),
            ("ei", "Внутренние уголки глаз"), ("half", "Ширина половин"), ("nose", "Кончик носа"), ("chin", "Подбородок")]
    bias_m, bias_s, mdc = [], [], []
    for k, _ in keys:
        b = [st.mean(m[k] * IPD_MM for m in s["zero"]) for s in subs]
        w = [st.stdev([m[k] * IPD_MM for m in s["zero"]]) for s in subs]
        bias_m.append(st.mean(b))
        bias_s.append(st.stdev(b))
        mdc.append(1.96 * 2 ** 0.5 * (st.mean(v * v for v in w)) ** 0.5)
    fig, ax = plt.subplots(1, 2, figsize=(11, 3.8))
    y = range(len(keys))
    ax[0].errorbar(bias_m, y, xerr=bias_s, fmt="o", color=C2, capsize=3)
    ax[0].axvline(0, color=C3, lw=1)
    ax[0].set(title="Смещение нуля: среднее ± SD между людьми", xlabel="мм (на идеально симметричном лице)", yticks=list(y), yticklabels=[k[1] for k in keys])
    bars = ax[1].barh(list(y), mdc, color=C1)
    ax[1].bar_label(bars, fmt="%.2f")
    ax[1].set(title="Повторяемость: MDC95", xlabel="мм", yticks=list(y), yticklabels=[""] * len(keys))
    fig.suptitle("Цифровой фантом: систематическая ошибка и повторяемость", y=1.03)
    save(fig, "metro_phantom_bias.png")

    conds = [("dark", "темно −45 %"), ("bright", "пересвет +35 %"), ("noise", "сильный шум"), ("jpeg", "JPEG 35"), ("lowres", "МЗР 60 px")]
    fig, ax = plt.subplots(figsize=(8, 3.4))
    width = 0.25
    for j, (k, lab, col) in enumerate((("brow", "высота бровей", C1), ("mcor", "высота уголков рта", C2), ("mouth", "уголки рта до ср. линии", C3))):
        vals = []
        for c, _ in conds:
            dev = []
            for s in subs:
                z = st.mean(m[k] for m in s["zero"])
                dev += [abs(m[k] - z) * IPD_MM for m in s["conditions"].get(c, []) if m]
            vals.append(st.mean(dev) if dev else 0)
        ax.bar([i + (j - 1) * width for i in range(len(conds))], vals, width, color=col, label=lab)
    ax.set(title="Плохие условия съёмки: отклонение от нормальных условий\n(наклон головы 6° отклоняется контролем позы в 100 % случаев)",
           ylabel="мм", xticks=range(len(conds)), xticklabels=[c[1] for c in conds])
    ax.legend()
    save(fig, "metro_phantom_conditions.png")


def roc(neg: list[float], pos: list[float]):
    thr = sorted(set(neg + pos), reverse=True)
    pts = [(0.0, 0.0)]
    for t in thr:
        pts.append((sum(n >= t for n in neg) / len(neg), sum(p >= t for p in pos) / len(pos)))
    pts.append((1.0, 1.0))
    return pts


def metro_meei() -> None:
    d = load(MR / "meei.json")
    ok = [s for s in d["subjects"] if "dai" in s]
    labels = ["норма", "near\nnormal", "mild", "moderate", "severe", "complete"]
    fig, ax = plt.subplots(1, 2, figsize=(11, 3.8), sharey=True)
    for i, (cat, title, col) in enumerate((("flaccid", "Вялый паралич", C2), ("synkinetic", "Синкинезия", C1))):
        for sev in range(6):
            v = [s["dai"] for s in ok if (s["category"] == cat or s["category"] == "normal") and s["severity"] == sev]
            if not v:
                continue
            ax[i].scatter([sev + (k - len(v) / 2) * 0.04 for k in range(len(v))], v, color=C3 if sev == 0 else col, s=22, zorder=3)
            ax[i].hlines(st.median(v), sev - 0.25, sev + 0.25, color="black", lw=2, zorder=4)
        rho = d["summary"]["severity"][cat]
        ax[i].set(title="%s: ρ = %.2f (95 %% ДИ %.2f–%.2f)" % (title, rho["rho"], *rho["ci95"]), xticks=range(6), xticklabels=labels,
                  xlabel="тяжесть по eFACE (оценка врача)")
    ax[0].set_ylabel("индекс динамической асимметрии")
    fig.suptitle("MEEI Facial Palsy Standard Set: индекс асимметрии против оценки врача (черта — медиана)", y=1.03)
    save(fig, "metro_meei_severity.png")

    neg = [s["dai"] for s in ok if s["category"] == "normal"]
    pos = [s["dai"] for s in ok if s["category"] != "normal"]
    a = d["summary"]["auc_normal_vs_patients"]
    pts = roc(neg, pos)
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.2), gridspec_kw={"width_ratios": [1, 1.5]})
    ax[0].plot([p[0] for p in pts], [p[1] for p in pts], color=C1, lw=2, label="ИДА, AUC = %.2f" % a["auc"])
    ax[0].plot([0, 1], [0, 1], "--", color=C3, lw=1, label="случайное угадывание")
    ax[0].set(title="ROC: здоровые (%d) против пациентов (%d)" % (len(neg), len(pos)), xlabel="доля здоровых выше порога",
              ylabel="доля пациентов выше порога")
    ax[0].legend(loc="lower right")
    app = load(MR / "meei_app.json")["summary"] if (MR / "meei_app.json").exists() else {}
    items = [("Брови\n(точки)", d["summary"]["per_move"]["brow"]), ("Улыбка\n(точки)", d["summary"]["per_move"]["smile"]),
             ("Зажмуривание\n(точки)", d["summary"]["per_move"]["eye"])]
    items += [(lab, app[k]) for lab, k in (("Брови\n(blendshape)", "brow"), ("Улыбка\n(blendshape)", "smile"),
                                            ("Зажмуривание\n(blendshape)", "eye_forceful"), ("Мягкое закрытие\n(blendshape)", "eye_gentle")) if k in app]
    vals = [v["auc"] for _, v in items]
    lo = [v["auc"] - v["ci95"][0] for _, v in items]
    hi = [v["ci95"][1] - v["auc"] for _, v in items]
    ax[1].bar(range(len(items)), vals, yerr=[lo, hi], color=[C1 if v >= 0.7 else C3 for v in vals], capsize=3)
    ax[1].axhline(0.5, ls="--", color=C2, lw=1, label="0,5 — не различает")
    ax[1].set(title="AUC по движениям (95 % ДИ)", xticks=range(len(items)), xticklabels=[i[0] for i in items], ylim=(0, 1))
    ax[1].set_xticklabels([i[0].replace("\n", " ") for i in items], rotation=30, ha="right", fontsize=8)
    ax[1].legend(loc="upper right")
    save(fig, "metro_meei_roc.png")


if __name__ == "__main__":
    perf_baseline()
    perf_parallel()
    perf_llm()
    metro_phantom()
    metro_meei()
