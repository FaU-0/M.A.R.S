# -*- coding: utf-8 -*-
"""Единый формат ошибок всех сервисов."""


def error_body(code, message, service, **extra):
    body = {"error": code, "message": message, "service": service}
    body.update(extra)
    return body


def unavailable_body(service, message=None):
    return error_body("service_unavailable", message or "Сервис временно недоступен", service, degraded=True)


class ServiceUnavailable(Exception):
    """Сервис не ответил вовремя, отказал или отключён выключателем цепи."""

    def __init__(self, service, reason="", retry_after=10):
        super().__init__("%s: %s" % (service, reason or "недоступен"))
        self.service = service
        self.reason = reason
        self.retry_after = retry_after
