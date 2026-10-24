#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
M.A.R.S. - Mobility & Asymmetry Reporting Sessions: связь в локальной сети, оценка подвижности и асимметрии лица, отчёты по сеансу (один файл, Python 3.9+).
Запуск: python mars.py  (или вставить в ячейку Jupyter).
Сервер: только стандартная библиотека. Клиент: без CDN, MediaPipe отдаётся локально с /vendor.
"""
import base64
import hashlib
import hmac
import http.cookies
import json
import logging
import mimetypes
import os
import re
import secrets
import smtplib
import socket
import socketserver  # noqa: F401  (ThreadingHTTPServer построен на нём)
import sqlite3
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


# ----------------------------------------------------------------------------
# База данных
# ----------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS doctors(id INTEGER PRIMARY KEY, login TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  salt BLOB NOT NULL, pw BLOB NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(sid TEXT PRIMARY KEY, doctor_id INTEGER NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS patients(id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, birth TEXT DEFAULT '',
  sex TEXT DEFAULT '', blood TEXT DEFAULT '', allergies TEXT DEFAULT '', chronic TEXT DEFAULT '',
  meds TEXT DEFAULT '', complaints TEXT DEFAULT '', email TEXT DEFAULT '', created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, patient_id INTEGER,
  doctor_id INTEGER, scheduled INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'planned',
  protocol TEXT NOT NULL DEFAULT '{}', started INTEGER, ended INTEGER, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, appt_id INTEGER NOT NULL, ts INTEGER NOT NULL,
  sender TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notes(appt_id INTEGER PRIMARY KEY, body TEXT NOT NULL DEFAULT '', updated INTEGER);
CREATE TABLE IF NOT EXISTS conclusions(appt_id INTEGER PRIMARY KEY, body TEXT NOT NULL, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, appt_id INTEGER NOT NULL, ts INTEGER NOT NULL,
  sender TEXT NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, path TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS photos(id INTEGER PRIMARY KEY, appt_id INTEGER NOT NULL, patient_id INTEGER,
  pose TEXT NOT NULL, ts INTEGER NOT NULL, size INTEGER NOT NULL, path TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS measurements(id INTEGER PRIMARY KEY, appt_id INTEGER NOT NULL, patient_id INTEGER,
  kind TEXT NOT NULL, ts INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, room TEXT DEFAULT '', detail TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_photos_patient ON photos(patient_id, pose, ts);
CREATE INDEX IF NOT EXISTS ix_meas_patient ON measurements(patient_id, ts);
"""


class Database:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self.conn.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def run(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except Exception:
                pass


def audit(actor, action, room="", detail=""):
    try:
        DB.run("INSERT INTO audit(ts,actor,action,room,detail) VALUES(?,?,?,?,?)",
               (now(), actor, action, room, clean_text(detail, 200)))
    except Exception:
        log.exception("audit")


def hash_pw(pw, salt):
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def create_doctor(login, name, password, is_admin=0):
    salt = secrets.token_bytes(16)
    return DB.run("INSERT INTO doctors(login,name,salt,pw,is_admin,created) VALUES(?,?,?,?,?,?)",
                  (login, name, salt, hash_pw(password, salt), is_admin, now()))


def bootstrap_admin():
    """Администратор admin. Пароль: ADMIN_PASSWORD, иначе файл DATA_DIR/admin_password.txt,
    иначе случайный (сохраняется в этот файл). Логин и пароль печатаются при каждом запуске."""
    pfile = DATA_DIR / "admin_password.txt"
    fixed = os.environ.get("ADMIN_PASSWORD", "").strip()
    reset = _env_bool("RESET_ADMIN", False)
    pw = fixed
    if not pw and not reset and pfile.is_file():
        pw = pfile.read_text(encoding="utf-8").strip()
    if not pw:
        pw = secrets.token_urlsafe(9)
    try:
        pfile.write_text(pw + "\n", encoding="utf-8")
        os.chmod(str(pfile), 0o600)
    except OSError:
        pass
    row = DB.one("SELECT id, salt, pw FROM doctors WHERE login='admin'")
    if not row:
        create_doctor("admin", "Администратор", pw, 1)
    elif not hmac.compare_digest(bytes(row["pw"]), hash_pw(pw, bytes(row["salt"]))):
        salt = secrets.token_bytes(16)
        DB.run("UPDATE doctors SET salt=?, pw=?, is_admin=1 WHERE id=?", (salt, hash_pw(pw, salt), row["id"]))
        DB.run("DELETE FROM sessions WHERE doctor_id=?", (row["id"],))
    print("=" * 64)
    print("Вход администратора.  Логин: admin   Пароль: %s" % pw)
    print("Пароль хранится в файле %s" % pfile)
    print("=" * 64)


def sid_hash(sid):
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# Комнаты и сигналинг (в памяти)
# ----------------------------------------------------------------------------
class Room:
    def __init__(self, code, appt_id):
        self.code = code
        self.appt_id = appt_id
        self.doc_token = None
        self.pat_token = None
        self.events = []
        self.next_id = 1
        self.closed = False
        self.ended = False
        self.last = time.time()
        self.delivered_kill = set()
        self.buckets = {}

    def bucket(self, role, kind):
        key = (role, kind)
        if key not in self.buckets:
            self.buckets[key] = Bucket(40, 15) if kind == "main" else Bucket(10, 4)
        return self.buckets[key]


def add_event(room, typ, sender, to, data):
    """Вызывать под COND."""
    ev = {"id": room.next_id, "type": typ, "from": sender, "to": to, "ts": now(), "data": data}
    room.next_id += 1
    room.events.append(ev)
    over = len(room.events) - MAX_EVENTS
    if over > 0:
        keep = []
        for e in room.events:
            if over > 0 and e["type"] not in PERSIST_TYPES:
                over -= 1
                continue
            keep.append(e)
        if over > 0:
            keep = keep[over:]
        room.events = keep
    room.last = time.time()
    COND.notify_all()
    return ev


def new_code():
    for _ in range(50):
        code = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(6))
        if not DB.one("SELECT id FROM appointments WHERE code=?", (code,)) and code not in ROOMS:
            return code
    raise ApiError(500, "internal", "Не удалось создать код")


def get_room(code, create=False):
    code = code.upper()
    with COND:
        r = ROOMS.get(code)
        if r:
            return r
    appt = DB.one("SELECT id,status FROM appointments WHERE code=?", (code,))
    if not appt:
        raise ApiError(404, "room_not_found", "Комната не найдена")
    if appt["status"] == "completed":
        raise ApiError(410, "room_closed", "Консультация завершена")
    if not create:
        raise ApiError(404, "room_not_found", "Комната не найдена")
    with COND:
        r = ROOMS.get(code)
        if r:
            return r
        if len(ROOMS) >= MAX_ROOMS:
            raise ApiError(429, "rooms_limit", "Достигнут лимит активных комнат")
        r = Room(code, appt["id"])
        ROOMS[code] = r
        return r


