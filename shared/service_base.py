# -*- coding: utf-8 -*-
"""Каркас вторичного сервиса: /health, /ready, проверка подписи, единый формат ошибок.

Сервис слушает только 127.0.0.1. /health и /ready открыты (для проверки живости),
остальные маршруты требуют подпись общего секрета.
"""
import json
import logging
import signal
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, security
from .errors import error_body

MAX_BODY = 25 * 1024 * 1024


class Service:
    def __init__(self, name, routes=None, ready_fn=None, host="127.0.0.1", port=None):
        """routes: {("POST", "/v1/analyze"): fn(data, request_id) -> (status, obj)}
        ready_fn: () -> (bool, dict) готовы ли модели и зависимости."""
        self.name = name
        self.routes = routes or {}
        self.ready_fn = ready_fn or (lambda: (True, {}))
        self.host = host
        self.port = port if port is not None else config.service_port(name)
        self.secret = config.service_secret()
        self.log = logging.getLogger("mars." + name)
        self.httpd = None

    def _handler(self):
        svc = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                svc.log.debug("%s " + fmt, self.client_address[0], *args)

            def _send(self, status, obj, rid, extra=None):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Request-ID", rid)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _handle(self, method):
                rid = self.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
                path = self.path.split("?", 1)[0]
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n > MAX_BODY:
                    self.close_connection = True
                    return self._send(413, error_body("too_large", "Слишком большой запрос", svc.name), rid)
                raw = self.rfile.read(n) if n else b""
                if method == "GET" and path == "/health":
                    return self._send(200, {"status": "ok", "service": svc.name}, rid)
                if method == "GET" and path == "/ready":
                    try:
                        ok, info = svc.ready_fn()
                    except Exception as e:  # noqa: BLE001
                        ok, info = False, {"error": str(e)}
                    body = {"ready": bool(ok), "service": svc.name}
                    body.update(info or {})
                    return self._send(200 if ok else 503, body, rid)
                if not security.verify(svc.secret, method, path, raw, self.headers):
                    return self._send(401, error_body("bad_signature", "Неверная подпись запроса", svc.name), rid)
                fn = svc.routes.get((method, path))
                if fn is None:
                    return self._send(404, error_body("not_found", "Маршрут не найден", svc.name), rid)
                try:
                    data = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    return self._send(400, error_body("bad_json", "Тело запроса не JSON", svc.name), rid)
                try:
                    status, obj = fn(data, rid)
                except Exception:  # noqa: BLE001
                    svc.log.exception("[%s] ошибка обработчика %s", rid, path)
                    return self._send(500, error_body("internal", "Внутренняя ошибка сервиса", svc.name), rid)
                return self._send(status, obj, rid)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        return H

    def run(self):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

        class Srv(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        try:
            self.httpd = Srv((self.host, self.port), self._handler())
        except OSError as e:
            print("[%s] не удалось занять порт %d: %s" % (self.name, self.port, e))
            sys.exit(1)

        def stop(*_):
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()

        try:
            signal.signal(signal.SIGTERM, stop)
        except (ValueError, OSError):
            pass
        print("[%s] слушает http://%s:%d" % (self.name, self.host, self.port))
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.httpd.server_close()
