# -*- coding: utf-8 -*-
"""Расчёты метрологии (metrology/): статистика и эталон деформации фантома, без снимков и без MediaPipe.

    vision/.venv/bin/python tests/test_metrology.py
Нужны numpy и opencv (окружение vision/.venv). Без них тест пропускается.
"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FAILS = []


def check(c, m):
    print(("OK    " if c else "FAIL  ") + m)
    if not c:
        FAILS.append(m)


def main():
    try:
        import numpy as np
        from metrology import meei, phantom
    except ImportError as e:
        print("SKIP  нет numpy/opencv (%s): запускайте из vision/.venv" % e)
        return
    check(abs(meei.spearman([1, 2, 3, 4], [10, 20, 30, 40]) - 1) < 1e-9, "Спирмен: монотонный рост = 1")
    check(abs(meei.spearman([1, 2, 3, 4], [4, 3, 2, 1]) + 1) < 1e-9, "Спирмен: монотонное убывание = −1")
    check(abs(meei.spearman([1, 2, 2, 3], [1, 2, 2, 3]) - 1) < 1e-9, "Спирмен: повторы получают средний ранг")
    check(meei.auc([0.1, 0.2], [0.3, 0.4]) == 1.0, "AUC: все пациенты выше здоровых = 1")
    check(meei.auc([0.3], [0.3]) == 0.5, "AUC: ничья считается половиной")
    lo, hi = meei.wilson(8, 10)
    check(abs(lo - 0.490) < 0.005 and abs(hi - 0.943) < 0.005, "ДИ Уилсона для 8 из 10: 0,49–0,94")
    check(math.isnan(meei.wilson(0, 0)[0]), "ДИ Уилсона без наблюдений не определён")

    # эталон фантома на искусственных точках: зрачки (468, 473), брови (105, 334), уголки рта (61, 291)
    P = np.zeros((478, 2))
    P[468], P[473] = (100, 200), (200, 200)
    P[105], P[334] = (100, 150), (200, 150)
    P[61], P[291] = (110, 300), (190, 300)
    c = P[105].copy()
    check(abs(phantom.truth(P, c, (0, -1), 0.0, (30, 12), 100, "brow")) < 1e-12, "без сдвига истинная асимметрия = 0")
    t = phantom.truth(P, c, (0, -1), 5.0, (1e-3, 1e-3), 100, "brow")
    check(abs(t - 0.05) < 1e-9, "точечный подъём правой брови на 5 пикселей при МЗР 100 = +0,05 МЗР")
    t_wide = phantom.truth(P, c, (0, -1), 5.0, (30, 60), 100, "brow")
    check(0 < t_wide < 0.05, "широкая деформация задевает линию глаз: истинное изменение меньше сдвига")
    img = np.zeros((40, 40, 3), np.uint8)
    img[10:30, 18:22] = 255
    same = phantom.warp(img, np.array([20.0, 20.0]), (0, -1), 0.0, (5.0, 5.0))
    check(np.array_equal(same, img), "деформация с нулевым сдвигом не меняет снимок")
    check(abs(meei.amplitude("smile", np.zeros((478, 2)), np.full((478, 2), 0.3), "R") - math.hypot(0.3, 0.3)) < 1e-9,
          "амплитуда улыбки = длина смещения угла рта")


if __name__ == "__main__":
    main()
    print("\nИтог: %s" % ("всё прошло" if not FAILS else "ошибок %d" % len(FAILS)))
    sys.exit(1 if FAILS else 0)