def room_role(room, headers):
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    tok = auth[7:].strip().encode("utf-8", "ignore")
    with COND:
        if room.doc_token and hmac.compare_digest(tok, room.doc_token.encode()):
            return "doctor"
        if room.pat_token and hmac.compare_digest(tok, room.pat_token.encode()):
            return "patient"
    return None


def cleanup_loop(stop_evt):
    while not stop_evt.wait(60):
        try:
            t = time.time()
            with COND:
                for code in [c for c, r in ROOMS.items() if t - r.last > ROOM_TTL]:
                    del ROOMS[code]
            DB.run("DELETE FROM sessions WHERE expires<?", (now(),))
        except Exception:
            log.exception("cleanup")


# ----------------------------------------------------------------------------
# Проверки данных
# ----------------------------------------------------------------------------
CARD_FIELDS = ("full_name", "birth", "sex", "blood", "allergies", "chronic", "meds", "complaints", "email")
CONCL_FIELDS = ("complaints", "anamnesis", "diagnosis", "icd", "recommendations", "followup")


def clean_conclusion(d):
    if not isinstance(d, dict):
        raise ApiError(400, "bad_request", "Некорректное заключение")
    out = {k: clean_text(d.get(k), 8000) for k in CONCL_FIELDS}
    out["icd"] = out["icd"].upper().replace(",", ".")
    if out["icd"] and not ICD_RE.match(out["icd"]):
        raise ApiError(400, "bad_icd", "Код МКБ-10 должен иметь вид A00 или A00.0")
    if out["followup"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", out["followup"]):
        raise ApiError(400, "bad_date", "Дата повторного контакта: ГГГГ-ММ-ДД")
    return out


def has_mm_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).endswith("_mm") and v is not None:
                return True
            if has_mm_keys(v):
                return True
    elif isinstance(obj, list):
        return any(has_mm_keys(x) for x in obj)
    return False


def appt_by_room(room):
    return DB.one("SELECT * FROM appointments WHERE id=?", (room.appt_id,))


def load_protocol_json(appt):
    try:
        return json.loads(appt.get("protocol") or "{}")
    except ValueError:
        return {}


def save_card(appt, card):
    vals = {k: clean_text(card.get(k), 2000) for k in CARD_FIELDS if k in card}
    pid = appt["patient_id"]
    if pid is None:
        name = vals.get("full_name") or "Без имени"
        pid = DB.run("INSERT INTO patients(full_name,created) VALUES(?,?)", (name, now()))
        DB.run("UPDATE appointments SET patient_id=? WHERE id=?", (pid, appt["id"]))
    if vals:
        if "full_name" in vals and not vals["full_name"]:
            vals.pop("full_name")
        sets = ",".join("%s=?" % k for k in vals)
        if sets:
            DB.run("UPDATE patients SET %s WHERE id=?" % sets, tuple(vals.values()) + (pid,))
    return pid


def patient_card(pid):
    if pid is None:
        return {k: "" for k in CARD_FIELDS}
    p = DB.one("SELECT * FROM patients WHERE id=?", (pid,)) or {}
    return {k: p.get(k, "") or "" for k in CARD_FIELDS}


# ----------------------------------------------------------------------------
# Файлы MediaPipe (vendor)
# ----------------------------------------------------------------------------
def vendor_missing():
    vd = DATA_DIR / "vendor"
    return [(rel, url.format(v=MEDIAPIPE_VERSION)) for rel, url in VENDOR_FILES
            if not (vd / rel).is_file() or (vd / rel).stat().st_size == 0]


