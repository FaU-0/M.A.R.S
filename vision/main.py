# -*- coding: utf-8 -*-
"""Сервис vision: метрики лица (симметрия, подвижность, сравнение до и после, калибровка). Состояния не хранит.

Точки лица находит браузер (живая сетка остаётся там) и присылает их вместе с запросом.
Если установлен mediapipe, сервис сам находит точки на снимке (/v1/detect, поле image_b64 в /v1/compare).
"""
import base64
import binascii
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.errors import error_body  # noqa: E402
from shared.service_base import Service  # noqa: E402
from vision import detector, metrics  # noqa: E402

SVC = "vision"


def _tol(data):
    try:
        return max(1.0, min(15.0, float(data.get("tolerance", 5))))
    except (TypeError, ValueError):
        return 5.0


def _cal(data):
    cal = data.get("calibration")
    return cal if isinstance(cal, dict) and cal.get("source") else None


def _bad(e):
    return 422, error_body(e.code, e.message, SVC)


def _need_obj(data):
    if not isinstance(data, dict):
        raise metrics.MetricsError("bad_request", "Ожидается объект")


def _frame_of(side):
    """Кадр снимка: готовые точки (frame) или сам снимок (image_b64), если есть детектор."""
    if not isinstance(side, dict):
        raise metrics.MetricsError("bad_request", "Нет данных снимка")
    if isinstance(side.get("frame"), dict):
        return side["frame"]
    if side.get("image_b64"):
        if not detector.available():
            raise metrics.MetricsError("detector_unavailable", "Поиск точек на сервере недоступен: нужны точки из браузера")
        try:
            raw = base64.b64decode(side["image_b64"], validate=True)
        except (binascii.Error, ValueError):
            raise metrics.MetricsError("bad_image", "Снимок не в base64")
        f = detector.detect(raw)
        if f is None:
            raise metrics.MetricsError("face_not_found", "Лицо не найдено. Если снимок сохранён со скрытым лицом, анализировать его нельзя")
        return f
    raise metrics.MetricsError("bad_request", "Нужен frame или image_b64")


def r_symmetry(data, rid):
    try:
        _need_obj(data)
        return 200, metrics.symmetry(data.get("frames"), _tol(data), _cal(data))
    except metrics.MetricsError as e:
        return _bad(e)


def r_mobility(data, rid):
    try:
        _need_obj(data)
        return 200, metrics.mobility(data.get("tasks"), _tol(data))
    except metrics.MetricsError as e:
        return _bad(e)


def r_compare(data, rid):
    try:
        _need_obj(data)
        res = metrics.compare(_frame_of(data.get("before")), _frame_of(data.get("after")),
                              str(data.get("pose") or "frontal"), _tol(data), _cal(data))
        for k in ("before", "after"):
            side = data.get(k) or {}
            res[k + "_id"], res[k + "_ts"] = side.get("id"), side.get("ts")
        return 200, res
    except metrics.MetricsError as e:
        return _bad(e)


def r_calibrate(data, rid):
    try:
        _need_obj(data)
        method = data.get("method")
        if method == "iris":
            return 200, {"calibration": metrics.calibrate_iris(data.get("frames"), _tol(data))}
        if method == "card":
            ipd = data.get("ipd_px")
            if ipd is None and isinstance(data.get("frame"), dict):
                ipd = metrics.ipd_px(metrics.Frame(data["frame"]))
            return 200, {"calibration": metrics.calibrate_card(data.get("pts") or [], ipd)}
        raise metrics.MetricsError("bad_method", "method: iris или card")
    except metrics.MetricsError as e:
        return _bad(e)


def r_detect(data, rid):
    try:
        _need_obj(data)
        return 200, {"frame": _frame_of({"image_b64": data.get("image_b64")})}
    except metrics.MetricsError as e:
        return _bad(e)


ROUTES = {
    ("POST", "/v1/symmetry"): r_symmetry,
    ("POST", "/v1/mobility"): r_mobility,
    ("POST", "/v1/compare"): r_compare,
    ("POST", "/v1/calibrate"): r_calibrate,
    ("POST", "/v1/detect"): r_detect,
}


def ready():
    # метрики готовы всегда; детектор необязателен и показывается для сведения
    return True, dict(detector.status(), metrics=True)


if __name__ == "__main__":
    Service(SVC, routes=ROUTES, ready_fn=ready).run()
