# -*- coding: utf-8 -*-
"""Настройки сервисов: порты, адреса, общий секрет."""
import os
import secrets
from pathlib import Path

# Порты по умолчанию. gateway (8000) слушает снаружи, остальные только 127.0.0.1.
SERVICE_PORTS = {"vision": 8001, "reports": 8002, "speech": 8003, "assistant": 8004, "database": 8005}


def data_dir():
    return Path(os.environ.get("DATA_DIR", "mars_data")).resolve()


def secure_data_dir():
    """Данные пациентов доступны только владельцу: новые файлы 0600, папка данных 0700.
    Вызывается при запуске каждого процесса, который пишет в DATA_DIR."""
    os.umask(0o077)
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def service_url(name):
    """Адрес сервиса. Переопределяется MARS_<ИМЯ>_URL, например MARS_VISION_URL."""
    env = os.environ.get("MARS_%s_URL" % name.upper())
    if env:
        return env.rstrip("/")
    return "http://127.0.0.1:%d" % service_port(name)


def service_port(name):
    env = os.environ.get("MARS_%s_PORT" % name.upper())
    return int(env) if env else SERVICE_PORTS[name]


def monitored_services():
    """Какие сервисы gateway показывает в /status. Пусто: одиночный режим (python mars.py)."""
    raw = os.environ.get("MARS_SERVICES", "")
    return [s for s in (x.strip() for x in raw.split(",")) if s in SERVICE_PORTS]


def service_secret():
    """Общий секрет для подписи запросов между сервисами.

    Берётся из SERVICE_SECRET. Если не задан, читается или создаётся DATA_DIR/service_secret.txt.
    """
    env = os.environ.get("SERVICE_SECRET", "").strip()
    if env:
        return env
    path = data_dir() / "service_secret.txt"
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(32)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # сразу 0600, без окна между записью и chmod
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
        return value
    except OSError:
        return secrets.token_hex(32)