def ensure_vendor():
    vd = DATA_DIR / "vendor"
    for rel, url in vendor_missing():
        target = vd / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            print("Скачиваю %s ..." % rel)
            req = urllib.request.Request(url, headers={"User-Agent": "MARS/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            if not data:
                raise ValueError("пустой ответ")
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(target)
            print("  готово: %s, %.1f КБ" % (rel, len(data) / 1024))
        except Exception as e:  # сеть недоступна и т.п.
            print("  не удалось: %s (%s)" % (rel, e))
    miss = vendor_missing()
    if miss:
        print("-" * 64)
        print("Модуль Face Mesh отключён: не хватает файлов MediaPipe. Видеозвонок работает.")
        print("Положите файлы вручную в каталог %s :" % vd)
        for rel, url in miss:
            print("  %s   <-  %s" % (rel, url))
        print("-" * 64)
    return not miss


# ----------------------------------------------------------------------------
# Почта
# ----------------------------------------------------------------------------
def send_mail_async(to, subject, text):
    if not (SMTP_HOST and to and SMTP_FROM):
        return False

    def work():
        try:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = SMTP_FROM, to, subject
            msg.set_content(text)
            if SMTP_SSL:
                s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
            else:
                s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
                s.starttls()
            with s:
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        except Exception as e:
            log.warning("SMTP: письмо не отправлено (%s)", e.__class__.__name__)

    threading.Thread(target=work, daemon=True).start()
    return True


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
SPA_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(self), microphone=(self), display-capture=(self)",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
                                "style-src 'self' 'unsafe-inline'; worker-src 'self' blob:; connect-src 'self'; "
                                "img-src 'self' data: blob:; media-src 'self' blob:"),
    "Cache-Control": "no-store",
}
VENDOR_MIME = {".mjs": "text/javascript", ".js": "text/javascript", ".wasm": "application/wasm",
               ".task": "application/octet-stream"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MARS"
    sys_version = ""
    timeout = 90

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    # --- вывод ---
    def _headers_common(self, json_mode=True):
        if json_mode:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Pose")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")

    def reply(self, status, body, ctype, extra=None, json_mode=True):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._headers_common(json_mode)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, obj, extra=None):
        self.reply(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def send_err(self, status, code, message):
        try:
            if not self._body_read and int(self.headers.get("Content-Length") or 0) > 0:
                self.close_connection = True
        except ValueError:
            self.close_connection = True
        self.send_json(status, {"error": code, "message": message})

    # --- ввод ---
    def read_raw(self, limit):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad_length", "Некорректная длина запроса")
        if n < 0:
            raise ApiError(400, "bad_length", "Некорректная длина запроса")
        if n > limit:
            raise ApiError(413, "too_large", "Запрос слишком большой")
        data = self.rfile.read(n) if n else b""
        self._body_read = True
        if len(data) != n:
            raise ApiError(400, "truncated", "Тело запроса получено не полностью")
        return data

    def read_json(self, limit=MAX_JSON, allow_empty=False):
        raw = self.read_raw(limit)
        if not raw.strip():
            if allow_empty:
                return {}
            raise ApiError(400, "bad_json", "Пустое тело запроса")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_json", "Некорректный JSON")
        if not isinstance(obj, dict):
            raise ApiError(400, "bad_json", "Ожидался объект JSON")
        return obj

    # --- авторизация врача ---
    def cookie(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = http.cookies.SimpleCookie()
            c.load(raw)
            return c[name].value if name in c else None
        except http.cookies.CookieError:
            return None

    def doctor(self, required=True):
        sid = self.cookie("mars_sid")
        if sid:
            row = DB.one("SELECT d.* FROM sessions s JOIN doctors d ON d.id=s.doctor_id "
                         "WHERE s.sid=? AND s.expires>?", (sid_hash(sid), now()))
            if row:
                return row
        if not AUTH_REQUIRED and not (self.headers.get("Authorization") or "").startswith("Bearer pat_"):
            return {"id": 0, "login": "local", "name": "Врач", "is_admin": 1}
        if required:
            raise ApiError(401, "unauthorized", "Требуется вход врача")
        return None

    def actor(self, doc):
        return "doctor:%s" % doc["login"]

    def base_url(self):
        host = self.headers.get("Host") or "localhost:%d" % PORT
        if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host):
            host = "localhost:%d" % PORT
        return "%s://%s" % ("https" if SECURE else "http", host)

    # --- маршрутизация ---
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._headers_common(True)
        self.end_headers()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_HEAD(self):
        self._route("GET")

    do_PUT = do_DELETE = do_PATCH = lambda self: self._route("X")

    def _route(self, method):
        self._body_read = False
        if getattr(self.server, "stop_evt", None) is not None and self.server.stop_evt.is_set():
            self.close_connection = True
            return
        try:
            sp = urllib.parse.urlsplit(self.path)
            path = sp.path
            qs = urllib.parse.parse_qs(sp.query)
            self._dispatch(method, path, qs)
        except ApiError as e:
            self.send_err(e.status, e.code, e.message)
        except (BrokenPipeError, ConnectionError, socket.timeout, ssl.SSLError):
            self.close_connection = True
        except Exception:
            log.exception("Ошибка обработки запроса")
            try:
                self.send_err(500, "internal", "Внутренняя ошибка сервера")
            except Exception:
                self.close_connection = True

    def _dispatch(self, method, path, qs):
        if method == "GET" and path in ("/", "/index.html"):
            return self.reply(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8", SPA_HEADERS, False)
        if method == "GET" and path == "/health":
            return self.send_json(200, {"status": "ok", "center": CENTER_NAME, "auth_required": AUTH_REQUIRED,
                                        "mediapipe": not vendor_missing(), "secure": SECURE})
        if method == "GET" and path == "/status":
            return self.send_json(200, services_status())
        if method == "GET" and path.startswith("/vendor/"):
            return self.h_vendor(path[len("/vendor/"):])
        if method == "POST" and path == "/auth/login":
            return self.h_login()
        if method == "POST" and path == "/auth/logout":
            return self.h_logout()
        if method == "GET" and path == "/auth/me":
            return self.h_me()
        if method == "POST" and path == "/doctors":
            return self.h_doctor_create()
        if path == "/appointments":
            if method == "GET":
                return self.h_appts_list(qs)
            if method == "POST":
                return self.h_appts_create()
        if method == "POST" and path == "/rooms":
            return self.h_rooms_create()
        if method == "GET" and path == "/patients":
            return self.h_patients(qs)
        if method == "GET" and path == "/audit":
            return self.h_audit(qs)
        m = re.fullmatch(r"/patients/(\d+)/(photos|measurements)", path)
        if method == "GET" and m:
            return self.h_patient_sub(int(m.group(1)), m.group(2))
        m = re.fullmatch(r"/photos/(\d+)", path)
        if method == "GET" and m:
            return self.h_photo(int(m.group(1)))
        m = re.fullmatch(r"/rooms/([A-Za-z0-9]{4,12})/(events|join|frames|protocol|measurements|ice-config|info|render-conclusion)", path)
        if m:
            code, sub = m.group(1), m.group(2)
            key = (method, sub)
            table = {
                ("POST", "join"): self.h_join, ("POST", "events"): self.h_events_post,
                ("GET", "events"): lambda c: self.h_events_get(c, qs),
                ("POST", "frames"): self.h_frames, ("GET", "protocol"): self.h_protocol_get,
                ("POST", "protocol"): self.h_protocol_post, ("POST", "measurements"): self.h_meas_post,
                ("GET", "ice-config"): self.h_ice, ("GET", "info"): self.h_info,
                ("POST", "render-conclusion"): self.h_render_conclusion,
            }
            if key in table:
                return table[key](code)
        m = re.fullmatch(r"/rooms/([A-Za-z0-9]{4,12})/files/(\d+)", path)
        if method == "GET" and m:
            return self.h_file_get(m.group(1), int(m.group(2)))
        raise ApiError(404, "not_found", "Ресурс не найден")

    # --- vendor ---
    def h_vendor(self, rel):
        rel = urllib.parse.unquote(rel)
        vd = (DATA_DIR / "vendor").resolve()
        if "\x00" in rel or ".." in rel.replace("\\", "/").split("/"):
            raise ApiError(404, "not_found", "Файл не найден")
        p = (vd / rel).resolve()
        if vd not in p.parents or not p.is_file():
            raise ApiError(404, "not_found", "Файл не найден")
        ctype = VENDOR_MIME.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.reply(200, p.read_bytes(), ctype, {"Cache-Control": "public, max-age=86400"}, False)

    # --- врачи ---
    def h_login(self):
        ip_limit(self.client_address[0], "login", 8, 8 / 300.0)
        b = self.read_json(65536)
        login, pw = clean_text(b.get("login"), 100), str(b.get("password") or "")[:200]
        row = DB.one("SELECT * FROM doctors WHERE login=?", (login,))
        salt = row["salt"] if row else b"\x00" * 16
        calc = hash_pw(pw, salt)
        if not row or not hmac.compare_digest(calc, row["pw"]):
            raise ApiError(401, "bad_credentials", "Неверный логин или пароль")
        sid = secrets.token_urlsafe(32)
        DB.run("INSERT INTO sessions(sid,doctor_id,expires) VALUES(?,?,?)", (sid_hash(sid), row["id"], now() + SESSION_TTL))
        audit("doctor:%s" % row["login"], "login")
        cookie = "mars_sid=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (sid, SESSION_TTL)
        if SECURE:
            cookie += "; Secure"
        self.send_json(200, {"doctor": {"id": row["id"], "name": row["name"], "login": row["login"],
                                        "admin": bool(row["is_admin"])}}, {"Set-Cookie": cookie})

    def h_logout(self):
        self.read_raw(65536)
        sid = self.cookie("mars_sid")
        doc = self.doctor(required=False)
        if sid:
            DB.run("DELETE FROM sessions WHERE sid=?", (sid_hash(sid),))
        if doc:
            audit(self.actor(doc), "logout")
        cookie = "mars_sid=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" + ("; Secure" if SECURE else "")
        self.send_json(200, {"status": "ok"}, {"Set-Cookie": cookie})

    def h_me(self):
        doc = self.doctor(required=False)
        out = {"auth_required": AUTH_REQUIRED, "center": CENTER_NAME, "doctor": None}
        if doc:
            out["doctor"] = {"id": doc["id"], "name": doc["name"], "login": doc["login"], "admin": bool(doc["is_admin"])}
        self.send_json(200, out)

    def h_doctor_create(self):
        doc = self.doctor()
        if not doc["is_admin"]:
            raise ApiError(403, "forbidden", "Только администратор")
        b = self.read_json(65536)
        login, name, pw = clean_text(b.get("login"), 60), clean_text(b.get("name"), 100), str(b.get("password") or "")
        if not login or not name or len(pw) < 8:
            raise ApiError(400, "bad_request", "Нужны логин, имя и пароль не короче 8 символов")
        if DB.one("SELECT id FROM doctors WHERE login=?", (login,)):
            raise ApiError(409, "exists", "Логин занят")
        did = create_doctor(login, name, pw, 1 if b.get("admin") else 0)
        audit(self.actor(doc), "doctor_created", "", "id=%d" % did)
        self.send_json(201, {"id": did})

    def h_audit(self, qs):
        doc = self.doctor()
        if not doc["is_admin"]:
            raise ApiError(403, "forbidden", "Только администратор")
        try:
            lim = max(1, min(1000, int((qs.get("limit") or ["200"])[0])))
        except ValueError:
            lim = 200
        self.send_json(200, {"items": DB.q("SELECT ts,actor,action,room,detail FROM audit ORDER BY id DESC LIMIT ?", (lim,))})

    # --- приёмы ---
    def create_appt(self, doc, patient_id, name, email, when, birth=""):
        if patient_id:
            if not DB.one("SELECT id FROM patients WHERE id=?", (patient_id,)):
                raise ApiError(404, "patient_not_found", "Пациент не найден")
            if email:
                DB.run("UPDATE patients SET email=? WHERE id=?", (email, patient_id))
        elif name:
            patient_id = DB.run("INSERT INTO patients(full_name,birth,email,created) VALUES(?,?,?,?)",
                                (name, birth, email, now()))
        code = new_code()
        did = doc["id"] if doc["id"] else None
        aid = DB.run("INSERT INTO appointments(code,patient_id,doctor_id,scheduled,status,created) VALUES(?,?,?,?, 'planned',?)",
                     (code, patient_id, did, when, now()))
        return aid, code, patient_id

    def appt_dict(self, r):
        st = r["status"]
        with COND:
            room = ROOMS.get(r["code"])
            if st == "planned" and room and room.pat_token and not room.closed:
                st = "waiting"
        return {"id": r["id"], "code": r["code"], "link": "%s/?code=%s" % (self.base_url(), r["code"]),
                "scheduled": r["scheduled"], "status": st, "patient_id": r["patient_id"],
                "patient_name": r.get("patient_name") or "", "email": r.get("patient_email") or "",
                "doctor": r.get("doctor_name") or ""}

    def h_appts_create(self):
        doc = self.doctor()
        b = self.read_json(65536)
        name = clean_text(b.get("full_name"), 200)
        pid = b.get("patient_id")
        pid = int(pid) if str(pid or "").isdigit() else None
        if not pid and not name:
            raise ApiError(400, "bad_request", "Укажите ФИО пациента или выберите существующего")
        email = clean_text(b.get("email"), 200)
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ApiError(400, "bad_email", "Некорректный email")
        when = parse_when(b.get("scheduled"))
        aid, code, pid = self.create_appt(doc, pid, name, email, when, clean_text(b.get("birth"), 20))
        audit(self.actor(doc), "appointment_created", code)
        row = DB.one("SELECT a.*, p.full_name AS patient_name, p.email AS patient_email, d.name AS doctor_name FROM appointments a "
                     "LEFT JOIN patients p ON p.id=a.patient_id LEFT JOIN doctors d ON d.id=a.doctor_id WHERE a.id=?", (aid,))
        out = self.appt_dict(row)
        sent = False
        if out["email"]:
            when_s = datetime.fromtimestamp(when).strftime("%d.%m.%Y %H:%M")
            text = ("Здравствуйте!\n\nВаша консультация в центре «%s» назначена на %s.\n"
                    "Ссылка для подключения: %s\nКод: %s\n\nПерейдите по ссылке за несколько минут до начала.\n"
                    % (CENTER_NAME, when_s, out["link"], code))
            sent = send_mail_async(out["email"], "Приглашение на консультацию", text)
        out["email_sent"] = sent
        self.send_json(201, out)

    def h_appts_list(self, qs):
        doc = self.doctor()
        try:
            t_from = int((qs.get("from") or [now() - 12 * 3600])[0])
            t_to = int((qs.get("to") or [now() + 14 * 86400])[0])
        except ValueError:
            raise ApiError(400, "bad_request", "Некорректный период")
        sql = ("SELECT a.*, p.full_name AS patient_name, p.email AS patient_email, d.name AS doctor_name FROM appointments a "
               "LEFT JOIN patients p ON p.id=a.patient_id LEFT JOIN doctors d ON d.id=a.doctor_id "
               "WHERE a.scheduled BETWEEN ? AND ?")
        args = [t_from, t_to]
        if not doc["is_admin"]:
            sql += " AND (a.doctor_id=? OR a.doctor_id IS NULL)"
            args.append(doc["id"])
        sql += " ORDER BY a.scheduled LIMIT 500"
        self.send_json(200, {"appointments": [self.appt_dict(r) for r in DB.q(sql, tuple(args))]})

    def h_patients(self, qs):
        self.doctor()
        q = clean_text((qs.get("q") or [""])[0], 100)
        rows = DB.q("SELECT id, full_name, birth FROM patients WHERE full_name LIKE ? ORDER BY full_name LIMIT 100",
                    ("%" + q.replace("%", "") + "%",))
        self.send_json(200, {"patients": rows})

    def h_patient_sub(self, pid, sub):
        doc = self.doctor()
        if sub == "photos":
            rows = DB.q("SELECT id, appt_id, pose, ts, size FROM photos WHERE patient_id=? ORDER BY ts DESC LIMIT 500", (pid,))
            return self.send_json(200, {"photos": rows})
        rows = DB.q("SELECT id, appt_id, kind, ts, body FROM measurements WHERE patient_id=? ORDER BY ts LIMIT 2000", (pid,))
        for r in rows:
            try:
                r["data"] = json.loads(r.pop("body"))
            except ValueError:
                r["data"] = {}
        self.send_json(200, {"measurements": rows})

    def h_photo(self, pid):
        doc = self.doctor()
        row = DB.one("SELECT * FROM photos WHERE id=?", (pid,))
        if not row:
            raise ApiError(404, "not_found", "Снимок не найден")
        p = DATA_DIR / row["path"]
        if not p.is_file():
            raise ApiError(404, "not_found", "Файл снимка отсутствует")
        audit(self.actor(doc), "photo_viewed", "", "id=%d" % pid)
        self.reply(200, p.read_bytes(), "image/jpeg")

    # --- комнаты ---
    def h_rooms_create(self):
        doc = self.doctor()
        ip_limit(self.client_address[0], "rooms", 30, 0.2)
        b = self.read_json(65536, allow_empty=True)
        aid = b.get("appointment_id")
        if aid:
            appt = DB.one("SELECT * FROM appointments WHERE id=?", (int(aid),)) if str(aid).isdigit() else None
            if not appt:
                raise ApiError(404, "appointment_not_found", "Приём не найден")
            if appt["status"] == "completed":
                raise ApiError(410, "room_closed", "Консультация завершена")
            if doc["id"] and appt["doctor_id"] and appt["doctor_id"] != doc["id"] and not doc["is_admin"]:
                raise ApiError(403, "forbidden", "Приём назначен другому врачу")
        else:
            aid, code, _ = self.create_appt(doc, None, "", "", now())
            appt = DB.one("SELECT * FROM appointments WHERE id=?", (aid,))
            audit(self.actor(doc), "appointment_created", code)
        room = get_room(appt["code"], create=True)
        with COND:
            if room.closed:
                raise ApiError(410, "room_closed", "Консультация завершена")
            if not room.doc_token:
                room.doc_token = "doc_" + secrets.token_urlsafe(24)
            token = room.doc_token
            room.last = time.time()
        if not appt["doctor_id"] and doc["id"]:
            DB.run("UPDATE appointments SET doctor_id=? WHERE id=?", (doc["id"], appt["id"]))
        self.send_json(201, {"room": room.code, "token": token, "appointment_id": appt["id"]})

    def h_info(self, code):
        ip_limit(self.client_address[0], "info", 30, 0.5)
        code = code.upper()
        appt = DB.one("SELECT a.*, d.name AS doctor_name FROM appointments a LEFT JOIN doctors d ON d.id=a.doctor_id WHERE a.code=?", (code,))
        if not appt:
            raise ApiError(404, "room_not_found", "Комната не найдена")
        with COND:
            r = ROOMS.get(code)
            closed = bool(r and r.closed)
        if appt["status"] == "completed" or closed:
            raise ApiError(410, "room_closed", "Консультация завершена")
        self.send_json(200, {"center": CENTER_NAME, "doctor": appt["doctor_name"] or "", "scheduled": appt["scheduled"],
                             "status": appt["status"]})

    def h_join(self, code):
        ip_limit(self.client_address[0], "join", 20, 0.5)
        b = self.read_json(65536, allow_empty=True)
        room = get_room(code, create=True)
        tok = (self.headers.get("Authorization") or "")
        tok = tok[7:].strip().encode("utf-8", "ignore") if tok.startswith("Bearer ") else b""
        with COND:
            if room.closed:
                raise ApiError(410, "room_closed", "Консультация завершена")
            if room.pat_token:
                if tok and hmac.compare_digest(tok, room.pat_token.encode()):
                    return self.send_json(200, {"token": room.pat_token})
                raise ApiError(409, "room_full", "В комнате уже есть пациент")
            if room.ended:
                raise ApiError(410, "room_closed", "Консультация завершена")
            room.pat_token = "pat_" + secrets.token_urlsafe(24)
            token = room.pat_token
            room.last = time.time()
        audit("patient", "patient_join", room.code)
        if b.get("consent") is True:
            audit("patient", "consent_consultation", room.code)
        self.send_json(200, {"token": token})

    def need_room(self, code, sub_ok_closed=False):
        room = get_room(code)
        with COND:
            closed = room.closed
        if closed:
            raise ApiError(410, "room_closed", "Консультация прервана")
        role = room_role(room, self.headers)
        if not role:
            raise ApiError(401, "unauthorized", "Нет доступа к комнате")
        return room, role

    def h_ice(self, code):
        room, role = self.need_room(code)
        servers = [{"urls": ["stun:stun.l.google.com:19302"]}]
        if TURN_URL and TURN_SECRET:
            user = "%d:%s" % (now() + 86400, room.code)
            cred = base64.b64encode(hmac.new(TURN_SECRET.encode(), user.encode(), hashlib.sha1).digest()).decode()
            servers.append({"urls": [u.strip() for u in TURN_URL.split(",") if u.strip()], "username": user, "credential": cred})
        self.send_json(200, {"iceServers": servers})

    # --- события ---
    def h_events_get(self, code, qs):
        room = get_room(code)
        with COND:
            closed = room.closed
        role = room_role(room, self.headers)
        if not role:
            raise ApiError(401, "unauthorized", "Нет доступа к комнате")
        try:
            after = int((qs.get("after") or ["0"])[0])
            wait = max(0.0, min(20.0, float((qs.get("wait") or ["0"])[0])))
        except ValueError:
            raise ApiError(400, "bad_request", "Некорректные параметры")
        history = (qs.get("history") or ["0"])[0] == "1"
        deadline = time.monotonic() + wait
        with COND:
            while True:
                if room.closed:
                    if role not in room.delivered_kill:
                        room.delivered_kill.add(role)
                        ks = [e for e in room.events if e["type"] == "kill" and e["id"] > after]
                        if ks:
                            return self._send_events(ks, room)
                    raise ApiError(410, "room_closed", "Консультация прервана")
                evs = [e for e in room.events if e["id"] > after and e["to"] in (role, "all")
                       and (not history or e["type"] in PERSIST_TYPES)]
                rem = deadline - time.monotonic()
                if evs or rem <= 0 or self.server.stop_evt.is_set():
                    room.last = time.time()
                    return self._send_events(evs, room)
                COND.wait(min(rem, 5.0))

    def _send_events(self, evs, room):
        out = [{"id": e["id"], "type": e["type"], "from": e["from"], "ts": e["ts"], "data": e["data"]} for e in evs]
        self.send_json(200, {"events": out, "last": room.next_id - 1})

    def h_events_post(self, code):
        room, role = self.need_room(code)
        other = "patient" if role == "doctor" else "doctor"
        raw_cost = 1
        # тип известен только после разбора тела; мягкий лимит применяем после
        b = self.read_json(MAX_FILE_JSON)
        typ = b.get("type")
        if typ not in EVENT_TYPES:
            raise ApiError(400, "bad_type", "Неизвестный тип события")
        data = b.get("data", {})
        if typ not in ("file",) and len(json.dumps(data, ensure_ascii=False)) > 262144:
            raise ApiError(413, "too_large", "Событие слишком большое")
        if typ in SOFT_TYPES:
            if not room.bucket(role, "soft").take():
                return self.send_json(202, {"status": "throttled", "id": 0})
        else:
            if typ == "file":
                raw_cost = 5
            if not room.bucket(role, "main").take(raw_cost):
                raise ApiError(429, "rate_limited", "Слишком много событий")
        appt = appt_by_room(room)
        who = "doctor" if role == "doctor" else "patient"
        if typ in ("offer", "answer", "ice"):
            if not isinstance(data, dict):
                raise ApiError(400, "bad_request", "Некорректные данные сигналинга")
            to, out = other, data
        elif typ == "chat":
            text = clean_text((data or {}).get("text") if isinstance(data, dict) else "", 4000)
            if not text:
                raise ApiError(400, "empty", "Пустое сообщение")
            mid = DB.run("INSERT INTO messages(appt_id,ts,sender,kind,body) VALUES(?,?,?,?,?)",
                         (appt["id"], now(), who, "chat", json.dumps({"text": text}, ensure_ascii=False)))
            to, out = "all", {"text": text, "mid": mid}
        elif typ == "file":
            to, out = "all", self.store_file(room, appt, who, data)
        elif typ == "ehr":
            if role != "doctor":
                raise ApiError(403, "forbidden", "Только врач")
            card = (data or {}).get("card") if isinstance(data, dict) else None
            if not isinstance(card, dict):
                raise ApiError(400, "bad_request", "Нет данных карточки")
            save_card(appt, card)
            to, out = "doctor", {"saved": True}
        elif typ == "document":
            if role != "doctor":
                raise ApiError(403, "forbidden", "Только врач")
            to, out = "all", self.build_document(room, appt)
        elif typ == "control":
            to, out = self.control(room, role, appt, data)
        elif typ in ("presence", "face", "measure"):
            if not isinstance(data, dict):
                raise ApiError(400, "bad_request", "Некорректные данные")
            if typ == "presence" and role == "patient" and data.get("state") == "waiting" and appt["status"] in ("planned", "waiting"):
                DB.run("UPDATE appointments SET status='waiting' WHERE id=?", (appt["id"],))
            to, out = other, data
        elif typ == "kill":
            if role != "doctor":
                raise ApiError(403, "forbidden", "Только врач")
            return self.do_kill(room, appt)
        else:
            raise ApiError(400, "bad_type", "Неизвестный тип события")
        with COND:
            if room.closed:
                raise ApiError(410, "room_closed", "Консультация прервана")
            ev = add_event(room, typ, who, to, out)
        self.send_json(202, {"status": "accepted", "id": ev["id"]})

    def do_kill(self, room, appt):
        with COND:
            if room.closed:
                raise ApiError(410, "room_closed", "Консультация прервана")
            ev = add_event(room, "kill", "doctor", "all", {"reason": "emergency"})
            room.closed = True
            room.delivered_kill.add("doctor")
            COND.notify_all()
        DB.run("UPDATE appointments SET status='completed', ended=? WHERE id=?", (now(), appt["id"]))
        audit("doctor", "emergency_end", room.code)
        self.send_json(202, {"status": "accepted", "id": ev["id"]})

    def control(self, room, role, appt, data):
        if not isinstance(data, dict):
            raise ApiError(400, "bad_request", "Некорректные данные")
        act = clean_text(data.get("action"), 40)
        if not act:
            raise ApiError(400, "bad_request", "Не указано действие")
        other = "patient" if role == "doctor" else "doctor"
        if act == "start":
            if role != "doctor":
                raise ApiError(403, "forbidden", "Только врач")
            loc = load_protocol_json(appt).get("location") or {}
            if not clean_text(loc.get("address")) or not clean_text(loc.get("phone")):
                raise ApiError(400, "location_required", "Укажите адрес и телефон пациента перед началом приёма")
            DB.run("UPDATE appointments SET status='in_progress', started=? WHERE id=?", (now(), appt["id"]))
            audit("doctor", "consultation_start", room.code)
            return other, data
        if act == "end":
            if role != "doctor":
                raise ApiError(403, "forbidden", "Только врач")
            DB.run("UPDATE appointments SET status='completed', ended=? WHERE id=?", (now(), appt["id"]))
            room.ended = True
            audit("doctor", "consultation_end", room.code)
            return "all", data
        if act == "face_consent":
            if role != "patient":
                raise ApiError(403, "forbidden", "Только пациент")
            audit("patient", "consent_face", room.code, "granted" if data.get("granted") else "revoked")
            return other, data
        if act == "reset_patient":
            if role != "doctor":
                raise ApiError(403, "forbidden", "Только врач")
            with COND:
                room.pat_token = None
            audit("doctor", "patient_seat_reset", room.code)
            return "doctor", {"action": "reset_patient"}
        return other, data

    def build_document(self, room, appt):
        row = DB.one("SELECT body FROM conclusions WHERE appt_id=?", (appt["id"],))
        if not row:
            raise ApiError(400, "no_conclusion", "Сначала сохраните заключение")
        concl = json.loads(row["body"])
        doc = DB.one("SELECT name FROM doctors WHERE id=?", (appt["doctor_id"],)) if appt["doctor_id"] else None
        pname = (DB.one("SELECT full_name FROM patients WHERE id=?", (appt["patient_id"],)) or {}).get("full_name", "") if appt["patient_id"] else ""
        payload = {"center": CENTER_NAME, "doctor": (doc or {}).get("name", ""), "patient": pname,
                   "date": datetime.now().strftime("%Y-%m-%d"), "conclusion": concl}
        mid = DB.run("INSERT INTO messages(appt_id,ts,sender,kind,body) VALUES(?,?,?,?,?)",
                     (appt["id"], now(), "doctor", "document", json.dumps(payload, ensure_ascii=False)))
        audit("doctor", "conclusion_sent", room.code)
        payload["mid"] = mid
        return payload

    def store_file(self, room, appt, who, data):
        if not isinstance(data, dict):
            raise ApiError(400, "bad_request", "Некорректные данные файла")
        name = clean_name(data.get("name"))
        ext = os.path.splitext(name)[1].lower()
        if ext not in FILE_TYPES:
            raise ApiError(400, "bad_file_type", "Допустимы PDF, JPEG, PNG, CSV, XLSX, XLS")
        try:
            raw = base64.b64decode(str(data.get("b64") or ""), validate=True)
        except (ValueError, TypeError):
            raise ApiError(400, "bad_base64", "Некорректное содержимое файла")
        if not raw:
            raise ApiError(400, "empty", "Пустой файл")
        if len(raw) > MAX_FILE:
            raise ApiError(413, "too_large", "Файл больше 20 МБ")
        if not check_signature(ext, raw):
            raise ApiError(400, "bad_signature", "Содержимое не соответствует типу файла")
        fid = DB.run("INSERT INTO files(appt_id,ts,sender,name,mime,size) VALUES(?,?,?,?,?,?)",
                     (appt["id"], now(), who, name, FILE_TYPES[ext], len(raw)))
        rel = "files/%d.dat" % fid
        (DATA_DIR / rel).write_bytes(raw)
        DB.run("UPDATE files SET path=? WHERE id=?", (rel, fid))
        meta = {"id": fid, "name": name, "mime": FILE_TYPES[ext], "size": len(raw)}
        DB.run("INSERT INTO messages(appt_id,ts,sender,kind,body) VALUES(?,?,?,?,?)",
               (appt["id"], now(), who, "file", json.dumps(meta, ensure_ascii=False)))
        audit("doctor" if who == "doctor" else "patient", "file_upload", room.code, "id=%d size=%d" % (fid, len(raw)))
        return meta

    def h_file_get(self, code, fid):
        room, role = self.need_room(code)
        row = DB.one("SELECT * FROM files WHERE id=? AND appt_id=?", (fid, room.appt_id))
        if not row or not (DATA_DIR / row["path"]).is_file():
            raise ApiError(404, "not_found", "Файл не найден")
        q = urllib.parse.quote(row["name"])
        ext = os.path.splitext(row["name"])[1] or ".bin"
        self.reply(200, (DATA_DIR / row["path"]).read_bytes(), row["mime"],
                   {"Content-Disposition": "attachment; filename=\"file%s\"; filename*=UTF-8''%s" % (ext, q)})

    # --- снимки ---
    def h_frames(self, code):
        room, role = self.need_room(code)
        if not room.bucket(role, "main").take(3):
            raise ApiError(429, "rate_limited", "Слишком много снимков")
        pose = (self.headers.get("X-Pose") or "free").strip()
        if pose not in POSES:
            raise ApiError(400, "bad_pose", "Неизвестный ракурс")
        raw = self.read_raw(MAX_FRAME)
        if len(raw) < 20 or raw[:3] != b"\xff\xd8\xff":
            raise ApiError(400, "bad_jpeg", "Ожидался JPEG")
        appt = appt_by_room(room)
        ts = now()
        pid = DB.run("INSERT INTO photos(appt_id,patient_id,pose,ts,size) VALUES(?,?,?,?,?)",
                     (appt["id"], appt["patient_id"], pose, ts, len(raw)))
        rel = "photos/%d.jpg" % pid
        (DATA_DIR / rel).write_bytes(raw)
        DB.run("UPDATE photos SET path=? WHERE id=?", (rel, pid))
        audit("doctor" if role == "doctor" else "patient", "photo_saved", room.code, "id=%d pose=%s" % (pid, pose))
        with COND:
            if not room.closed:
                add_event(room, "face", "server", "doctor", {"kind": "photo", "id": pid, "pose": pose, "ts": ts})
        self.send_json(200, {"timestamp": ts, "frame_size": len(raw), "id": pid, "pose": pose, "status": "stored"})

    # --- протокол ---
    def meta(self, appt):
        d = DB.one("SELECT name FROM doctors WHERE id=?", (appt["doctor_id"],)) if appt["doctor_id"] else None
        p = DB.one("SELECT full_name FROM patients WHERE id=?", (appt["patient_id"],)) if appt["patient_id"] else None
        return {"id": appt["id"], "code": appt["code"], "status": appt["status"], "scheduled": appt["scheduled"],
                "doctor": (d or {}).get("name", ""), "patient_name": (p or {}).get("full_name", ""),
                "patient_id": appt["patient_id"], "center": CENTER_NAME}

    def h_render_conclusion(self, code):
        """Печатная форма заключения. Строит сервис reports; если он недоступен, отвечаем 503,
        и браузер печатает сам (запасной путь)."""
        room, role = self.need_room(code)
        b = self.read_json(1024 * 1024)
        payload = {"center": clean_text(b.get("center"), 200), "doctor": clean_text(b.get("doctor"), 200),
                   "patient": clean_text(b.get("patient"), 200), "date": clean_text(b.get("date"), 40),
                   "conclusion": clean_conclusion(b.get("conclusion") or {})}
        if _ServiceClient is None:
            return self.send_json(503, {"error": "service_unavailable", "service": "reports", "degraded": True,
                                        "message": "Сервис отчётов не подключён"}, {"Retry-After": "30"})
        cli = _SVC_CLIENTS.get("reports") or _SVC_CLIENTS.setdefault("reports", _ServiceClient("reports", probe_every=5.0))
        try:
            st, data = cli.call("POST", "/v1/conclusion", payload, timeout=4.0, request_id=self.headers.get("X-Request-ID"))
        except Exception as e:  # noqa: BLE001  (ServiceUnavailable и любые сбои сети)
            ra = str(getattr(e, "retry_after", 10))
            return self.send_json(503, {"error": "service_unavailable", "service": "reports", "degraded": True,
                                        "message": "Сервис отчётов временно недоступен"}, {"Retry-After": ra})
        if st != 200 or not isinstance(data, dict) or "html" not in data:
            return self.send_json(502, {"error": "bad_gateway", "service": "reports", "message": "Некорректный ответ сервиса отчётов"})
        self.send_json(200, {"html": data["html"]})

    def h_protocol_get(self, code):
        room, role = self.need_room(code)
        appt = appt_by_room(room)
        proto = load_protocol_json(appt)
        face = proto.get("face") or {}
        if role == "patient":
            return self.send_json(200, {"appointment": self.meta(appt), "status": appt["status"], "face": face})
        note = DB.one("SELECT body FROM notes WHERE appt_id=?", (appt["id"],))
        concl = DB.one("SELECT body FROM conclusions WHERE appt_id=?", (appt["id"],))
        self.send_json(200, {"appointment": self.meta(appt), "status": appt["status"], "location": proto.get("location") or {},
                             "face": face, "card": patient_card(appt["patient_id"]),
                             "notes": (note or {}).get("body", ""),
                             "conclusion": json.loads(concl["body"]) if concl else None})

    def h_protocol_post(self, code):
        room, role = self.need_room(code)
        if role != "doctor":
            raise ApiError(403, "forbidden", "Только врач")
        b = self.read_json(1024 * 1024)
        appt = appt_by_room(room)
        proto = load_protocol_json(appt)
        if "location" in b:
            loc = b["location"] if isinstance(b["location"], dict) else {}
            proto["location"] = {"address": clean_text(loc.get("address"), 500), "phone": clean_text(loc.get("phone"), 50)}
        if "face" in b and isinstance(b["face"], dict):
            poses = [p for p in (b["face"].get("poses") or []) if p in POSES]
            try:
                tol = max(1.0, min(15.0, float(b["face"].get("tolerance", 5))))
            except (TypeError, ValueError):
                tol = 5.0
            proto["face"] = {"poses": poses or ["frontal"], "tolerance": tol}
        concl = None
        if "conclusion" in b and b["conclusion"] is not None:
            concl = clean_conclusion(b["conclusion"])
        if "card" in b and isinstance(b["card"], dict):
            save_card(appt, b["card"])
        DB.run("UPDATE appointments SET protocol=? WHERE id=?", (json.dumps(proto, ensure_ascii=False), appt["id"]))
        if "notes" in b:
            DB.run("INSERT INTO notes(appt_id,body,updated) VALUES(?,?,?) ON CONFLICT(appt_id) DO UPDATE SET body=excluded.body, updated=excluded.updated",
                   (appt["id"], clean_text(b["notes"], 20000), now()))
        if concl is not None:
            DB.run("INSERT INTO conclusions(appt_id,body,updated) VALUES(?,?,?) ON CONFLICT(appt_id) DO UPDATE SET body=excluded.body, updated=excluded.updated",
                   (appt["id"], json.dumps(concl, ensure_ascii=False), now()))
            audit("doctor", "conclusion_saved", room.code)
        self.send_json(200, {"status": "saved"})

    def h_meas_post(self, code):
        room, role = self.need_room(code)
        b = self.read_json(2 * 1024 * 1024)
        kind = clean_text(b.get("kind"), 20)
        if kind not in ("symmetry", "mobility", "compare", "calibration", "report"):
            raise ApiError(400, "bad_kind", "Неизвестный вид измерения")
        if kind in ("symmetry", "mobility", "compare") and b.get("pose_ok") is not True:
            raise ApiError(400, "pose_out_of_tolerance", "Поза вне допуска: значения не сохраняются")
        cal = b.get("calibration")
        if has_mm_keys(b) and not (isinstance(cal, dict) and cal.get("source")):
            raise ApiError(400, "calibration_required", "Значения в мм без калибровки не сохраняются")
        appt = appt_by_room(room)
        DB.run("INSERT INTO measurements(appt_id,patient_id,kind,ts,body) VALUES(?,?,?,?,?)",
               (appt["id"], appt["patient_id"], kind, now(), json.dumps(b, ensure_ascii=False)))
        audit("doctor" if role == "doctor" else "patient", "measurements_saved", room.code, kind)
        self.send_json(200, {"status": "stored", "kind": kind})


# ----------------------------------------------------------------------------
# Запуск
# ----------------------------------------------------------------------------
_SVC_CLIENTS = {}
_SVC_CACHE = {"t": 0.0, "v": None}
_SVC_LOCK = threading.Lock()


def services_status():
    """Состояние вторичных сервисов для индикатора. Кэш 5 секунд.
    Список сервисов задаёт MARS_SERVICES (его выставляет run_all.py). Пусто: одиночный режим."""
    names = _svc_config.monitored_services() if _svc_config else []
    if not names or _ServiceClient is None:
        return {"status": "ok", "services": {}}
    with _SVC_LOCK:
        if _SVC_CACHE["v"] is not None and time.time() - _SVC_CACHE["t"] < 5:
            return _SVC_CACHE["v"]
    out = {}

    def probe(n):
        c = _SVC_CLIENTS.get(n) or _SVC_CLIENTS.setdefault(n, _ServiceClient(n, probe_every=5.0))
        try:
            c.call("GET", "/ready", timeout=1.5)
            out[n] = {"up": True}
        except Exception:  # noqa: BLE001
            out[n] = {"up": False}

    ths = [threading.Thread(target=probe, args=(n,), daemon=True) for n in names]
    for t in ths:
        t.start()
    for t in ths:
        t.join(2.5)
    for n in names:
        out.setdefault(n, {"up": False})
    res = {"status": "ok" if all(v["up"] for v in out.values()) else "degraded", "services": out}
    with _SVC_LOCK:
        _SVC_CACHE.update(t=time.time(), v=res)
    return res


class Srv(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        log.debug("соединение закрыто: %s", client_address)


class Runtime:
    def __init__(self, httpd, db, stop_evt, cond):
        self.httpd, self.db, self.stop_evt, self.cond = httpd, db, stop_evt, cond
        self.thread = None
        self.stopped = False

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.stop_evt.set()
        with self.cond:
            self.cond.notify_all()
        try:
            if self.thread is not None and self.thread.is_alive():
                self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        if self.thread is not None:
            self.thread.join(5)
        self.db.close()


def in_jupyter():
    return "ipykernel" in sys.modules


def main():
    global DB, _MARS_RUNNING
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for sub in ("", "files", "photos", "vendor"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    DB = Database(DATA_DIR / "mars.sqlite3")
    bootstrap_admin()
    ensure_vendor()
    stop_evt = threading.Event()
    try:
        httpd = Srv(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print("Не удалось занять порт %d: %s" % (PORT, e))
        DB.close()
        return
    httpd.stop_evt = stop_evt
    if SECURE:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT_FILE, KEY_FILE)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    rt = Runtime(httpd, DB, stop_evt, COND)
    _MARS_RUNNING = rt
    threading.Thread(target=cleanup_loop, args=(stop_evt,), daemon=True).start()
    scheme = "https" if SECURE else "http"
    print("M.A.R.S.: %s://localhost:%d  (центр: %s)" % (scheme, PORT, CENTER_NAME))
    if not SECURE:
        print("Камера и микрофон работают только на https:// и http://localhost. Для центра задайте CERT_FILE и KEY_FILE.")
    if not AUTH_REQUIRED:
        print("ВНИМАНИЕ: AUTH_REQUIRED=False, функции врача открыты без входа. Только для отладки.")
    if in_jupyter():
        rt.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        rt.thread.start()
        print("Сервер запущен в фоновом потоке. Повторный запуск ячейки остановит его.")
        return
    rt.thread = threading.current_thread()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка...")
    finally:
        rt.thread = None
        rt.stop()


