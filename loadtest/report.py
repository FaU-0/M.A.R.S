# -*- coding: utf-8 -*-
"""Сводка нагрузочного теста: метрики по ступеням, % ухудшения от базовой линии, проверка порогов, графики.

    python3 loadtest/report.py            # читает loadtest/results/, пишет summary.json, tables.md и *.png

Время готовности расшифровки берётся из базы стенда (speech_segments.created, transcripts.updated),
путь к ней записан в steps.json (data_dir).
"""
import csv
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RES = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "results"
LONG_POLL = "GET /rooms/{c}/events"
AUDIO = "POST /rooms/{c}/audio"
# пороги «работает нормально» (согласованы в плане теста)
LIMITS = {"api_p95_ms": 500, "signal_p95_ms": 1000, "error_pct": 1.0, "lag_p95_s": 30, "deid_after_end_s": 300, "print_p95_ms": 2000}


def pct(vals, q):
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * q / 100
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def r1(x):
    return None if x is None else round(x, 1)


def load():
    meta = json.loads((RES / "steps.json").read_text(encoding="utf-8"))
    rd = lambda n: list(csv.DictReader(open(RES / n, encoding="utf-8")))  # noqa: E731
    return meta, rd("requests.csv"), rd("signaling.csv"), rd("audio_sends.csv"), rd("resources.csv"), rd("queue.csv"), rd("jobs.csv")


