# -*- coding: utf-8 -*-
"""Цифровой фантом: точность, линейность, повторяемость и устойчивость метрик симметрии без живых людей.

1. Симметричный фантом: снимок здорового человека (анфас, покой) выравнивается по линии зрачков,
   одна половина лица отражается на другую. Истинная асимметрия = 0.
2. Заданная асимметрия: область брови (точка 105) или уголка рта (точка 61) плавно сдвигается
   на d мм (гауссово поле смещений). Истинный сдвиг каждой точки известен из формулы деформации.
3. Одно измерение — как в приложении: медиана по 12 кадрам. Кадры — копии снимка со случайным шумом
   сенсора, яркостью, сжатием JPEG и сдвигом/масштабом на доли пикселя (дрожание камеры).
4. Устойчивость: фантом без асимметрии в плохих условиях (темно, шум, сильное сжатие, низкое разрешение, наклон).

Миллиметры: доли МЗР × 63 мм (средний МЗР взрослого). В приложении мм считаются по калибровке.

    vision/.venv/bin/python -m metrology.phantom [папка_снимков] [--quick]
Результат: metrology/results/phantom.json
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from metrology import common as C

IPD_MM = 63.0
LEVELS_MM = [0.5, 1.0, 2.0, 3.0, 4.0]
REPEATS = 8
# область сдвига: вся бровь (вытянута вдоль неё) или угол рта; σ в долях МЗР по x и y
REGIONS = {
    "brow": {"points": (70, 63, 105, 66, 107), "sigma": (0.30, 0.12), "dir": (0.0, -1.0), "metric": "brow", "label": "вся бровь вверх"},
    "mouth": {"points": (61,), "sigma": (0.15, 0.15), "dir": (0.0, -1.0), "metric": "mcor", "label": "угол рта вверх"},
}
CONDITIONS = {
    "dark": "темно (яркость −45 %)",
    "bright": "пересвет (+35 %)",
    "noise": "сильный шум (σ = 12)",
    "jpeg": "сильное сжатие (JPEG 35)",
    "lowres": "низкое разрешение (МЗР 60 пикселей)",
    "roll": "наклон головы в плоскости 6°",
}
OUT = C.ROOT / "metrology" / "results"


def landmarks_px(frame: dict) -> np.ndarray:
    return np.array([[p[0] * frame["vw"], p[1] * frame["vh"]] for p in frame["lm"]])


def symmetric_phantom(img: np.ndarray, side: str = "left") -> tuple[np.ndarray, float] | None:
    """Выравнивание по зрачкам и отражение половины лица. Возвращает (фантом, МЗР в пикселях)."""
    fr = C.frame_of(img)
    if fr is None:
        return None
    P = landmarks_px(fr)
    r, l = P[468], P[473]
    ang = np.degrees(np.arctan2(l[1] - r[1], l[0] - r[0]))
    mid = (r + l) / 2
    M = cv2.getRotationMatrix2D((float(mid[0]), float(mid[1])), float(ang), 1.0)
    rot = cv2.warpAffine(img, M, (img.shape[1], img.shape[0]), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
    cx = int(round(mid[0]))
    w = min(cx, rot.shape[1] - cx)
    half = rot[:, cx - w:cx] if side == "left" else rot[:, cx:cx + w][:, ::-1]
    out = np.concatenate([half, half[:, ::-1]], axis=1)
    return out, float(np.hypot(*(l - r)))


def weight(x, y, center: np.ndarray, sigma_px: tuple):
    """Вес деформации: гауссиана вокруг center с полуосями σx, σy."""
    return np.exp(-((x - center[0]) ** 2 / (2 * sigma_px[0] ** 2) + (y - center[1]) ** 2 / (2 * sigma_px[1] ** 2)))


def warp(img: np.ndarray, center: np.ndarray, direction: tuple, d_px: float, sigma_px: tuple) -> np.ndarray:
    """Плавный сдвиг области: точка p переходит в p + d·w(p)·dir."""
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    # обратное отображение: для пикселя результата берём исходный пиксель, сдвинутый назад (сдвиг мал относительно σ)
    wgt = weight(xx, yy, center, sigma_px)
    mapx = xx - d_px * wgt * direction[0]
    mapy = yy - d_px * wgt * direction[1]
    return cv2.remap(img, mapx, mapy, interpolation=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)


def truth(P: np.ndarray, center: np.ndarray, direction: tuple, d_px: float, sigma_px: tuple, ipd_px: float, metric: str) -> float:
    """Истинное изменение разности «правая − левая» в долях МЗР по сдвигам точек из формулы деформации."""
    def moved(i):
        wgt = weight(P[i][0], P[i][1], center, sigma_px)
        return P[i] + d_px * wgt * np.array(direction)
    Q = P.copy()
    for i in (468, 473, 105, 334, 61, 291):
        Q[i] = moved(i)

    def height_diff(A, ri, li):
        R, L = A[468], A[473]
        u = (L - R) / np.linalg.norm(L - R)
        n = np.array([-u[1], u[0]])
        Mi = (R + L) / 2
        ipd = np.linalg.norm(L - R)
        return (-np.dot(A[ri] - Mi, n) + np.dot(A[li] - Mi, n)) / ipd

    pts = (105, 334) if metric == "brow" else (61, 291)
    return float(height_diff(Q, *pts) - height_diff(P, *pts))


def jitter(img: np.ndarray, rng: random.Random, cond: str | None = None, ipd_px: float = 120.0) -> tuple[np.ndarray, int]:
    """Один «кадр»: шум сенсора, яркость, дрожание камеры; cond — плохие условия съёмки."""
    h, w = img.shape[:2]
    s = 1 + rng.uniform(-0.01, 0.01)
    M = cv2.getRotationMatrix2D((w / 2, h / 2), rng.uniform(-0.3, 0.3) + (6.0 if cond == "roll" else 0.0), s)
    M[:, 2] += (rng.uniform(-1, 1), rng.uniform(-1, 1))
    out = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT).astype(np.float32)
    gain = 1 + rng.uniform(-0.05, 0.05)
    sigma = 2.5
    q = rng.randint(85, 95)
    if cond == "dark":
        gain *= 0.55
    elif cond == "bright":
        gain *= 1.35
    elif cond == "noise":
        sigma = 12.0
    elif cond == "jpeg":
        q = 35
    out = out * gain + np.random.default_rng(rng.randrange(1 << 30)).normal(0, sigma, out.shape)
    out = np.clip(out, 0, 255).astype(np.uint8)
    if cond == "lowres":
        k = 60.0 / max(1.0, ipd_px)
        if k < 1:
            small = cv2.resize(out, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA)
            out = cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
    return out, q


def measure(img: np.ndarray, rng: random.Random, cond: str | None = None, ipd_px: float = 120.0) -> tuple[dict | None, int]:
    """Одно измерение: медиана по 12 кадрам в допуске позы. Возвращает (разности, отброшено кадров)."""
    samples, rejected, tries = [], 0, 0
    while len(samples) < C.FRAMES_PER_MEASUREMENT and tries < 3 * C.FRAMES_PER_MEASUREMENT:
        tries += 1
        frame_img, q = jitter(img, rng, cond, ipd_px)
        fr = C.frame_of(frame_img, q)
        s = C.sym(fr) if fr else None
        if s is None:
            rejected += 1
            continue
        samples.append(s)
    if len(samples) < C.FRAMES_PER_MEASUREMENT:
        return None, rejected
    return C.diffs(C.measurement(samples)), rejected


def run_subject(path: Path, rng: random.Random, repeats: int, levels: list[float], conds: list[str]) -> dict | None:
    base = C.read(path)
    ph = symmetric_phantom(base, "left")
    if ph is None:
        return None
    img, ipd_px = ph
    fr = C.frame_of(img)
    if fr is None or C.sym(fr) is None:
        return {"subject": path.stem, "error": "фантом вне допуска позы или лицо не найдено"}
    P = landmarks_px(fr)
    res = {"subject": path.stem, "ipd_px": ipd_px, "zero": [], "induced": {}, "conditions": {}, "rejected": 0}
    for _ in range(repeats):
        m, rej = measure(img, rng)
        res["rejected"] += rej
        if m:
            res["zero"].append(m)
    for reg, spec in REGIONS.items():
        center = P[list(spec["points"])].mean(axis=0)
        sigma = (spec["sigma"][0] * ipd_px, spec["sigma"][1] * ipd_px)
        res["induced"][reg] = []
        for mm in levels:
            d_px = mm / IPD_MM * ipd_px
            warped = warp(img, center, spec["dir"], d_px, sigma)
            gt = truth(P, center, spec["dir"], d_px, sigma, ipd_px, spec["metric"])
            for _ in range(repeats):
                m, rej = measure(warped, rng)
                res["rejected"] += rej
                if m:
                    res["induced"][reg].append({"mm": mm, "truth": gt, "measured": m[spec["metric"]], "all": m})
    for cond in conds:
        res["conditions"][cond] = []
        for _ in range(repeats):
            m, rej = measure(img, rng, cond, ipd_px)
            res["rejected"] += rej
            res["conditions"][cond].append(m)
    return res


def main() -> None:
    C.need_detector()
    quick = "--quick" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    src = Path(args[0]) if args else C.ROOT / "datasets" / "meei" / "Normals"
    photos = [ph for ph in (C.photo(d, 1) for d in sorted(src.iterdir()) if d.is_dir()) if ph]
    if quick:
        photos = photos[:1]
    rng = random.Random(20261120)
    repeats = 3 if quick else REPEATS
    levels = [1.0, 4.0] if quick else LEVELS_MM
    conds = ["noise"] if quick else list(CONDITIONS)
    out = {"method": __doc__.strip().splitlines()[0], "ipd_mm": IPD_MM, "repeats": repeats, "levels_mm": levels,
           "frames_per_measurement": C.FRAMES_PER_MEASUREMENT, "pose_tol": C.POSE_TOL, "subjects": []}
    t0 = time.time()
    for i, p in enumerate(photos, 1):
        r = run_subject(p, rng, repeats, levels, conds)
        if r:
            out["subjects"].append(r)
        print("%d/%d %s: %s, %.0f с" % (i, len(photos), p.stem, r.get("error", "готово") if r else "лицо не найдено",
                                         time.time() - t0), flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    name = "phantom_quick.json" if quick else "phantom.json"
    (OUT / name).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("сохранено: metrology/results/%s" % name)


if __name__ == "__main__":
    main()
