# -*- coding: utf-8 -*-
"""Как распараллелить тяжёлую работу (расшифровку речи) на этом интерпретаторе.

Правило: без GIL (сборка Python 3.14t, free-threaded) — потоки, они идут на ядрах параллельно и делят
одну модель в памяти; с GIL (обычный Python, в том числе 3.14 без «t») — процессы, у каждого своя модель.

Решает не номер версии, а то, выключен ли GIL на самом деле: на 3.14t модуль расширения, не объявивший
работу без GIL (например, PyAV), при импорте включает GIL обратно. Поэтому режим нужно выбирать после
импорта тяжёлых библиотек.
"""
import os
import sys
import sysconfig

THREADS, PROCESSES = "threads", "processes"


def free_threaded_build():
    """Сборка без GIL (python3.14t): Py_GIL_DISABLED задан при компиляции."""
    return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def gil_enabled():
    """GIL включён сейчас. До Python 3.13 функции _is_gil_enabled нет, и GIL всегда включён."""
    fn = getattr(sys, "_is_gil_enabled", None)
    return True if fn is None else bool(fn())


def mode(override=None):
    """THREADS, если GIL выключен, иначе PROCESSES. override ('threads' или 'processes') — для проверки и замеров."""
    override = override or os.environ.get("MARS_PARALLEL", "")
    if override in (THREADS, PROCESSES):
        return override
    return THREADS if not gil_enabled() else PROCESSES


def describe():
    """Строка для журнала и ответа сервиса: версия, сборка, GIL и выбранный режим."""
    return {"python": "%d.%d.%d" % sys.version_info[:3], "free_threaded_build": free_threaded_build(),
            "gil_enabled": gil_enabled(), "mode": mode()}


def workers(default=2):
    """Сколько исполнителей держать: SPEECH_WORKERS, не меньше 1 и не больше числа логических ядер."""
    try:
        n = int(os.environ.get("SPEECH_WORKERS") or default)
    except ValueError:
        n = default
    return max(1, min(n, os.cpu_count() or 1))


def threads_per_worker(n_workers):
    """Потоки ctranslate2 на одного исполнителя: ядра делятся поровну, WHISPER_THREADS — общий лимит."""
    total = int(os.environ.get("WHISPER_THREADS") or os.cpu_count() or 4)
    return max(1, total // max(1, n_workers))
