# -*- coding: utf-8 -*-
"""Клиент GigaChat (assistant/gigachat.py) без сети: сервер подменён, ключ ненастоящий.

    python tests/test_gigachat.py
Проверяет: получение и кэш токена, повторный вход при 401, повтор при 429, отказ облака -> LlmDown,
формат запроса (response_format json_schema, температура > 0), учёт токенов, выбор варианта в assistant/main.py.
"""
import io
import json
import os
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from assistant import gigachat  # noqa: E402

FAILED = []
FAKE_KEY = "ZmFrZS1jbGllbnQ6ZmFrZS1zZWNyZXQ="  # base64("fake-client:fake-secret"), не настоящий ключ


def check(ok, msg):
    print(("OK    " if ok else "FAIL  ") + msg)
    if not ok:
        FAILED.append(msg)


class Resp(io.BytesIO):
    def __init__(self, obj, status=200):
        super().__init__(json.dumps(obj).encode("utf-8"))
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(url, code):
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(json.dumps({"status": code}).encode()))


class FakeServer:
    """Отвечает на /oauth и /chat/completions по сценарию; запоминает запросы."""

    def __init__(self, chat_script=None, now=1790000000.0):
        self.requests = []
        self.chat_script = list(chat_script or [])
        self.tokens_issued = 0
        self.now = now

    def urlopen(self, req, timeout=None, context=None):
        url = req.full_url
        body = req.data.decode("utf-8") if req.data else ""
        self.requests.append({"url": url, "headers": dict(req.header_items()), "body": body, "timeout": timeout})
        if url.endswith("/oauth"):
            self.tokens_issued += 1
            return Resp({"access_token": "tok%d" % self.tokens_issued, "expires_at": int((self.now + 1800) * 1000)})
        step = self.chat_script.pop(0) if self.chat_script else "ok"
        if isinstance(step, int):
            raise http_error(url, step)
        if step == "net":
            raise urllib.error.URLError("connection refused")
        content = step if isinstance(step, str) and step != "ok" else json.dumps({"items": [{"text": "Анна", "type": "NAME"}]}, ensure_ascii=False)
        return Resp({"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})


def client(server, **kw):
    params = dict(auth_key=FAKE_KEY, model="GigaChat-2-Max", urlopen=server.urlopen, clock=lambda: server.now,
                  sleep=lambda s: None, ca_file=None)
    params.update(kw)
    return gigachat.GigaChat(**params)


MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "Меня зовут Анна"}]
SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}}


def test_token_and_request_shape():
    s = FakeServer()
    c = client(s)
    content, usage = c.chat(MSGS, SCHEMA, max_tokens=600)
    oauth, chat = s.requests[0], s.requests[1]
    check(oauth["url"].endswith("/api/v2/oauth"), "токен берётся на /api/v2/oauth")
    check(oauth["headers"].get("Authorization") == "Basic " + FAKE_KEY, "ключ уходит в заголовке Basic")
    rquid = oauth["headers"].get("Rquid") or oauth["headers"].get("RqUID") or ""
    check(len(rquid) == 36 and rquid.count("-") == 4, "заголовок RqUID в формате uuid4")
    check(oauth["body"] == "scope=GIGACHAT_API_PERS", "scope физлица по умолчанию")
    check(chat["url"].endswith("/chat/completions"), "запрос на /chat/completions")
    check(chat["headers"].get("Authorization") == "Bearer tok1", "запрос с токеном Bearer")
    body = json.loads(chat["body"])
    check(body["model"] == "GigaChat-2-Max" and body["messages"] == MSGS, "модель и сообщения как переданы")
    rf = body.get("response_format") or {}
    check(rf.get("type") == "json_schema" and rf.get("schema") == SCHEMA and rf.get("strict") is True,
          "ответ по JSON-схеме (response_format json_schema, strict)")
    check(0 < body.get("temperature", 0) < 0.01, "температура малая, но больше 0 (0 API не принимает)")
    check(body.get("max_tokens") == 600 and body.get("stream") is False, "ограничение длины ответа, без потока")
    check(json.loads(content)["items"][0]["text"] == "Анна", "содержимое ответа возвращается строкой")
    check(usage == {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}, "учёт токенов из usage")
    check(all("ZmFrZS" not in r["body"] for r in s.requests[1:]), "ключ не попадает в тело запросов к модели")


def test_token_cache_and_refresh():
    s = FakeServer()
    c = client(s)
    c.chat(MSGS, SCHEMA)
    c.chat(MSGS, SCHEMA)
    check(s.tokens_issued == 1, "токен переиспользуется, пока не истёк")
    s.now += 1800 - 30  # меньше минуты до истечения: обновляем заранее
    c.chat(MSGS, SCHEMA)
    check(s.tokens_issued == 2, "токен обновляется заранее, до истечения 30 минут")


