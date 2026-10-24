# -*- coding: utf-8 -*-
"""Клиент вызова сервисов: подпись, таймаут, выключатель цепи (circuit breaker)."""
import json
import threading
import time
import urllib.error
import urllib.request
import uuid

from . import config, security
from .errors import ServiceUnavailable


class Breaker:
    """Закрыт: запросы идут. После `threshold` ошибок подряд открывается.
    В открытом состоянии запросы не шлются; раз в `probe_every` секунд пропускается один пробный."""

    def __init__(self, threshold=3, probe_every=10.0):
        self.threshold, self.probe_every = threshold, probe_every
        self.fails = 0
        self.opened_at = None
        self.last_probe = 0.0
        self.lock = threading.Lock()

    def allow(self):
        with self.lock:
            if self.opened_at is None:
                return True
            now = time.monotonic()
            if now - self.last_probe >= self.probe_every:
                self.last_probe = now
                return True
            return False

    def ok(self):
        with self.lock:
            self.fails = 0
            self.opened_at = None

    def fail(self):
        with self.lock:
            self.fails += 1
            if self.fails >= self.threshold and self.opened_at is None:
                self.opened_at = time.monotonic()
                self.last_probe = self.opened_at

    @property
    def is_open(self):
        return self.opened_at is not None

    def retry_after(self):
        with self.lock:
            if self.opened_at is None:
                return 0
            return max(1, int(self.probe_every - (time.monotonic() - self.last_probe)))


class ServiceClient:
    def __init__(self, name, base_url=None, secret=None, threshold=3, probe_every=10.0):
        self.name = name
        self.base = (base_url or config.service_url(name)).rstrip("/")
        self.secret = secret if secret is not None else config.service_secret()
        self.breaker = Breaker(threshold, probe_every)

    def call(self, method, path, payload=None, timeout=3.0, request_id=None):
        """Возвращает (status, data). Бросает ServiceUnavailable при сбое, таймауте и 5xx.
        Ответы 4xx возвращаются как есть: это ошибка запроса, а не сервиса."""
        if not self.breaker.allow():
            raise ServiceUnavailable(self.name, "выключатель цепи открыт", self.breaker.retry_after())
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        headers = {"Content-Type": "application/json", "X-Request-ID": request_id or uuid.uuid4().hex[:16]}
        headers.update(security.sign(self.secret, method, path, body))
        req = urllib.request.Request(self.base + path, data=body or None, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            self.breaker.fail()
            raise ServiceUnavailable(self.name, str(getattr(e, "reason", e)), self.breaker.retry_after() or 10)
        if status >= 500:
            self.breaker.fail()
            raise ServiceUnavailable(self.name, "HTTP %d" % status, self.breaker.retry_after() or 10)
        self.breaker.ok()
        try:
            data = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            data = None
        return status, data
