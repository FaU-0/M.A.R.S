# -*- coding: utf-8 -*-
"""Сервис speech: расшифровка кусков речи (faster-whisper на процессоре). Состояния не хранит:
очередь кусков на диске ведёт gateway, сюда приходит один кусок за запрос.

Настройки: WHISPER_MODEL (base, small, ...), WHISPER_THREADS, MODELS_DIR (по умолчанию DATA_DIR/models).
Модель скачивается при первом запуске; пока она не загружена, /ready отвечает 503.
"""
import base64
import binascii
import io
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import config  # noqa: E402
from shared.errors import error_body  # noqa: E402
from shared.service_base import Service  # noqa: E402

SVC = "speech"
MODEL = os.environ.get("WHISPER_MODEL", "small")
LANG = "ru"
MAX_AUDIO = 4 * 1024 * 1024
log = logging.getLogger("mars.speech")
_STATE = {"model": None, "error": None, "loading": False, "loaded_in": None}
_RUN = threading.Lock()  # один кусок за раз: процессор и так занят полностью


def models_dir():
    return os.environ.get("MODELS_DIR") or str(config.data_dir() / "models")


def load_model():
    _STATE["loading"] = True
    t = time.time()
    try:
        from faster_whisper import WhisperModel

        threads = int(os.environ.get("WHISPER_THREADS") or os.cpu_count() or 4)
        _STATE["model"] = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=threads, download_root=models_dir())
        _STATE["loaded_in"] = round(time.time() - t, 1)
        log.info("модель %s загружена за %.1f с", MODEL, time.time() - t)
    except Exception as e:  # noqa: BLE001
        _STATE["error"] = "%s: %s" % (type(e).__name__, e)
        log.error("не удалось загрузить модель %s: %s", MODEL, _STATE["error"])
    finally:
        _STATE["loading"] = False


def transcribe(raw):
    """Сегменты куска: [{start, end, text}], время в секундах от начала куска."""
    seg_iter, info = _STATE["model"].transcribe(
        io.BytesIO(raw), language=LANG, task="transcribe", beam_size=1, temperature=0.0,
        condition_on_previous_text=False,  # куски независимы: так ошибка одного не тянется в следующий
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},  # тишина вырезается, на паузах модель не выдумывает текст
        no_speech_threshold=0.6)
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in seg_iter if s.text.strip()]
    return segs, info.duration


def r_transcribe(data, rid):
    if not isinstance(data, dict) or not data.get("audio_b64"):
        return 400, error_body("bad_request", "Нужен audio_b64", SVC)
    if _STATE["model"] is None:
        return 503, error_body("not_ready", "Модель ещё не загружена", SVC)
    try:
        raw = base64.b64decode(data["audio_b64"], validate=True)
    except (binascii.Error, ValueError):
        return 400, error_body("bad_audio", "Аудио не в base64", SVC)
    if len(raw) > MAX_AUDIO:
        return 413, error_body("too_large", "Слишком большой кусок", SVC)
    t = time.time()
    with _RUN:
        try:
            segs, dur = transcribe(raw)
        except Exception as e:  # noqa: BLE001  (битый или пустой файл: PyAV не смог прочитать)
            log.warning("[%s] не удалось прочитать аудио: %s", rid, e)
            return 422, error_body("bad_audio", "Не удалось прочитать аудио", SVC)
    took = time.time() - t
    log.info("[%s] %.1f с аудио за %.1f с, сегментов %d", rid, dur, took, len(segs))
    return 200, {"segments": segs, "duration": round(dur, 2), "elapsed": round(took, 2), "model": MODEL, "language": LANG}


def ready():
    if _STATE["model"] is not None:
        return True, {"model": MODEL, "loaded_in": _STATE["loaded_in"]}
    return False, {"model": MODEL, "loading": _STATE["loading"], "error": _STATE["error"]}


if __name__ == "__main__":
    threading.Thread(target=load_model, daemon=True).start()
    Service(SVC, routes={("POST", "/v1/transcribe"): r_transcribe}, ready_fn=ready).run()
