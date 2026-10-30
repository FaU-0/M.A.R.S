#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
M.A.R.S. - Mobility & Asymmetry Reporting Sessions: связь в локальной сети, оценка подвижности и асимметрии лица, отчёты по сеансу (один файл, Python 3.9+).
Запуск: python mars.py  (или вставить в ячейку Jupyter).
Сервер: только стандартная библиотека. Клиент: без CDN, MediaPipe отдаётся локально с /vendor.
"""
import base64
import hashlib
import heapq
import hmac
import http.cookies
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import smtplib
import socket
import socketserver  # noqa: F401  (ThreadingHTTPServer построен на нём)
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Общий код сервисов (папка shared рядом с gateway). Без него работает одиночный режим.
try:
    _root = Path(__file__).resolve().parent.parent
    if (_root / "shared").is_dir():
        sys.path.insert(0, str(_root))
    from shared import config as _svc_config
    from shared.client import ServiceClient as _ServiceClient
except Exception:  # noqa: BLE001
    _svc_config = None
    _ServiceClient = None

# Повторный запуск ячейки Jupyter: сначала останавливаем предыдущий экземпляр
_prev = globals().get("_MARS_RUNNING")
if _prev is not None:
    try:
        _prev.stop()
    except Exception:
        pass
_MARS_RUNNING = None


# ----------------------------------------------------------------------------
# Настройки (блок констант, переопределяются переменными окружения)
# ----------------------------------------------------------------------------
def _env_bool(name, default):
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "да")


PORT = int(os.environ.get("PORT", "8000"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "mars_data")).resolve()
AUTH_REQUIRED = _env_bool("AUTH_REQUIRED", True)
CERT_FILE = os.environ.get("CERT_FILE", "")
KEY_FILE = os.environ.get("KEY_FILE", "")
TURN_URL = os.environ.get("TURN_URL", "")
TURN_SECRET = os.environ.get("TURN_SECRET", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", os.environ.get("SMTP_PASS", ""))
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER
SMTP_SSL = _env_bool("SMTP_SSL", SMTP_PORT == 465)
CENTER_NAME = os.environ.get("CENTER_NAME", "Медицинский центр")
MEDIAPIPE_VERSION = os.environ.get("MEDIAPIPE_VERSION", "0.10.14")

SECURE = bool(CERT_FILE and KEY_FILE)
MAX_JSON = 25 * 1024 * 1024
MAX_FILE = 20 * 1024 * 1024
MAX_FILE_JSON = MAX_FILE * 4 // 3 + 65536  # Base64 файла 20 МБ чуть больше 25 МБ
MAX_FRAME = 5 * 1024 * 1024
MAX_ROOMS = 50
ROOM_TTL = 2 * 3600
MAX_EVENTS = 500
SESSION_TTL = 12 * 3600
EVENT_TYPES = {"offer", "answer", "ice", "chat", "file", "control", "kill", "ehr",
               "document", "presence", "face", "measure"}
SOFT_TYPES = {"presence", "face"}
PERSIST_TYPES = {"chat", "file", "document", "kill"}
POSES = {"frontal", "left45", "right45", "left90", "right90", "free"}
ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без 0, O, 1, I
ICD_RE = re.compile(r"^[A-Z]\d{2}(\.\d{1,2})?$")
FILE_TYPES = {
    ".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
}
VENDOR_FILES = [
    ("vision_bundle.mjs", "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@{v}/vision_bundle.mjs"),
    ("wasm/vision_wasm_internal.js", "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@{v}/wasm/vision_wasm_internal.js"),
    ("wasm/vision_wasm_internal.wasm", "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@{v}/wasm/vision_wasm_internal.wasm"),
    ("wasm/vision_wasm_nosimd_internal.js", "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@{v}/wasm/vision_wasm_nosimd_internal.js"),
    ("wasm/vision_wasm_nosimd_internal.wasm", "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@{v}/wasm/vision_wasm_nosimd_internal.wasm"),
    ("face_landmarker.task", "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"),
]

log = logging.getLogger("mars")
DB = None
COND = threading.Condition()
ROOMS = {}
IP_BUCKETS = {}
IP_LOCK = threading.Lock()


# ----------------------------------------------------------------------------
# Утилиты
# ----------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def now():
    return int(time.time())


def clean_text(v, n=4000):
    if v is None:
        return ""
    return str(v).replace("\x00", "").strip()[:n]


def clean_name(name):
    name = os.path.basename(str(name).replace("\\", "/"))
    name = re.sub(r"[^\w .\-()]", "_", name, flags=re.UNICODE)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:100] or "file"


def parse_when(v):
    if v is None or v == "":
        return now()
    if isinstance(v, (int, float)):
        v = float(v)
        return int(v / 1000) if v > 1e12 else int(v)
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return parse_when(float(s))
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        raise ApiError(400, "bad_time", "Некорректная дата и время")


def check_signature(ext, raw):
    if ext == ".pdf":
        return raw[:5] == b"%PDF-"
    if ext in (".jpg", ".jpeg"):
        return raw[:3] == b"\xff\xd8\xff"
    if ext == ".png":
        return raw[:8] == b"\x89PNG\r\n\x1a\n"
    if ext == ".xlsx":
        return raw[:4] == b"PK\x03\x04"
    if ext == ".xls":
        return raw[:8] == bytes.fromhex("D0CF11E0A1B11AE1")
    if ext == ".csv":
        head = raw[:4096]
        return not any(b < 9 or (13 < b < 32) for b in head)
    return False


class Bucket:
    def __init__(self, cap, rate):
        self.cap, self.rate, self.tokens, self.t = cap, rate, cap, time.monotonic()

    def take(self, n=1):
        t = time.monotonic()
        self.tokens = min(self.cap, self.tokens + (t - self.t) * self.rate)
        self.t = t
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


def ip_limit(ip, kind, cap, rate):
    with IP_LOCK:
        if len(IP_BUCKETS) > 5000:
            IP_BUCKETS.clear()
        b = IP_BUCKETS.setdefault((ip, kind), Bucket(cap, rate))
        if not b.take():
            raise ApiError(429, "rate_limited", "Слишком много запросов, попробуйте позже")


