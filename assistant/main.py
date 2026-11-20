# -*- coding: utf-8 -*-
"""Сервис assistant: обезличивание расшифровки через нейросеть. Состояния не хранит.

Нейросеть выбирает LLM_BACKEND:
- ollama (по умолчанию) — локально: OLLAMA_URL (http://127.0.0.1:11434), OLLAMA_MODEL (qwen2.5:3b);
- gigachat — облако Сбера, только для экспериментов на синтетических данных: в облако уходит текст,
  где имена ещё не скрыты. GIGACHAT_AUTH_KEY (ключ авторизации), GIGACHAT_MODEL (GigaChat-2-Max),
  GIGACHAT_CA_FILE (сертификат НУЦ Минцифры), GIGACHAT_SCOPE, GIGACHAT_API_URL, GIGACHAT_OAUTH_URL.
Без нейросети сервис не готов (/ready 503) и /v1/deidentify отвечает 503: обезличивание без нейросети
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

from assistant import deid, gigachat  # noqa: E402
from shared.errors import error_body  # noqa: E402
from shared.service_base import Service  # noqa: E402

SVC = "assistant"
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
BACKEND = os.environ.get("LLM_BACKEND", "ollama").strip().lower()
if BACKEND not in ("ollama", "gigachat"):
    raise SystemExit("LLM_BACKEND: ollama или gigachat, а не %r" % BACKEND)
MODEL = os.environ.get("GIGACHAT_MODEL", "GigaChat-2-Max") if BACKEND == "gigachat" else os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
log = logging.getLogger("mars.assistant")
_RUN = threading.Lock()  # Ollama: процессор один; GigaChat: у физлица один поток. Окна идут по очереди

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


OllamaDown = gigachat.LlmDown  # прежнее имя: отказ любой нейросети


class _Cloud:
    """Клиент GigaChat создаётся при первом запросе; usage копит токены за время жизни сервиса."""

    def __init__(self):
        self.client = None
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def get(self):
        if self.client is None:
            self.client = gigachat.GigaChat(
                auth_key=os.environ.get("GIGACHAT_AUTH_KEY", "").strip(), model=MODEL,
                scope=os.environ.get("GIGACHAT_SCOPE", gigachat.SCOPE),
                oauth_url=os.environ.get("GIGACHAT_OAUTH_URL", gigachat.OAUTH_URL),
                api_url=os.environ.get("GIGACHAT_API_URL", gigachat.API_URL),
                ca_file=os.environ.get("GIGACHAT_CA_FILE") or None)
        return self.client

    def chat(self, messages):
        content, usage = self.get().chat(messages, SCHEMA, max_tokens=600)
        self.usage = dict(self.usage, calls=self.usage["calls"] + 1,
                          **{k: self.usage[k] + usage.get(k, 0) for k in ("prompt_tokens", "completion_tokens", "total_tokens")})
        return content


LLM = _Cloud()


def ollama(path, body=None, timeout=300):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8") if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OllamaDown(str(getattr(e, "reason", e)))


def messages_for(text):
    """Один и тот же запрос для обеих нейросетей: инструкция, пример, текст."""
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": EXAMPLE_IN},
            {"role": "assistant", "content": json.dumps(EXAMPLE_OUT, ensure_ascii=False)},
            {"role": "user", "content": text}]


def ask_ollama(text):
    body = {"model": MODEL, "stream": False, "format": SCHEMA, "keep_alive": "10m",
            "options": {"temperature": 0, "seed": 1, "num_ctx": 4096, "num_predict": 600},  # ответ короткий: зацикленная генерация обрывается
            "messages": messages_for(text)}
    r = ollama("/api/chat", body)
    return str((r.get("message") or {}).get("content") or "")


def find_entities(text):
    """Шаг 3: модель возвращает фрагменты с типом. Текст она не переписывает."""
    content = LLM.chat(messages_for(text)) if BACKEND == "gigachat" else ask_ollama(text)
    try:
        return (json.loads(content) or {}).get("items") or []
    except (TypeError, ValueError, AttributeError):
        # ответ оборвался на ограничении длины: берём целые элементы до обрыва
        items = [{"text": json.loads('"%s"' % m.group(1)), "type": m.group(2)}
                 for m in re.finditer(r'\{\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"type"\s*:\s*"([A-Z]+)"\s*\}', content)]
        if not items:
            # сам ответ не пишем: в нём имена и места из расшифровки
            log.warning("модель вернула не JSON (%d символов)", len(content))
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
        log.warning("[%s] нейросеть (%s) недоступна: %s", rid, BACKEND, e)
        return 503, error_body("ollama_unavailable", "Нейросеть недоступна", SVC, degraded=True)
    stats.update(model=MODEL, backend=BACKEND, elapsed=round(time.time() - t, 1))
    log.info("[%s] сегментов %d, замен %d, окон %d, %.1f с", rid, len(segs), stats["replacements"], stats["llm_calls"], stats["elapsed"])
    return 200, {"segments": out, "replacements": repl, "stats": stats}


def ready():
    if BACKEND == "gigachat":
        try:
            LLM.get().token()
        except gigachat.LlmDown as e:
            return False, {"backend": BACKEND, "model": MODEL, "error": str(e)}
        return True, {"backend": BACKEND, "model": MODEL, "usage": LLM.usage}
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