def test_expires_at_seconds_or_ms():
    s = FakeServer()
    c = client(s)
    check(abs(c._expiry(int((s.now + 1800) * 1000)) - (s.now + 1800)) < 1, "expires_at в миллисекундах понимается")
    check(abs(c._expiry(int(s.now + 1800)) - (s.now + 1800)) < 1, "expires_at в секундах понимается")


def test_401_relogin():
    s = FakeServer(chat_script=[401, "ok"])
    c = client(s)
    c.chat(MSGS, SCHEMA)
    check(s.tokens_issued == 2, "при 401 токен берётся заново и запрос повторяется")


def test_429_retry_then_fail():
    slept = []
    s = FakeServer(chat_script=[429, 429, "ok"])
    c = client(s, sleep=slept.append)
    c.chat(MSGS, SCHEMA)
    check(len(slept) == 2 and slept[1] > slept[0], "при 429 повтор с растущей паузой")
    s2 = FakeServer(chat_script=[429] * 10)
    c2 = client(s2, sleep=lambda x: None)
    try:
        c2.chat(MSGS, SCHEMA)
        check(False, "после исчерпания повторов 429 -> LlmDown")
    except gigachat.LlmDown:
        check(True, "после исчерпания повторов 429 -> LlmDown")


def test_failures_are_llmdown():
    for step, name in ((500, "ошибка сервера 500"), (402, "кончились токены 402"), ("net", "нет сети")):
        s = FakeServer(chat_script=[step] * 5)
        c = client(s, sleep=lambda x: None)
        try:
            c.chat(MSGS, SCHEMA)
            check(False, "%s -> LlmDown" % name)
        except gigachat.LlmDown as e:
            check("ZmFrZS" not in str(e), "%s -> LlmDown, ключ не в тексте ошибки" % name)


def test_no_key():
    try:
        gigachat.GigaChat(auth_key="", model="m")
        check(False, "без ключа клиент не создаётся")
    except gigachat.LlmDown:
        check(True, "без ключа клиент не создаётся")


def test_clean_llm_inflected_forms():
    from assistant import deid
    text = "Пациент: Я из Кинешмы, подруге Гульнаре звонила. Принимаю аспирин."
    items = [{"text": "Кинешма", "type": "CITY"}, {"text": "Гульнара", "type": "NAME"},
             {"text": "Москва", "type": "CITY"}, {"text": "монтажник", "type": "JOB"}]
    kept = {(f.kind, f.fragment) for f in deid.clean_llm(items, text)}
    check(("CITY", "Кинешмы") in kept, "модель вернула «Кинешма», в тексте «Кинешмы»: берётся форма из текста")
    check(("NAME", "Гульнаре") in kept, "модель вернула «Гульнара», в тексте «Гульнаре»: берётся форма из текста")
    check(not any(f == "Москва" or f.startswith("Моск") for _, f in kept), "фрагмента нет в тексте ни в какой форме: отброшен")
    check(not any(k == "JOB" for k, _ in kept), "тип вне схемы (JOB) отброшен")


def test_backend_switch():
    os.environ["LLM_BACKEND"] = "gigachat"
    os.environ["GIGACHAT_AUTH_KEY"] = FAKE_KEY
    import importlib
    from assistant import main
    importlib.reload(main)
    s = FakeServer()
    main.LLM.client = client(s)
    items = main.find_entities("Меня зовут Анна")
    check(main.BACKEND == "gigachat", "LLM_BACKEND=gigachat выбирает облако")
    check(items == [{"text": "Анна", "type": "NAME"}], "find_entities разбирает ответ GigaChat")
    check(main.LLM.usage["total_tokens"] == 120 and main.LLM.usage["calls"] == 1, "assistant считает токены облака")
    body = json.loads(s.requests[-1]["body"])
    check(body["messages"][0]["role"] == "system" and body["messages"][-1]["content"] == "Меня зовут Анна",
          "тот же запрос, что для Ollama: системная инструкция, пример, текст")
    os.environ["LLM_BACKEND"] = "ollama"
    importlib.reload(main)
    check(main.BACKEND == "ollama", "по умолчанию и при LLM_BACKEND=ollama остаётся Ollama")
    os.environ.pop("LLM_BACKEND", None)
    os.environ.pop("GIGACHAT_AUTH_KEY", None)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print("--- %s ---" % name)
            fn()
    print("\n%s: %d ошибок" % ("ПРОВАЛ" if FAILED else "ВСЁ В ПОРЯДКЕ", len(FAILED)))
    sys.exit(1 if FAILED else 0)
