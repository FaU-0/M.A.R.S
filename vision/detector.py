# -*- coding: utf-8 -*-
"""Поиск точек лица на снимке через MediaPipe (Python). Необязательная часть:
если пакет mediapipe не установлен (например, на Mac с Intel и Python 3.12 его нет),
сервис работает по точкам, которые находит браузер, а /v1/detect отвечает detector_unavailable.

Модель: DATA_DIR/models/face_landmarker.task (или путь из FACE_MODEL).
"""
import os
import threading

from shared import config

_LOCK = threading.Lock()
_STATE = {"landmarker": None, "error": None, "tried": False}


def model_path():
    env = os.environ.get("FACE_MODEL")
    if env:
        return env
    p = config.data_dir() / "models" / "face_landmarker.task"
    if not p.exists():
        alt = config.data_dir() / "vendor" / "face_landmarker.task"  # gateway уже скачивает её для браузера
        if alt.exists():
            return str(alt)
    return str(p)


def _load():
    with _LOCK:
        if _STATE["tried"]:
            return _STATE["landmarker"]
        _STATE["tried"] = True
        try:
            import mediapipe as mp  # noqa: WPS433
            from mediapipe.tasks.python import vision as mpv
            from mediapipe.tasks.python.core.base_options import BaseOptions
        except Exception as e:  # noqa: BLE001
            _STATE["error"] = "mediapipe не установлен: %s" % e
            return None
        path = model_path()
        if not os.path.exists(path):
            _STATE["error"] = "нет файла модели %s" % path
            return None
        try:
            opts = mpv.FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path=path), running_mode=mpv.RunningMode.IMAGE,
                                             num_faces=2, output_face_blendshapes=True, output_facial_transformation_matrixes=True)
            _STATE["landmarker"] = (mp, mpv.FaceLandmarker.create_from_options(opts))
        except Exception as e:  # noqa: BLE001
            _STATE["error"] = "не удалось загрузить модель: %s" % e
        return _STATE["landmarker"]


def available():
    return _load() is not None


def status():
    ok = available()
    return {"detector": ok} if ok else {"detector": False, "detector_error": _STATE["error"]}


def detect(jpeg_bytes):
    """Кадр в формате metrics.Frame для самого крупного лица или None, если лица нет."""
    lm = _load()
    if lm is None:
        return None
    mp, landmarker = lm
    import numpy as np  # идёт вместе с mediapipe
    import cv2  # noqa: WPS433

    img = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Не удалось прочитать JPEG")
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w = img.shape[:2]
    with _LOCK:
        r = landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=img))
    faces = r.face_landmarks or []
    if not faces:
        return None

    def area(f):
        xs, ys = [p.x for p in f], [p.y for p in f]
        return (max(xs) - min(xs)) * (max(ys) - min(ys))

    idx = max(range(len(faces)), key=lambda i: area(faces[i]))
    mats, bs = r.facial_transformation_matrixes, r.face_blendshapes
    # MediaPipe Python отдаёт матрицу по строкам, браузер - по столбцам (column-major): приводим к виду браузера
    mat = [float(v) for v in np.asarray(mats[idx]).T.flatten()] if mats else None
    return {"lm": [[p.x, p.y, p.z] for p in faces[idx]], "mat": mat,
            "bs": {c.category_name: float(c.score) for c in bs[idx]} if bs else {},
            "vw": w, "vh": h, "multi": len(faces) > 1}
