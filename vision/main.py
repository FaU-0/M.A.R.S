# -*- coding: utf-8 -*-
"""Сервис vision. Этап 0: заглушка с /health и /ready, логика появится на своём этапе."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.service_base import Service  # noqa: E402


def ready():
    return True, {"stub": True}


if __name__ == "__main__":
    Service("vision", routes={}, ready_fn=ready).run()
