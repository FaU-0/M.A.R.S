"""Проверка повторяемости: статистика серии замеров симметрии (FaceMesh.Repeat в d_face3.js).

Запуск: python tests/test_repeat.py
"""
import json
import math
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILED = []


def check(ok, msg):
    print(("OK    " if ok else "FAIL  ") + msg)
    if not ok:
        FAILED.append(msg)


# Загружает d_face1.js и d_face3.js так же, как js_face_harness.js, и считает статистику по входу
NODE = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const src = process.argv[1];
const c1 = fs.readFileSync(path.join(src, 'c_js1.js'), 'utf8');
const helpers = ['const clamp', 'const median'].map(k => c1.split('\n').find(l => l.startsWith(k))).join('\n');
const ctx = { console, performance, Math, JSON, Object, Array, Date, Promise, Set, Map, setTimeout, clearTimeout, setInterval, clearInterval };
vm.createContext(ctx);
vm.runInContext('const T = s => s, LANG = "ru", DEC = ",";\n' + helpers + '\n' + fs.readFileSync(path.join(src, 'd_face1.js'), 'utf8') + '\n' + fs.readFileSync(path.join(src, 'd_face3.js'), 'utf8') + '\nthis.FaceMesh = FaceMesh;', ctx);
const inp = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(inp.map(list => ({ stats: ctx.FaceMesh.Repeat.stats(list), csv: ctx.FaceMesh.Repeat.csv(list) }))));
"""


def meas(diffs, cal=None):
    """Замер симметрии с одной строкой «brow»: правая = 0.3 + diff, левая = 0.3."""
    out = []
    for d in diffs:
        row = {"id": "brow", "label": "Высота бровей", "right": 0.3 + d, "left": 0.3, "diff": d}
        m = {"kind": "symmetry", "rows": [row]}
        if cal:
            k = cal["ipd_mm"]
            row.update(right_mm=(0.3 + d) * k, left_mm=0.3 * k, diff_mm=d * k)
            m["calibration"] = cal
        out.append(m)
    return out


def run(cases):
    p = subprocess.run(["node", "-e", NODE, str(ROOT / "gateway" / "src")], input=json.dumps(cases), capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit("node: " + p.stderr[-800:])
    return json.loads(p.stdout)


def main():
    cal = {"label": "карта", "ipd_mm": 60.0}
    no_cal, with_cal, mixed, single = run([
        meas([0.10, 0.12, 0.14]),
        meas([0.10, 0.12, 0.14], cal),
        meas([0.10, 0.12], cal) + meas([0.14]),
        meas([0.10]),
    ])

    s = no_cal["stats"]
    r = s["rows"][0]
    check(s["n"] == 3 and s["mm"] is False, "3 замера без калибровки: n=3, единицы — доли МЗР")
    check(abs(r["diff"]["mean"] - 0.12) < 1e-9, "среднее разности 0,12")
    check(abs(r["diff"]["sd"] - 0.02) < 1e-9, "стандартное отклонение по выборке (n−1) 0,02")
    check(abs(r["diff"]["mdc"] - 1.96 * math.sqrt(2) * 0.02) < 1e-9, "минимальное различимое изменение = 1,96·√2·SD")
    check(abs(r["left"]["sd"]) < 1e-9 and abs(r["right"]["sd"] - 0.02) < 1e-9, "разброс считается и по каждой стороне")

    s = with_cal["stats"]
    r = s["rows"][0]
    check(s["mm"] is True and abs(r["diff"]["mean"] - 7.2) < 1e-9 and abs(r["diff"]["sd"] - 1.2) < 1e-9,
          "все замеры с калибровкой: статистика в миллиметрах (7,2 ± 1,2 мм)")

    check(mixed["stats"]["mm"] is False, "если хоть один замер без калибровки, миллиметры не используются")
    check(single["stats"]["rows"][0]["diff"]["sd"] is None, "по одному замеру разброс не считается")

    lines = no_cal["csv"].strip().split("\n")
    check(lines[0].startswith("замер;метрика;правая;левая;разность") and len([l for l in lines if l.startswith(("1;", "2;", "3;"))]) == 3,
          "CSV: заголовок и по строке на каждый замер")
    check(any(l.startswith("итог;") for l in lines) and "0,12" in no_cal["csv"], "CSV: итоговые строки с десятичной запятой")

    print("\nИтог: " + ("всё прошло" if not FAILED else "есть ошибки: %d" % len(FAILED)))
    raise SystemExit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
