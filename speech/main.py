# -*- coding: utf-8 -*-
"""Сервис speech: расшифровка кусков речи (faster-whisper на процессоре). Состояния не хранит:
очередь кусков на диске ведёт gateway, сюда приходит один кусок за запрос.

Настройки: WHISPER_MODEL (base, small, ...), WHISPER_THREADS (всего потоков ctranslate2), MODELS_DIR (по умолчанию
DATA_DIR/models), SPEECH_WORKERS (сколько кусков от разных пар расшифровывается одновременно, по умолчанию 2).
Параллельность выбирает shared/parallel.py: без GIL (Python 3.14t) — пул потоков с общей моделью, с GIL — пул
процессов, у каждого своя модель. Модель скачивается при первом запуске; пока пул не готов, /ready отвечает 503.
"""
import base64
import binascii
import concurrent.futures as cf
import logging
import multiprocessing
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import config, parallel  # noqa: E402
from shared.errors import error_body  # noqa: E402
from shared.service_base import Service  # noqa: E402
from speech import worker  # noqa: E402

SVC = "speech"
MODEL = os.environ.get("WHISPER_MODEL", "small")
LANG = "ru"
MAX_AUDIO = 4 * 1024 * 1024
log = logging.getLogger("mars.speech")
_STATE = {"pool": None, "mode": None, "workers": 0, "error": None, "loading": False, "loaded_in": None, "python": None}


def models_dir():
    return os.environ.get("MODELS_DIR") or str(config.data_dir() / "models")


def load_model():
    """Пул исполнителей. Режим выбирается после импорта faster-whisper: PyAV на 3.14t включает GIL обратно."""
    _STATE["loading"] = True
    t = time.time()
    try:
        import faster_whisper  # noqa: F401  (импорт до выбора режима, см. shared/parallel.py)

        n = parallel.workers()
        per = parallel.threads_per_worker(n)
        md = models_dir()
        mode = parallel.mode()
        if mode == parallel.THREADS:
            model = worker.load(MODEL, per, md, num_workers=n)  # одна модель, n одновременных расшифровок
            pool = cf.ThreadPoolExecutor(n, thread_name_prefix="whisper")
            _STATE["submit"] = lambda raw: pool.submit(worker.transcribe, raw, model)
        else:
            pool = cf.ProcessPoolExecutor(n, mp_context=multiprocessing.get_context("spawn"),
                                          initializer=worker.init_process, initargs=(MODEL, per, md))
            # дождаться загрузки модели в каждом процессе: инициализатор выполняется до первой задачи
            list(pool.map(worker.ping, range(n)))
            _STATE["submit"] = lambda raw: pool.submit(worker.transcribe, raw)
        _STATE.update(pool=pool, mode=mode, workers=n, python=parallel.describe(), vad=worker.vad_available(),
                      loaded_in=round(time.time() - t, 1))
        log.info("модель %s: %d исполнителей (%s, по %d потоков), Python %s, GIL %s, VAD %s, готово за %.1f с",
                 MODEL, n, mode, per, _STATE["python"]["python"], "вкл" if _STATE["python"]["gil_enabled"] else "выкл",
                 "да" if _STATE["vad"] else "нет", time.time() - t)
    except Exception as e:  # noqa: BLE001
        _STATE["error"] = "%s: %s" % (type(e).__name__, e)
        log.error("не удалось загрузить модель %s: %s", MODEL, _STATE["error"])
    finally:
        _STATE["loading"] = False


def r_transcribe(data, rid):
    if not isinstance(data, dict) or not data.get("audio_b64"):
        return 400, error_body("bad_request", "Нужен audio_b64", SVC)
    if _STATE["pool"] is None:
        return 503, error_body("not_ready", "Модель ещё не загружена", SVC)
    try:
        raw = base64.b64decode(data["audio_b64"], validate=True)
    except (binascii.Error, ValueError):
        return 400, error_body("bad_audio", "Аудио не в base64", SVC)
    if len(raw) > MAX_AUDIO:
        return 413, error_body("too_large", "Слишком большой кусок", SVC)
    t = time.time()
    try:  # не больше SPEECH_WORKERS кусков одновременно, остальные ждут свободного исполнителя
        segs, dur, who = _STATE["submit"](raw).result()
    except Exception as e:  # noqa: BLE001  (битый или пустой файл: PyAV не смог прочитать)
        log.warning("[%s] не удалось прочитать аудио: %s", rid, e)
        return 422, error_body("bad_audio", "Не удалось прочитать аудио", SVC)
    took = time.time() - t
    log.info("[%s] %.1f с аудио за %.1f с, сегментов %d, %s %s", rid, dur, took, len(segs), _STATE["mode"], who)
    return 200, {"segments": segs, "duration": round(dur, 2), "elapsed": round(took, 2), "model": MODEL, "language": LANG,
                 "parallel": _STATE["mode"], "worker": who}


def ready():
    if _STATE["pool"] is not None:
        return True, {"model": MODEL, "loaded_in": _STATE["loaded_in"], "parallel": _STATE["mode"], "workers": _STATE["workers"],
                      "python": _STATE["python"], "vad": _STATE["vad"]}
    return False, {"model": MODEL, "loading": _STATE["loading"], "error": _STATE["error"]}


if __name__ == "__main__":
    threading.Thread(target=load_model, daemon=True).start()
    Service(SVC, routes={("POST", "/v1/transcribe"): r_transcribe}, ready_fn=ready).run()
