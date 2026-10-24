# -*- coding: utf-8 -*-
"""Оценка обезличивания на 20 синтетических диалогах (tests/deid_dialogs.py) с настоящей Ollama.

    python tests/eval_deid.py [модель]          по умолчанию OLLAMA_MODEL или qwen2.5:3b
Печатает полноту по видам данных и сохранённость медицинских слов. Порог задаётся в test_assistant.py.
"""
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
if len(sys.argv) > 1:
    os.environ["OLLAMA_MODEL"] = sys.argv[1]
import deid_dialogs  # noqa: E402
from assistant import deid, main as assistant  # noqa: E402


def leaked(frag, kind, text):
    if len(frag) < 3:  # короткая фамилия («Ли»): отдельное слово с заглавной, а не частица «ли»
        return re.search(r"(?<![\w])%s(?![\w])" % re.escape(frag), text) is not None
    low = text.lower()
    if frag.lower() in low:
        return True
    if any(run in text for run in re.findall(r"\d{3,}", frag)):
        return True
    if kind == "entity":  # осталось ли хоть одно имя собственное из фрагмента
        for w in re.findall(r"[«\"]?([А-ЯЁA-Z][\w-]+)", frag):
            if re.search(r"(?<![\w])%s" % re.escape(deid.stem(w)), low.replace("ё", "е")) and len(deid.stem(w)) >= 3:
                return True
    return False


def evaluate(llm=True, dialogs=None):
    dialogs = dialogs or deid_dialogs.make()
    tot = {"structured": [0, 0], "entity": [0, 0]}
    keep_ok = keep_n = 0
    misses, lost, extra, t0 = [], [], [], time.time()
    for i, d in enumerate(dialogs):
        out, repl, st = deid.deidentify(d["segments"], d["known"], llm=assistant.find_entities if llm else None)
        text = "\n".join(s["text"] for s in out)
        for frag, kind in d["pii"]:
            tot[kind][1] += 1
            if leaked(frag, kind, text):
                misses.append((i, kind, frag))
            else:
                tot[kind][0] += 1
        # лишние замены: оригинал под меткой не входит ни в один размеченный фрагмент
        pii_stems = {deid.stem(w) for f, _ in d["pii"] for w in re.findall(r"\w+", f)}
        pii_stems |= {deid.stem(w) for v in (d["known"].get("patient") or {}).values() for w in re.findall(r"\w+", str(v))}
        pii_stems |= {deid.stem(w) for v in (d["known"].get("doctor") or {}).values() for w in re.findall(r"\w+", str(v))}
        for r in repl:
            for o in r["originals"]:
                if not any(deid.stem(w) in pii_stems for w in re.findall(r"\w+", o)):
                    extra.append((i, r["label"], o))
        for k in d["keep"]:
            keep_n += 1
            if k.lower() in text.lower():
                keep_ok += 1
            else:
                lost.append((i, k))
    return {"structured": tot["structured"][0] / tot["structured"][1], "entity": tot["entity"][0] / tot["entity"][1],
            "keep": keep_ok / keep_n, "misses": misses, "lost": lost, "extra": extra, "secs": time.time() - t0, "n": len(dialogs),
            "counts": tot, "model": assistant.MODEL if llm else "без нейросети"}


def show(r):
    print("модель: %s, диалогов %d, %.0f с (%.1f с на диалог)" % (r["model"], r["n"], r["secs"], r["secs"] / r["n"]))
    print("  структурированные данные удалены: %.1f%% (%d из %d)" % (100 * r["structured"], *r["counts"]["structured"]))
    print("  имена, места, организации удалены: %.1f%% (%d из %d)" % (100 * r["entity"], *r["counts"]["entity"]))
    print("  медицинские слова сохранены: %.1f%%" % (100 * r["keep"]))
    print("  лишних замен (скрыто не то): %d" % len(r["extra"]))
    for m in r["misses"]:
        print("    пропущено: диалог %d, %s: %s" % m)
    for m in r["lost"]:
        print("    потеряно медицинское слово: диалог %d: %s" % m)
    for m in r["extra"]:
        print("    лишняя замена: диалог %d, %s: %s" % m)


if __name__ == "__main__":
    import deid_dialogs as dd
    show(evaluate(llm=False))
    show(evaluate(llm=True))
    print("--- отложенный набор ---")
    show(evaluate(llm=True, dialogs=dd.make_holdout()))
