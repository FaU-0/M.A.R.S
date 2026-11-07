# -*- coding: utf-8 -*-
"""Расшифровка одного куска речи. Отдельный модуль, чтобы процессы пула (режим «процессы», spawn)
импортировали только его, а не весь сервис с HTTP-сервером."""
import io
import os
import threading

LANG = "ru"
_W = {"model": None, "vad": True}


def vad_available():
    """Отсечение тишины в faster-whisper работает через onnxruntime; для Python 3.14t его сборки нет."""
    try:
        import onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def load(model_name, cpu_threads, models_dir, num_workers=1):
    from faster_whisper import WhisperModel

    _W["model"] = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=cpu_threads,
                               num_workers=num_workers, download_root=models_dir)
    _W["vad"] = vad_available()
    return _W["model"]


def init_process(model_name, cpu_threads, models_dir):
    """Инициализатор процесса пула: своя модель в каждом процессе."""
    load(model_name, cpu_threads, models_dir)


def transcribe(raw, model=None):
    """Сегменты куска: [{start, end, text}], длительность и кто считал (процесс и поток)."""
    m = model or _W["model"]
    vad = _W["vad"]
    seg_iter, info = m.transcribe(
        io.BytesIO(raw), language=LANG, task="transcribe", beam_size=1, temperature=0.0,
        condition_on_previous_text=False,  # куски независимы: так ошибка одного не тянется в следующий
        vad_filter=vad, vad_parameters={"min_silence_duration_ms": 500} if vad else None,  # тишина вырезается, на паузах модель не выдумывает текст
        no_speech_threshold=0.6)
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in seg_iter if s.text.strip()]
    return segs, info.duration, {"pid": os.getpid(), "thread": threading.current_thread().name, "vad": vad}


def ping(_i=None):
    """Готов ли процесс пула (модель загружена инициализатором). Аргумент — номер из pool.map, не нужен."""
    return os.getpid()