def main():
    meta, reqs, sigs, sends, res, queue, jobs = load()
    db = Path(meta["data_dir"]) / "mars.sqlite3"
    seg_csv, tr_csv = RES / "speech_done.csv", RES / "deid_done.csv"
    if db.exists():  # первый запуск: время готовности берётся из базы стенда и сохраняется рядом с результатами
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        with open(seg_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["appt_id", "role", "seq", "done"])
            w.writerows(con.execute("SELECT appt_id, role, seq, MIN(created) FROM speech_segments GROUP BY appt_id, role, seq"))
        with open(tr_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["appt_id", "status", "updated"])
            w.writerows(con.execute("SELECT appt_id, status, updated FROM transcripts"))
        con.close()
    seg_done = {(int(r["appt_id"]), r["role"], int(r["seq"])): int(r["done"]) for r in csv.DictReader(open(seg_csv, encoding="utf-8"))}
    deid_ready = {int(r["appt_id"]): int(r["updated"]) for r in csv.DictReader(open(tr_csv, encoding="utf-8")) if r["status"] == "done"}
    jres = defaultdict(list)
    for j in jobs:
        if j["status"] == "done" and j["result"]:
            try:
                jres[j["kind"]].append(json.loads(j["result"]))
            except ValueError:
                pass

    summary = []
    for st in meta["steps"]:
        n = st["step"]
        code2appt = {x["code"]: x["appt"] for x in st["infos"] if "code" in x}
        ended = {x["appt"]: x["ended"] for x in st["infos"] if "ended" in x}
        R = [r for r in reqs if int(r["step"]) == n]
        api = [float(r["ms"]) for r in R if r["endpoint"] not in (LONG_POLL,)]
        errs = [r for r in R if int(r["status"]) < 0 or int(r["status"]) >= 400]
        deg = [r for r in R if r["degraded"] == "1"]
        window = st["t_load_end"] - st["t_start"]
        by_ep = defaultdict(list)
        for r in R:
            by_ep[r["endpoint"]].append(float(r["ms"]))
        sig = [float(s["delivery_ms"]) for s in sigs if int(s["step"]) == n]
        # отставание расшифровки: от отправки куска до готового текста
        lags, lag_first, lag_second, not_done = [], [], [], 0
        S = sorted([s for s in sends if int(s["step"]) == n and s["status"] == "202"], key=lambda s: float(s["sent"]))
        mid = (st["t_start"] + st["t_load_end"]) / 2
        for s in S:
            appt = code2appt.get(s["room"])
            seq = int(s["key"].rsplit("-", 1)[1])
            done = seg_done.get((appt, s["role"], seq))
            if done is None:
                not_done += 1
                continue
            lag = done - float(s["sent"])
            lags.append(lag)
            (lag_first if float(s["sent"]) < mid else lag_second).append(lag)
        deid_after = [deid_ready[a] - ended[a] for a in ended if a in deid_ready]
        prints = by_ep.get("POST /rooms/{c}/render-conclusion", [])
        analyze = [x["analyze_s"] for x in st["infos"] if "analyze_s" in x]
        RS = [r for r in res if int(r["step"]) == n]
        cpu = lambda k: [float(r[k]) for r in RS if r.get(k) not in (None, "")]  # noqa: E731
        services = ["gateway", "database", "speech", "assistant", "vision", "reports", "ollama", "generator"]
        qmax = defaultdict(int)
        for q in queue:
            if int(q["step"]) == n and q["status"] == "queued":
                qmax[q["kind"]] = max(qmax[q["kind"]], int(q["n"]))
        row = {
            "pairs": n, "requests": len(R), "rps": r1(len(R) / window) if window else None, "window_s": r1(window),
            "api_p50_ms": r1(pct(api, 50)), "api_p95_ms": r1(pct(api, 95)), "api_p99_ms": r1(pct(api, 99)),
            "error_pct": round(100 * len(errs) / len(R), 2) if R else None, "errors": len(errs), "degraded": len(deg),
            "error_kinds": sorted({"%s %s" % (e["endpoint"], e["status"]) for e in errs})[:8],
            "signal_p50_ms": r1(pct(sig, 50)), "signal_p95_ms": r1(pct(sig, 95)), "signals": len(sig),
            "chunks_sent": len(S), "chunks_not_transcribed_at_end": not_done,
            "lag_p50_s": r1(pct(lags, 50)), "lag_p95_s": r1(pct(lags, 95)), "lag_max_s": r1(max(lags) if lags else None),
            "lag_first_half_p50_s": r1(pct(lag_first, 50)), "lag_second_half_p50_s": r1(pct(lag_second, 50)),
            "deid_after_end_p50_s": r1(pct(deid_after, 50)), "deid_after_end_max_s": r1(max(deid_after) if deid_after else None),
            "deid_done": len(deid_after), "appointments": len(ended),
            "print_p95_ms": r1(pct(prints, 95)), "analyze_p50_s": r1(pct(analyze, 50)),
            "queue_max": dict(qmax), "pending_after_drain": st["pending_after_drain"],
            "system_cpu_mean": r1(statistics.mean(cpu("system_cpu"))) if cpu("system_cpu") else None,
            "avail_mb_min": min(int(float(r["avail_mb"])) for r in RS) if RS else None,
            "db_mb_end": float(RS[-1]["db_mb"]) if RS else None,
            "endpoints": {ep: {"n": len(v), "p50": r1(pct(v, 50)), "p95": r1(pct(v, 95)), "p99": r1(pct(v, 99))} for ep, v in sorted(by_ep.items())},
            "cpu_mean": {s: r1(statistics.mean(cpu(s + "_cpu"))) for s in services if cpu(s + "_cpu")},
            "cpu_max": {s: r1(max(cpu(s + "_cpu"))) for s in services if cpu(s + "_cpu")},
            "rss_max_mb": {s: r1(max(cpu(s + "_mb"))) for s in services if cpu(s + "_mb")},
        }
        checks = {"api_p95_ms": row["api_p95_ms"], "signal_p95_ms": row["signal_p95_ms"], "error_pct": row["error_pct"],
                  "lag_p95_s": row["lag_p95_s"], "deid_after_end_s": row["deid_after_end_max_s"], "print_p95_ms": row["print_p95_ms"]}
        row["violations"] = [k for k, v in checks.items() if v is None and k in ("lag_p95_s",) or (v is not None and v > LIMITS[k])]
        if row["chunks_not_transcribed_at_end"] and "lag_p95_s" not in row["violations"]:
            row["violations"].append("lag_p95_s")  # не расшифрованные к концу замера куски тоже считаются нарушением
        if row["appointments"] and row["deid_done"] < row["appointments"] and "deid_after_end_s" not in row["violations"]:
            row["violations"].append("deid_after_end_s")
        summary.append(row)

    base = summary[0]
    keys = ["api_p50_ms", "api_p95_ms", "api_p99_ms", "signal_p95_ms", "lag_p50_s", "lag_p95_s", "print_p95_ms", "analyze_p50_s", "system_cpu_mean"]
    for row in summary:
        row["degradation_pct"] = {k: (round((row[k] - base[k]) / base[k] * 100, 1) if row[k] is not None and base[k] else None) for k in keys}
    whisper = [x["elapsed"] / x["duration"] for x in jres["transcribe"] if x.get("duration")]
    deid_el = [x.get("elapsed") for x in jres["deidentify"] if x.get("elapsed")]
    totals = {"whisper_rtf_p50": round(pct(whisper, 50), 3) if whisper else None, "whisper_rtf_p95": round(pct(whisper, 95), 3) if whisper else None,
              "whisper_chunks": len(whisper), "deid_elapsed_p50_s": r1(pct(deid_el, 50)), "deid_elapsed_max_s": r1(max(deid_el) if deid_el else None),
              "deid_jobs": len(deid_el), "limits": LIMITS,
              "max_ok_pairs": max([r["pairs"] for r in summary if not r["violations"]], default=0)}
    (RES / "summary.json").write_text(json.dumps({"steps": summary, "totals": totals, "meta": {k: v for k, v in meta.items() if k != "steps"}},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    charts(summary)
    print(json.dumps(totals, ensure_ascii=False))
    for r in summary:
        print(r["pairs"], {k: r[k] for k in ("api_p95_ms", "signal_p95_ms", "error_pct", "lag_p50_s", "lag_p95_s", "chunks_not_transcribed_at_end",
                                            "deid_done", "appointments", "print_p95_ms", "system_cpu_mean", "violations")})


def charts(summary):
    N = [r["pairs"] for r in summary]

    def line(fname, title, ylabel, series, limit=None):
        fig, ax = plt.subplots(figsize=(6.4, 3.8))
        for lbl, ys in series:
            pts = [(n, y) for n, y in zip(N, ys) if y is not None]
            if pts:
                ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", label=lbl)
        if limit:
            ax.axhline(limit, color="#B42318", linestyle="--", linewidth=1, label="порог")
        ax.set_xscale("log", base=2)
        ax.set_xticks(N)
        ax.set_xticklabels([str(n) for n in N])
        ax.set_xlabel("пар «врач + пациент» одновременно")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(alpha=.3)
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(RES / fname, dpi=130)
        plt.close(fig)

    line("api_latency.png", "Задержка API (без длинного опроса)", "мс",
         [("p50", [r["api_p50_ms"] for r in summary]), ("p95", [r["api_p95_ms"] for r in summary]), ("p99", [r["api_p99_ms"] for r in summary])], LIMITS["api_p95_ms"])
    line("signaling.png", "Доставка сообщения сигнализации", "мс",
         [("p50", [r["signal_p50_ms"] for r in summary]), ("p95", [r["signal_p95_ms"] for r in summary])], LIMITS["signal_p95_ms"])
    line("transcript_lag.png", "Отставание расшифровки речи", "с",
         [("p50", [r["lag_p50_s"] for r in summary]), ("p95", [r["lag_p95_s"] for r in summary])], LIMITS["lag_p95_s"])
    svcs = ["gateway", "database", "speech", "assistant", "ollama", "vision", "generator"]
    line("cpu.png", "Средняя загрузка CPU (100% = одно ядро)", "% CPU",
         [(s, [r["cpu_mean"].get(s) for r in summary]) for s in svcs])


if __name__ == "__main__":
    main()
