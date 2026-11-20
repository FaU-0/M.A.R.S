# -*- coding: utf-8 -*-
"""Клиент облачного GigaChat API (Сбер) на стандартной библиотеке.

Ключ авторизации берётся из окружения (GIGACHAT_AUTH_KEY), в код и журналы не попадает.
Токен доступа живёт 30 минут: храним его и обновляем за минуту до истечения.
Физлицам доступен один поток: при 429 повторяем с растущей паузой.
Сервер GigaChat подписан сертификатом НУЦ Минцифры: путь к нему в GIGACHAT_CA_FILE.
"""
from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Callable

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://gigachat.devices.sberbank.ru/api/v1"
SCOPE = "GIGACHAT_API_PERS"  # физлица
TEMPERATURE = 0.001  # API не принимает 0; меньше 0,001 — режим строгого контроля
REFRESH_BEFORE_S = 60
RETRIES_429 = 4
BACKOFF_S = 1.0
TIMEOUT_S = 120


class LlmDown(Exception):
    """Нейросеть недоступна: задание ждёт в очереди gateway, текст в отчёт не идёт."""


class GigaChat:
    def __init__(self, auth_key: str, model: str, scope: str = SCOPE, oauth_url: str = OAUTH_URL,
                 api_url: str = API_URL, ca_file: str | None = None, timeout: float = TIMEOUT_S,
                 urlopen: Callable | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        if not auth_key:
            raise LlmDown("не задан GIGACHAT_AUTH_KEY")
        self._key = auth_key
        self.model = model
        self.scope = scope
        self.oauth_url = oauth_url
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._ctx = ssl.create_default_context(cafile=ca_file) if ca_file else None
        self._urlopen = urlopen or urllib.request.urlopen
        self._clock = clock
        self._sleep = sleep
        self._token = None
        self._token_until = 0.0
        self._lock = threading.Lock()

    def _open(self, req: urllib.request.Request, timeout: float) -> dict:
        kw = {"timeout": timeout}
        if self._ctx is not None:
            kw["context"] = self._ctx
        with self._urlopen(req, **kw) as r:
            return json.loads(r.read().decode("utf-8"))

    def _expiry(self, expires_at) -> float:
        """expires_at в документации без единиц: значения больше 10^12 — миллисекунды."""
        value = float(expires_at)
        return value / 1000.0 if value > 1e12 else value

    def _login(self) -> str:
        req = urllib.request.Request(self.oauth_url, method="POST",
                                     data=urllib.parse.urlencode({"scope": self.scope}).encode("ascii"),
                                     headers={"Authorization": "Basic " + self._key, "RqUID": str(uuid.uuid4()),
                                              "Content-Type": "application/x-www-form-urlencoded",
                                              "Accept": "application/json"})
        try:
            r = self._open(req, timeout=15)
        except urllib.error.HTTPError as e:
            raise LlmDown("GigaChat: вход не удался, HTTP %d" % e.code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LlmDown("GigaChat: вход не удался: %s" % type(e).__name__)
        token = r.get("access_token")
        if not token:
            raise LlmDown("GigaChat: в ответе нет access_token")
        self._token = token
        self._token_until = self._expiry(r.get("expires_at") or self._clock() + 1800)
        return token

    def token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or self._clock() > self._token_until - REFRESH_BEFORE_S:
                return self._login()
            return self._token

    def _request(self, body: dict, token: str) -> dict:
        req = urllib.request.Request(self.api_url + "/chat/completions", method="POST",
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                              "Accept": "application/json"})
        return self._open(req, timeout=self.timeout)

    def chat(self, messages: list, schema: dict | None = None, max_tokens: int = 600) -> tuple[str, dict]:
        """Возвращает (текст ответа, usage). Любой отказ облака — LlmDown."""
        body = {"model": self.model, "messages": messages, "temperature": TEMPERATURE,
                "max_tokens": max_tokens, "stream": False}
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "schema": schema, "strict": True}
        relogged = False
        attempt_429 = 0
        while True:
            try:
                r = self._request(body, self.token(force=False))
                break
            except urllib.error.HTTPError as e:
                if e.code == 401 and not relogged:
                    relogged = True
                    self.token(force=True)
                    continue
                if e.code == 429 and attempt_429 < RETRIES_429:
                    self._sleep(BACKOFF_S * (2 ** attempt_429))
                    attempt_429 += 1
                    continue
                raise LlmDown("GigaChat: HTTP %d" % e.code)
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise LlmDown("GigaChat: %s" % type(e).__name__)
        try:
            content = str(r["choices"][0]["message"].get("content") or "")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LlmDown("GigaChat: ответ без choices")
        usage = r.get("usage") or {}
        return content, {k: int(usage.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
