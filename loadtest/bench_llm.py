# -*- coding: utf-8 -*-
"""Обезличивание: Ollama (локально) против GigaChat (облако) на одних и тех же синтетических диалогах.

    LLM_BACKEND=ollama   python loadtest/bench_llm.py [повторов]
    LLM_BACKEND=gigachat python loadtest/bench_llm.py [повторов]
    python loadtest/bench_llm.py --table          сводная таблица по сохранённым прогонам

Диалоги только синтетические (tests/deid_dialogs.py): в облако уходит текст, где имена ещё не скрыты.
Меряется каждый запрос к нейросети (окно текста до 700 символов) и каждый диалог целиком
(карточка + правила + нейросеть). Результат: loadtest/results_llm/<вариант>.json.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
OUT = ROOT / "loadtest" / "results_llm"


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def run(repeats: int) -> dict:
    sys.argv = sys.argv[:1]  # eval_deid при импорте читает sys.argv[1] как имя модели Ollama
    import deid_dialogs
    import eval_deid
    from assistant import main as assistant

    calls: list[dict] = []
    real = assistant.find_entities

    def timed(text: str):
        t = time.perf_counter()
        ok = True
        try:
            return real(text)
        except Exception:
            ok = False
            raise
        finally:
            calls.append({"s": time.perf_counter() - t, "chars": len(text), "ok": ok})

    assistant.find_entities = timed
    sets = {"main": deid_dialogs.make(), "holdout": deid_dialogs.make_holdout()}
    runs = []
    for rep in range(repeats):
        for name, dialogs in sets.items():
            start = len(calls)
            t = time.perf_counter()
            r = eval_deid.evaluate(llm=True, dialogs=dialogs)
            runs.append({"rep": rep, "set": name, "dialogs": r["n"], "wall_s": time.perf_counter() - t,
                         "structured": r["structured"], "entity": r["entity"], "keep": r["keep"],
                         "extra": len(r["extra"]), "misses": len(r["misses"]), "calls": len(calls) - start})
            print("повтор %d, %s: %.0f с, имена и места %.1f%%, мед. слова %.1f%%, лишних %d"
                  % (rep + 1, name, runs[-1]["wall_s"], 100 * r["entity"], 100 * r["keep"], len(r["extra"])), flush=True)
    secs = [c["s"] for c in calls if c["ok"]]
    per_dialog = [x["wall_s"] / x["dialogs"] for x in runs]
    return {"backend": assistant.BACKEND, "model": assistant.MODEL, "repeats": repeats, "runs": runs,
            "calls": len(calls), "failed_calls": sum(1 for c in calls if not c["ok"]),
            "call_p50_s": statistics.median(secs) if secs else 0, "call_p95_s": pct(secs, 0.95),
            "call_mean_s": statistics.mean(secs) if secs else 0, "dialog_mean_s": statistics.mean(per_dialog),
            "chars_per_s": sum(c["chars"] for c in calls if c["ok"]) / max(sum(secs), 1e-9),
            "usage": getattr(assistant.LLM, "usage", None) if assistant.BACKEND == "gigachat" else None,
            "raw_calls": calls}


def table() -> None:
    rows = [json.loads(p.read_text()) for p in sorted(OUT.glob("*.json"))]
    print("| Вариант | Модель | Запрос p50, с | Запрос p95, с | Диалог, с | Символов/с | Имена и места | Мед. слова | Лишние замены | Сбои | Токенов |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        q = [x for x in r["runs"]]
        ent = statistics.mean(x["entity"] for x in q)
        keep = statistics.mean(x["keep"] for x in q)
        extra = sum(x["extra"] for x in q) / r["repeats"]
        tokens = (r["usage"] or {}).get("total_tokens", "—")
        print("| %s | %s | %.1f | %.1f | %.1f | %.0f | %.1f %% | %.1f %% | %.1f | %d из %d | %s |"
              % (r["backend"], r["model"], r["call_p50_s"], r["call_p95_s"], r["dialog_mean_s"], r["chars_per_s"],
                 100 * ent, 100 * keep, extra, r["failed_calls"], r["calls"], tokens))


if __name__ == "__main__":
    if "--table" in sys.argv:
        table()
        sys.exit(0)
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    result = run(repeats)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / ("%s_%s.json" % (result["backend"], result["model"].replace(":", "-").replace("/", "-")))
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print("сохранено: %s" % path.relative_to(ROOT))
    table()
