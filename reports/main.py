# -*- coding: utf-8 -*-
"""Сервис reports: печатные формы (заключение). Состояния не хранит."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.service_base import Service  # noqa: E402
from reports.render import conclusion_html  # noqa: E402


def r_conclusion(data, rid):
    if not isinstance(data, dict):
        return 400, {"error": "bad_request", "message": "Ожидается объект", "service": "reports"}
    return 200, {"html": conclusion_html(data)}


def ready():
    return True, {}


if __name__ == "__main__":
    Service("reports", routes={("POST", "/v1/conclusion"): r_conclusion}, ready_fn=ready).run()
