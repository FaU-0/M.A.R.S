"""Английская версия интерфейса: у каждой строки T('...') в gateway/src есть перевод в словаре EN (c_i18n.js).

Запуск: python tests/test_i18n.py
"""
import json
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "gateway" / "src"
CYR = re.compile("[А-Яа-яЁё]")
FAILED = []
# названия языков на кнопке переключателя пишутся на самом языке и не переводятся
LANG_NAMES = {"Русский"}


def check(ok, msg):
    print(("OK    " if ok else "FAIL  ") + msg)
    if not ok:
        FAILED.append(msg)


def dictionary():
    text = (SRC / "c_i18n.js").read_text(encoding="utf-8")
    body = text[text.index("const EN = {") + len("const EN = "):text.index("\n};", text.index("const EN = {")) + 2]
    return json.loads(body.replace(",\n}", "\n}"))


def main():
    en = dictionary()
    check(len(en) > 700, "словарь EN загружен: %d строк" % len(en))
    check(all(not CYR.search(v) for v in en.values()), "в переводах нет кириллицы")
    used, missing, bare = set(), [], []
    for p in sorted(SRC.glob("*.js")):
        if p.name == "c_i18n.js":
            continue
        code = p.read_text(encoding="utf-8")
        for m in re.finditer(r"T\('((?:[^'\\\n]|\\.)*)'\)", code):
            used.add(m.group(1))
            if CYR.search(m.group(1)) and m.group(1) not in en:
                missing.append("%s: %s" % (p.name, m.group(1)))
        stripped = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
        stripped = re.sub(r"(?m)//[^\n]*", "", stripped)
        for m in re.finditer(r"(?<!T\()'((?:[^'\\\n]|\\.)*[А-Яа-яЁё](?:[^'\\\n]|\\.)*)'", stripped):
            if len(m.group(1)) > 1 and m.group(1) not in LANG_NAMES:  # одиночные буквы — горячие клавиши в русской раскладке
                bare.append("%s: %s" % (p.name, m.group(1)))
    check(not missing, "у каждой строки T('...') есть перевод" + ("" if not missing else ": нет для " + "; ".join(missing[:5])))
    check(not bare, "русские строки интерфейса не выводятся мимо T()" + ("" if not bare else ": " + "; ".join(bare[:5])))
    print("  (строк в интерфейсе через T: %d)" % len(used))
    print("\nИтог: " + ("всё прошло" if not FAILED else "есть ошибки: %d" % len(FAILED)))
    raise SystemExit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
