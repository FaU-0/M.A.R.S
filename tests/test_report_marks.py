# -*- coding: utf-8 -*-
"""Пометки отчёта по лицу из метрологии (gateway/src/d_face2.js): «в пределах погрешности» и индекс асимметрии подвижности.

    python tests/test_report_marks.py
Нужен node. Код берётся из исходника клиента как есть: блок от SYM_NOISE до объявления fx.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILS = []


def check(c, m):
    print(("OK    " if c else "FAIL  ") + m)
    if not c:
        FAILS.append(m)


def main():
    src = (ROOT / "gateway" / "src" / "d_face2.js").read_text(encoding="utf-8")
    i, j = src.index("const SYM_NOISE"), src.index("const fx =")
    block = src[i:j]
    js = block + """
const cases = {
  brow_small: inNoise('brow', 0.5 / 63), brow_big: inNoise('brow', 1.5 / 63), brow_neg_small: inNoise('brow', -0.5 / 63),
  tilt_small: inNoise('tilt', 0.3), tilt_big: inNoise('tilt', 0.8), unknown: inNoise('xyz', 0), nullv: inNoise('brow', null),
  index: mobIndex({ tasks: [{ id: 'brow', ratio: 0.5 }, { id: 'smile', ratio: 0.5 }, { id: 'eye', ratio: 0.8 }, { id: 'lips', ratio: 0.1 }] }),
  index_missing: mobIndex({ tasks: [{ id: 'brow', ratio: 0.5 }, { id: 'smile', ratio: null }, { id: 'eye', ratio: 0.8 }] }),
  index_symmetric: mobIndex({ tasks: [{ id: 'brow', ratio: 1 }, { id: 'smile', ratio: 1 }, { id: 'eye', ratio: 1 }] }),
  normal_max: MOB_INDEX_NORMAL_MAX
};
process.stdout.write(JSON.stringify(cases));
"""
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode:
        check(False, "блок порогов из d_face2.js выполняется в node: %s" % r.stderr[:200])
        return
    c = json.loads(r.stdout)
    check(c["brow_small"] is True and c["brow_neg_small"] is True, "разность бровей 0,5 мм (любого знака) — в пределах погрешности")
    check(c["brow_big"] is False, "разность бровей 1,5 мм — выше погрешности")
    check(c["tilt_small"] is True and c["tilt_big"] is False, "наклон линии глаз: 0,3° в пределах, 0,8° — нет")
    check(c["unknown"] is False and c["nullv"] is False, "неизвестная метрика и пустое значение не помечаются")
    check(abs(c["index"] - (0.5 + 0.5 + 0.2) / 3) < 1e-9, "индекс = среднее (1 − соотношение) по бровям, улыбке, глазам; губы не входят")
    check(c["index_missing"] is None, "без одного из трёх заданий индекс не считается")
    check(c["index_symmetric"] == 0, "одинаковые стороны — индекс 0")
    check(c["normal_max"] == 0.27, "ориентир по здоровым из MEEI — 0,27")


if __name__ == "__main__":
    main()
    print("\nИтог: %s" % ("всё прошло" if not FAILS else "ошибок %d" % len(FAILS)))
    sys.exit(1 if FAILS else 0)
