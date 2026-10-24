# -*- coding: utf-8 -*-
"""Сервис assistant: обезличивание расшифровки через локальную нейросеть (Ollama). Состояния не хранит.

Настройки: OLLAMA_URL (по умолчанию http://127.0.0.1:11434), OLLAMA_MODEL (qwen2.5:3b).
Без Ollama сервис не готов (/ready 503) и /v1/deidentify отвечает 503: обезличивание без нейросети
пропускает имена, поэтому такой текст в отчёт не пускается, а задание ждёт в очереди gateway.
"""
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import deid  # noqa: E402
from shared.errors import error_body  # noqa: E402
from shared.service_base import Service  # noqa: E402

SVC = "assistant"
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
log = logging.getLogger("mars.assistant")
_RUN = threading.Lock()  # процессор один: окна текста идут по очереди

SYSTEM = ("Найди в тексте упоминания людей и мест. Выпиши каждое отдельно, коротко: только само имя или название, 1-4 слова, "
          "точно как в тексте.\nNAME - имя, фамилия или отчество человека. ORG - компания, работодатель, больница, поликлиника. "
          "CITY - город, село, страна, район. SCHOOL - школа, вуз, колледж, детский сад.\n"
          "Лекарства, болезни, симптомы и слова врач, доктор, пациент не выписывай.")
EXAMPLE_IN = "Пациент: Меня зовут Анна Козлова, я учитель в школе номер 12 в Твери. Муж Олег работает на заводе «Автоваз». Принимаю аспирин."
EXAMPLE_OUT = {"items": [{"text": "Анна Козлова", "type": "NAME"}, {"text": "школе номер 12", "type": "SCHOOL"},
                         {"text": "Твери", "type": "CITY"}, {"text": "Олег", "type": "NAME"}, {"text": "заводе «Автоваз»", "type": "ORG"}]}
SCHEMA = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "required": ["text", "type"],
    "properties": {"text": {"type": "string"}, "type": {"type": "string", "enum": ["NAME", "ORG", "CITY", "SCHOOL"]}}}}}}


class OllamaDown(Exception):
    pass


def ollama(path, body=None, timeout=300):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8") if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OllamaDown(str(getattr(e, "reason", e)))


def find_entities(text):
    """Шаг 3: модель возвращает фрагменты с типом. Текст она не переписывает."""
    body = {"model": MODEL, "stream": False, "format": SCHEMA, "keep_alive": "10m",
            "options": {"temperature": 0, "seed": 1, "num_ctx": 4096, "num_predict": 600},  # ответ короткий: зацикленная генерация обрывается
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": EXAMPLE_IN},
                         {"role": "assistant", "content": json.dumps(EXAMPLE_OUT, ensure_ascii=False)},
                         {"role": "user", "content": text}]}
    r = ollama("/api/chat", body)
    content = str((r.get("message") or {}).get("content") or "")
    try:
        return (json.loads(content) or {}).get("items") or []
    except (TypeError, ValueError, AttributeError):
        # ответ оборвался на ограничении длины: берём целые элементы до обрыва
        items = [{"text": json.loads('"%s"' % m.group(1)), "type": m.group(2)}
                 for m in re.finditer(r'\{\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"type"\s*:\s*"([A-Z]+)"\s*\}', content)]
        if not items:
            log.warning("модель вернула не JSON: %.200s", content)
        return items


def r_deidentify(data, rid):
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        return 400, error_body("bad_request", "Нужен список segments", SVC)
    segs = [s for s in data["segments"] if isinstance(s, dict)][:5000]
    t = time.time()
    try:
        with _RUN:
            out, repl, stats = deid.deidentify(segs, data.get("known") or {}, llm=find_entities)
    except OllamaDown as e:
        log.warning("[%s] Ollama недоступна: %s", rid, e)
        return 503, error_body("ollama_unavailable", "Локальная нейросеть недоступна", SVC, degraded=True)
    stats.update(model=MODEL, elapsed=round(time.time() - t, 1))
    log.info("[%s] сегментов %d, замен %d, окон %d, %.1f с", rid, len(segs), stats["replacements"], stats["llm_calls"], stats["elapsed"])
    return 200, {"segments": out, "replacements": repl, "stats": stats}


def ready():
    try:
        tags = ollama("/api/tags", timeout=2)
    except OllamaDown as e:
        return False, {"ollama": False, "error": str(e), "model": MODEL}
    names = {m.get("name") for m in tags.get("models") or []}
    if MODEL not in names and MODEL + ":latest" not in names:
        return False, {"ollama": True, "model": MODEL, "error": "модель не скачана: ollama pull " + MODEL}
    return True, {"ollama": True, "model": MODEL}


if __name__ == "__main__":
    Service(SVC, routes={("POST", "/v1/deidentify"): r_deidentify}, ready_fn=ready).run()
