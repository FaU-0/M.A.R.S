"""Экспорт отчёта по лицу в HL7 FHIR R4 (FaceMesh.Fhir в d_face3.js).

Запуск: python tests/test_fhir.py
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILED = []
UCUM = "http://unitsofmeasure.org"


def check(ok, msg):
    print(("OK    " if ok else "FAIL  ") + msg)
    if not ok:
        FAILED.append(msg)


# Загружает d_face1.js и d_face3.js так же, как js_face_harness.js, и собирает пакет по каждому отчёту
NODE = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const src = process.argv[1];
const c1 = fs.readFileSync(path.join(src, 'c_js1.js'), 'utf8');
const helpers = ['const clamp', 'const median'].map(k => c1.split('\n').find(l => l.startsWith(k))).join('\n');
const ctx = { console, performance, Math, JSON, Object, Array, Date, Promise, Set, Map, setTimeout, clearTimeout, setInterval, clearInterval };
vm.createContext(ctx);
vm.runInContext(helpers + '\n' + fs.readFileSync(path.join(src, 'd_face1.js'), 'utf8') + '\n' + fs.readFileSync(path.join(src, 'd_face3.js'), 'utf8') + '\nthis.FaceMesh = FaceMesh;', ctx);
const inp = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(inp.map(rep => ctx.FaceMesh.Fhir.bundle(rep))));
"""


def report(cal=None):
    rows = [{"id": "brow", "label": "Высота бровей", "right": 0.31, "left": 0.25, "diff": 0.06}]
    sym = {"kind": "symmetry", "source": "vision", "ts": 1790000000, "rows": rows,
           "singles": [{"id": "tilt", "label": "Наклон линии глаз", "unit": "deg", "value": 2.1}]}
    if cal:
        rows[0].update(right_mm=19.2, left_mm=15.5, diff_mm=3.7)
        sym["calibration"] = cal
    mob = {"kind": "mobility", "ts": 1790000100, "tasks": [
        {"id": "smile", "label": "Улыбка", "right": 0.82, "left": 0.41, "ratio": 0.5},
        {"id": "eye", "label": "Закрыть глаза", "single": 0.77}]}
    return {"kind": "report", "patient": "Тестов Тест", "patient_id": 42, "appointment_id": 7, "ts": 1790000200,
            "symmetry": sym, "mobility": mob}


def run(cases):
    p = subprocess.run(["node", "-e", NODE, str(ROOT / "gateway" / "src")], input=json.dumps(cases), capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit("node: " + p.stderr[-800:])
    return json.loads(p.stdout)


def obs(bundle):
    return [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Observation"]


def comp(o, text):
    return next(c for c in o["component"] if c["code"]["text"] == text)


def main():
    cal = {"source": "card", "label": "по банковской карте 85,6 мм", "ipd_mm": 62.0}
    plain, mm, empty = run([report(), report(cal), {"kind": "report", "ts": 1790000200, "symmetry": None, "mobility": None}])

    check(plain["resourceType"] == "Bundle" and plain["type"] == "collection", "Bundle типа collection")
    check(all(e["fullUrl"].startswith("urn:uuid:") for e in plain["entry"]), "у каждой записи fullUrl вида urn:uuid")
    pats = [e for e in plain["entry"] if e["resource"]["resourceType"] == "Patient"]
    check(len(pats) == 1 and pats[0]["resource"]["identifier"][0]["value"] == "42", "один Patient с идентификатором из клиники")
    o = obs(plain)
    check(len(o) == 4, "4 наблюдения: бровь, наклон, улыбка, закрытие глаз")
    check(all(x["subject"]["reference"] == pats[0]["fullUrl"] for x in o), "каждое наблюдение ссылается на Patient")
    check(all(x["status"] == "final" and x["code"]["coding"][0]["system"] == "urn:mars:face-metric" for x in o), "статус final, коды из системы M.A.R.S.")
    brow = next(x for x in o if x["code"]["coding"][0]["code"] == "symmetry.brow")
    d = comp(brow, "Разность (правая − левая)")["valueQuantity"]
    check(d["system"] == UCUM and d["code"] == "1" and abs(d["value"] - 0.06) < 1e-9, "без калибровки: безразмерная доля МЗР (UCUM «1»)")
    check(brow["effectiveDateTime"].startswith("2026-09-21"), "время наблюдения из замера")
    check(any("не диагноз" in n["text"] for n in brow["note"]), "пометка «не диагноз» в наблюдении симметрии")
    tilt = next(x for x in o if x["code"]["coding"][0]["code"] == "symmetry.tilt")
    check(tilt["valueQuantity"]["code"] == "deg", "наклон в градусах (UCUM deg)")
    eye = next(x for x in o if x["code"]["coding"][0]["code"] == "mobility.eye")
    check("valueQuantity" in eye and "component" not in eye, "задание без разделения сторон: одно значение")
    smile = next(x for x in o if x["code"]["coding"][0]["code"] == "mobility.smile")
    check(any("клинической шкале" in n["text"] for n in smile["note"]), "пометка о клинической шкале в подвижности")

    b = next(x for x in obs(mm) if x["code"]["coding"][0]["code"] == "symmetry.brow")
    q = comp(b, "Разность (правая − левая)")["valueQuantity"]
    check(q["code"] == "mm" and abs(q["value"] - 3.7) < 1e-9, "с калибровкой: миллиметры (UCUM mm)")
    check("карте" in b["method"]["text"], "способ калибровки записан в method")

    check(obs(empty) == [] and not any(e["resource"]["resourceType"] == "Patient" for e in empty["entry"]),
          "пустой отчёт: нет наблюдений и нет пациента")

    print("\nИтог: " + ("всё прошло" if not FAILED else "есть ошибки: %d" % len(FAILED)))
    raise SystemExit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
