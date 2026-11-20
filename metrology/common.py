# -*- coding: utf-8 -*-
"""Общее для метрологии: точки лица и метрики тем же кодом, что в приложении.

Точки ищет vision/detector.py (MediaPipe для Python), метрики считает vision/metrics.py —
те же формулы, что в браузере (совпадение проверяет tests/test_vision.py).
Запуск только из окружения vision/.venv:  vision/.venv/bin/python -m metrology.<скрипт>
"""
from __future__ import annotations

import math
import os
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FACE_MODEL", str(ROOT / "mars_data" / "vendor" / "face_landmarker.task"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from vision import detector, metrics  # noqa: E402

PAIR_KEYS = [(pid, a, b) for pid, _, a, b in metrics.PAIRS]
POSE_TOL = 5.0  # градусов, как в приложении по умолчанию
FRAMES_PER_MEASUREMENT = metrics.SYM_NEED  # приложение берёт медиану не меньше чем по 12 кадрам


def need_detector() -> None:
    if not detector.available():
        raise SystemExit("MediaPipe недоступен: %s. Запускайте из vision/.venv" % detector.status().get("detector_error"))


def encode(img: np.ndarray, quality: int = 95) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise ValueError("не удалось закодировать JPEG")
    return buf.tobytes()


def frame_of(img: np.ndarray, quality: int = 95) -> dict | None:
    """Кадр для vision/metrics.py или None, если лица нет."""
    return detector.detect(encode(img, quality))


def pose(frame: dict) -> dict | None:
    return metrics.euler(metrics.Frame(frame))


def sym(frame: dict, tol: float = POSE_TOL) -> dict | None:
    """Метрики одного кадра в долях МЗР; None, если поза вне допуска или точек не хватает."""
    f = metrics.Frame(frame)
    if not metrics.in_tol(metrics.euler(f), tol):
        return None
    return metrics.sym_one(f)


def diffs(s: dict) -> dict:
    """Разность правой и левой стороны по каждой паре + одиночные метрики, как в отчёте врача."""
    out = {pid: s[a] - s[b] for pid, a, b in PAIR_KEYS}
    out.update(tilt=s["tilt"], nose=s["nose"], chin=s["chin"])
    return out


def measurement(samples: list[dict]) -> dict:
    """Одно измерение = медиана по кадрам (как sym_build в приложении)."""
    keys = samples[0].keys()
    return {k: statistics.median(s[k] for s in samples) for k in keys}


def sd(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else float("nan")


def mdc95(sd_value: float) -> float:
    """Минимальное различимое изменение: 1,96 · √2 · SD (как в приложении, режим повторяемости)."""
    return 1.96 * math.sqrt(2) * sd_value


CAMERA_SIDE = 1920  # кадр камеры Full HD, как на приёме; снимки набора 6000×4000 уменьшаются до него


def read(path: Path, max_side: int = CAMERA_SIDE) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("не удалось прочитать %s" % path)
    k = max_side / max(img.shape[:2])
    if k < 1:
        img = cv2.resize(img, (round(img.shape[1] * k), round(img.shape[0] * k)), interpolation=cv2.INTER_AREA)
    return img


def photo(folder: Path, k: int) -> Path | None:
    """Снимок k человека: в наборе встречаются .JPG и вложенная папка redo, поэтому поиск без учёта регистра."""
    hits = sorted(p for p in folder.rglob("*") if p.is_file() and re.fullmatch(r".*_%d\.jpe?g" % k, p.name, re.I))
    return hits[-1] if hits else None  # redo — повторная съёмка, она последняя по пути
