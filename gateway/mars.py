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
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, appt_id INTEGER NOT NULL, kind TEXT NOT NULL,
  key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
  next_try INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL, updated INTEGER NOT NULL,
  input TEXT NOT NULL, result TEXT, error TEXT, UNIQUE(appt_id, key));
CREATE INDEX IF NOT EXISTS ix_jobs_queue ON jobs(status, next_try);
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
            DB.run("DELETE FROM jobs WHERE created<?", (now() - JOB_KEEP,))
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
        m = re.fullmatch(r"/rooms/([A-Za-z0-9]{4,12})/analyze", path)
        if method == "POST" and m:
            return self.h_analyze(m.group(1))
        m = re.fullmatch(r"/rooms/([A-Za-z0-9]{4,12})/jobs/(\d+)", path)
        if method == "GET" and m:
            return self.h_job_get(m.group(1), int(m.group(2)))
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

    # --- анализ лица (сервис vision, через очередь заданий) ---
    def h_analyze(self, code):
        """Ставит расчёт в очередь и отвечает 202 с номером задания. Повтор с тем же key не создаёт дубля.
        Без сервиса vision (одиночный режим) сразу 503: браузер считает сам."""
        room, role = self.need_room(code)
        if not room.bucket(role, "main").take(1):
            raise ApiError(429, "rate_limited", "Слишком много запросов")
        b = self.read_json(4 * 1024 * 1024)
        kind = clean_text(b.get("kind"), 20)
        if kind not in VISION_KINDS:
            raise ApiError(400, "bad_kind", "Неизвестный вид анализа")
        key = clean_text(b.get("key"), 64)
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", key or ""):
            raise ApiError(400, "bad_key", "Нужен ключ задания (8-64 символа)")
        if not isinstance(b.get("input"), dict):
            raise ApiError(400, "bad_input", "Нет данных для анализа")
        if not vision_enabled():
            return self.send_json(503, {"error": "service_unavailable", "service": "vision", "degraded": True,
                                        "message": "Сервис анализа не подключён"}, {"Retry-After": "30"})
        appt = appt_by_room(room)
        t = now()
        DB.run("INSERT OR IGNORE INTO jobs(appt_id,kind,key,created,updated,input) VALUES(?,?,?,?,?,?)",
               (appt["id"], kind, key, t, t, json.dumps(b["input"], ensure_ascii=False)))
        job = DB.one("SELECT * FROM jobs WHERE appt_id=? AND key=?", (appt["id"], key))
        JOBS_EVT.set()
        self.send_json(202, job_view(job))

    def h_job_get(self, code, jid):
        room, role = self.need_room(code)
        job = DB.one("SELECT * FROM jobs WHERE id=? AND appt_id=?", (jid, appt_by_room(room)["id"]))
        if not job:
            raise ApiError(404, "not_found", "Задание не найдено")
        self.send_json(200, job_view(job))

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


# Очередь заданий для vision: задания переживают перезапуск, повторы с нарастающей паузой
VISION_KINDS = ("symmetry", "mobility", "compare", "calibrate")
JOBS_EVT = threading.Event()
JOB_MAX_ATTEMPTS = 20
JOB_KEEP = 86400  # входные данные (точки лица) хранятся сутки


def vision_enabled():
    return _ServiceClient is not None and _svc_config is not None and "vision" in _svc_config.monitored_services()


def job_view(job):
    out = {"id": job["id"], "kind": job["kind"], "status": job["status"], "attempts": job["attempts"],
           "created": job["created"], "updated": job["updated"]}
    if job["status"] == "done" and job["result"]:
        out["result"] = json.loads(job["result"])
    if job["error"]:
        out["error"] = json.loads(job["error"])
    if job["status"] == "queued" and job["attempts"]:
        out["retry_at"] = job["next_try"]
    return out


def run_job(job):
    cli = _SVC_CLIENTS.get("vision") or _SVC_CLIENTS.setdefault("vision", _ServiceClient("vision", probe_every=5.0))
    t = now()
    try:
        st, data = cli.call("POST", "/v1/" + job["kind"], json.loads(job["input"]), timeout=10.0, request_id="job%d" % job["id"])
    except Exception as e:  # noqa: BLE001  (ServiceUnavailable и сбои сети: повторим позже)
        n = job["attempts"] + 1
        err = json.dumps({"error": "service_unavailable", "service": "vision", "message": "Сервис анализа временно недоступен"}, ensure_ascii=False)
        if n >= JOB_MAX_ATTEMPTS:
            DB.run("UPDATE jobs SET status='error', attempts=?, updated=?, error=? WHERE id=?", (n, t, err, job["id"]))
        else:
            DB.run("UPDATE jobs SET attempts=?, next_try=?, updated=?, error=? WHERE id=?",
                   (n, t + min(60, 2 ** n), t, err, job["id"]))
        log.info("задание %d: vision недоступен (%s), попытка %d", job["id"], e, n)
        return False
    if st == 200 and isinstance(data, dict):
        DB.run("UPDATE jobs SET status='done', updated=?, result=?, error=NULL WHERE id=?",
               (t, json.dumps(data, ensure_ascii=False), job["id"]))
    else:
        err = data if isinstance(data, dict) else {"error": "bad_response", "service": "vision", "message": "HTTP %d" % st}
        DB.run("UPDATE jobs SET status='error', updated=?, error=? WHERE id=?", (t, json.dumps(err, ensure_ascii=False), job["id"]))
    return True


def jobs_loop(stop_evt):
    while not stop_evt.is_set():
        JOBS_EVT.wait(1.0)
        JOBS_EVT.clear()
        if not vision_enabled():
            continue
        try:
            for job in DB.q("SELECT * FROM jobs WHERE status='queued' AND next_try<=? ORDER BY id LIMIT 10", (now(),)):
                if stop_evt.is_set() or not run_job(job):
                    break  # сервис недоступен: остальные ждут своей очереди
        except Exception:
            log.exception("очередь заданий")


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
    threading.Thread(target=jobs_loop, args=(stop_evt,), daemon=True).start()
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




INDEX_HTML = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>M.A.R.S.</title>
<style>
:root{--bg:#fff;--bg2:#f2f2f7;--bg3:#fff;--fg:#1c1c1e;--fg2:#6c6c70;--sep:#d1d1d6;--acc:#007AFF;--acct:#0062CC;--ok:#34C759;--okt:#1e7e34;--bad:#FF3B30;--badt:#C62828;--warn:#FF9500;--r1:10px;--r2:14px;--fs:15px;--bar:rgba(255,255,255,.72)}
@media (prefers-color-scheme:dark){:root{--bg:#000;--bg2:#1c1c1e;--bg3:#2c2c2e;--fg:#fff;--fg2:#a1a1a6;--sep:#38383a;--acc:#0A84FF;--acct:#4DA3FF;--okt:#34C759;--badt:#FF6961;--bar:rgba(28,28,30,.72)}}
html.big{--fs:19px}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:400 var(--fs)/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer;border:0;background:none;padding:0}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
h1,h2,h3{margin:0;font-weight:600}
h1{font-size:34px;line-height:1.15}h2{font-size:22px}h3{font-size:17px}
.sm{font-size:13px}.mut{color:var(--fg2)}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.hidden{display:none!important}
#app{height:100dvh;display:flex;flex-direction:column;padding-bottom:env(safe-area-inset-bottom)}
header.top{flex:none;height:52px;display:flex;align-items:center;gap:12px;padding:0 16px;padding-top:env(safe-area-inset-top);box-sizing:content-box;background:var(--bar);backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px);border-bottom:1px solid var(--sep);position:relative;z-index:30}
.brand{font-weight:600;font-size:17px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brand span{color:var(--fg2);font-weight:400}
.sp{flex:1}
.net{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--fg2)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--sep);display:inline-block}
.dot.ok{background:var(--ok)}.dot.mid{background:var(--warn)}.dot.bad{background:var(--bad)}
.codebox{display:flex;align-items:center;gap:6px;font-size:13px;min-height:32px;padding:0 10px;border-radius:var(--r1);background:var(--bg2)}
main#main{flex:1;min-height:0;position:relative;overflow:auto}
.page{max-width:720px;margin:0 auto;padding:24px 16px 48px}
.page.wide{max-width:960px}
.center-col{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:16px;padding:24px 16px}
.stack{display:flex;flex-direction:column;gap:12px}.row{display:flex;align-items:center;gap:8px}.rowb{display:flex;align-items:center;justify-content:space-between;gap:8px}.wrap{flex-wrap:wrap}
.btn{min-height:44px;padding:0 20px;border-radius:var(--r1);font-weight:600;display:inline-flex;align-items:center;justify-content:center;gap:8px;transition:opacity .2s,transform .2s;white-space:nowrap}
.btn:active{transform:scale(.98)}.btn[disabled]{opacity:.4;pointer-events:none}
.btn.pri{background:var(--acc);color:#fff}.btn.sec{background:var(--bg2);color:var(--acct)}.btn.dng{background:var(--bad);color:#fff}.btn.txt{color:var(--acct);padding:0 8px}
.btn.lg{min-height:52px;font-size:17px;padding:0 28px;border-radius:var(--r2)}
.ibtn{width:44px;height:44px;border-radius:50%;display:inline-grid;place-items:center;color:var(--acct)}
.ibtn:hover{background:var(--bg2)}
svg.ic{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round;flex:none}
.field{display:flex;flex-direction:column;gap:4px;text-align:left}
.field label{font-size:13px;color:var(--fg2)}
.inp{min-height:44px;width:100%;padding:8px 12px;border-radius:var(--r1);border:1px solid var(--sep);background:var(--bg3)}
textarea.inp{min-height:88px;resize:vertical}
.inp:focus{outline:2px solid var(--acc);outline-offset:-1px}
.list{background:var(--bg2);border-radius:var(--r2);overflow:hidden}
.li{display:flex;align-items:center;gap:12px;padding:12px 16px;min-height:44px;background:var(--bg3)}
.li+.li{border-top:1px solid var(--sep)}
.li .grow{flex:1;min-width:0}
.li .t{overflow:hidden;text-overflow:ellipsis}
.chip{display:inline-flex;align-items:center;min-height:24px;padding:0 10px;border-radius:12px;font-size:13px;background:var(--bg2);color:var(--fg2)}
.chip.g{background:rgba(52,199,89,.16);color:var(--okt)}.chip.b{background:rgba(0,122,255,.14);color:var(--acct)}.chip.o{background:rgba(255,149,0,.16);color:#A15C00}
@media (prefers-color-scheme:dark){.chip.o{color:#FFB340}}
.seg{display:inline-flex;background:var(--bg2);border-radius:var(--r1);padding:2px;gap:2px}
.seg button{min-height:32px;padding:0 12px;border-radius:8px;font-size:13px;color:var(--fg)}
.seg button[aria-pressed=true],.seg button[aria-selected=true]{background:var(--bg3);font-weight:600}
@media (prefers-color-scheme:light){.seg button[aria-pressed=true],.seg button[aria-selected=true]{box-shadow:0 0 0 1px var(--sep)}}
.banner{padding:10px 16px;font-size:15px;display:flex;align-items:center;gap:12px;background:rgba(0,122,255,.12);color:var(--fg)}
.banner.warn{background:rgba(255,149,0,.18)}.banner.bad{background:rgba(255,59,48,.14)}
#sheets{position:fixed;inset:0;z-index:60;pointer-events:none}
.scrim{position:absolute;inset:0;background:rgba(0,0,0,.4);pointer-events:auto;opacity:0;transition:opacity .2s}
.sheet{position:absolute;left:50%;top:50%;width:min(560px,calc(100vw - 24px));max-height:calc(100dvh - 48px);overflow:auto;background:var(--bg);border-radius:var(--r2);padding:20px;pointer-events:auto;transform:translate(-50%,-48%);opacity:0;transition:opacity .2s,transform .2s}
.scrim.on{opacity:1}.sheet.on{opacity:1;transform:translate(-50%,-50%)}
.sheet.wide{width:min(900px,calc(100vw - 24px))}
@media (max-width:700px){.sheet{top:auto;bottom:0;left:0;width:100%;border-radius:var(--r2) var(--r2) 0 0;transform:translateY(8px)}.sheet.on{transform:none}}
#toasts{position:fixed;left:50%;top:64px;transform:translateX(-50%);z-index:80;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none;width:min(520px,calc(100vw - 24px))}
.toast{background:var(--fg);color:var(--bg);padding:10px 16px;border-radius:var(--r1);font-size:15px;opacity:0;transform:translateY(-6px);transition:opacity .2s,transform .2s}
.toast.on{opacity:1;transform:none}.toast.err{background:var(--bad);color:#fff}
.sk{background:var(--bg2);border-radius:var(--r1);min-height:16px;opacity:.7;animation:pulse 1.2s ease-in-out infinite}
@keyframes pulse{50%{opacity:.35}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
/* раскладка консультации */
.dlayout{height:100%;display:grid;grid-template-columns:280px minmax(0,1fr) 380px;min-height:0}
.dlayout.pat{grid-template-columns:minmax(0,1fr) 380px}
.dlayout.pat.noside{grid-template-columns:minmax(0,1fr)}
.dlayout.pat.noside .rside{display:none}
.lside,.rside,.center{min-height:0;min-width:0;display:flex;flex-direction:column}
.lside{border-right:1px solid var(--sep);overflow:auto;padding:16px;gap:16px;background:var(--bg2)}
.rside{border-left:1px solid var(--sep)}
.rside .tabs{flex:none;display:flex;gap:2px;padding:8px;overflow-x:auto;border-bottom:1px solid var(--sep)}
.rside .tabs button{min-height:36px;padding:0 12px;border-radius:8px;white-space:nowrap;font-size:13px}
.rside .tabs button[aria-selected=true]{background:var(--bg2);font-weight:600}
.pane{flex:1;min-height:0;overflow:auto;padding:16px;display:none;flex-direction:column;gap:12px}
.pane.on{display:flex}
.center{padding:12px;gap:8px;background:var(--bg)}
.only-narrow{display:none!important}
.pbar{display:none}
@media (max-width:1099px){.dlayout{grid-template-columns:minmax(0,1fr) 360px}.lside{display:none}.only-narrow{display:inline-flex!important}}
@media (max-width:700px){
 .dlayout,.dlayout.pat{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr) auto}
 .dlayout .center,.dlayout .rside{grid-row:1;display:none}
 .dlayout[data-ptab=video] .center{display:flex}
 .dlayout:not([data-ptab=video]) .rside{display:flex}
 .dlayout .rside .tabs{display:none}
 .pbar{display:flex;grid-row:2;border-top:1px solid var(--sep);background:var(--bar);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);overflow-x:auto}
 .pbar button{flex:1;min-width:64px;min-height:50px;font-size:11px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;color:var(--fg2)}
 .pbar button[aria-selected=true]{color:var(--acct)}
 .dlayout.pat.noside .rside{display:none}
}
/* видео */
.callwrap{position:relative;flex:1;min-height:0;background:#000;border-radius:var(--r2);overflow:hidden;color:#fff}
.vstage{position:absolute;inset:0;transform-origin:50% 50%;will-change:transform}
.vstage video{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000}
canvas.ov{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.mirror{transform:scaleX(-1)}
.selfview{position:absolute;right:12px;top:56px;width:132px;aspect-ratio:3/4;border-radius:var(--r1);overflow:hidden;background:#222;touch-action:none;cursor:grab;z-index:5;border:1px solid rgba(255,255,255,.3)}
.selfview video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.vtop{position:absolute;left:0;right:0;top:0;z-index:6;display:flex;align-items:center;gap:12px;padding:12px 16px;font-size:15px;text-shadow:0 0 2px rgba(0,0,0,.6);pointer-events:none}
.vtop .grow{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.callbar{position:absolute;left:50%;transform:translateX(-50%);bottom:calc(12px + env(safe-area-inset-bottom));z-index:8;display:flex;gap:12px;padding:8px 12px;border-radius:32px;background:rgba(28,28,30,.55);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);max-width:calc(100% - 16px);overflow-x:auto}
.cb{width:48px;height:48px;border-radius:50%;flex:none;background:rgba(255,255,255,.18);color:#fff;display:grid;place-items:center;transition:transform .2s,opacity .2s}
.cb:active{transform:scale(.94)}
.cb[aria-pressed=true]{background:#fff;color:#000}
.cb.end{background:var(--bad);color:#fff}
.vmsg{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;text-align:center;padding:24px;z-index:2;font-size:17px}
.hint{position:absolute;left:12px;right:12px;bottom:84px;z-index:7;text-align:center;font-size:22px;font-weight:600;color:#fff;text-shadow:0 0 3px rgba(0,0,0,.8);pointer-events:none}
.pqual{position:absolute;left:12px;top:56px;z-index:7;display:flex;align-items:center;gap:6px;font-size:13px;background:rgba(0,0,0,.45);padding:4px 10px;border-radius:12px;color:#fff}
.frame-ok{position:absolute;inset:6px;border:2px solid var(--ok);border-radius:var(--r2);pointer-events:none;z-index:4}
.meter{height:8px;border-radius:4px;background:var(--bg2);overflow:hidden}.meter>i{display:block;height:100%;width:0;background:var(--ok);transform-origin:left}
/* чат и файлы */
.msgs{flex:1;overflow:auto;display:flex;flex-direction:column;gap:8px;min-height:120px}
.msg{max-width:88%;padding:8px 12px;border-radius:var(--r2);background:var(--bg2);align-self:flex-start;word-break:break-word;white-space:pre-wrap}
.msg.me{align-self:flex-end;background:var(--acc);color:#fff}
.msg .tm{font-size:11px;opacity:.7;display:block;margin-top:2px}
.msg img.pv{max-width:100%;border-radius:var(--r1);display:block;margin-top:4px}
.sheetrow{display:flex;gap:8px}
table.tb{width:100%;border-collapse:collapse;font-size:13px}
table.tb th,table.tb td{padding:6px 8px;border-bottom:1px solid var(--sep);text-align:left}
table.tb td.n,table.tb th.n{text-align:right;font-variant-numeric:tabular-nums}
.thumbs{display:grid;grid-template-columns:repeat(auto-fill,minmax(84px,1fr));gap:8px}
.thumbs figure{margin:0;position:relative}.thumbs img{width:100%;aspect-ratio:3/4;object-fit:cover;border-radius:8px;background:var(--bg2);display:block}
.thumbs figcaption{font-size:11px;color:var(--fg2)}
.thumbs button.sel img{outline:3px solid var(--acc)}
.note{font-size:13px;color:var(--fg2)}
.big-hint{font-size:22px;font-weight:600;text-align:center}
.cmp{position:relative;background:#000;border-radius:var(--r1);overflow:hidden}
.cmp canvas{display:block;width:100%;height:auto}
/* печать */
#printArea{display:none}
@media print{
 body>*:not(#printArea){display:none!important}
 #printArea{display:block!important;color:#000;background:#fff;padding:16mm;font-size:12pt}
 #printArea h1{font-size:20pt}#printArea h2{font-size:14pt;margin-top:12pt}
 #printArea table{width:100%;border-collapse:collapse}#printArea td,#printArea th{border-bottom:1px solid #999;padding:3pt 4pt;text-align:left}
 #printArea .sign{margin-top:36pt;border-top:1px solid #000;width:60%;padding-top:4pt}
 #printArea img{max-height:50mm}
}
</style>
</head>
<body>
<div id="app"></div>
<div id="sheets"></div>
<div id="toasts" aria-live="polite"></div>
<div id="printArea"></div>
<script>
'use strict';
/* ===== Вспомогательные функции ===== */
const $ = (s, r) => (r || document).querySelector(s);
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) for (const k in attrs) {
    const v = attrs[k];
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'style') el.style.cssText = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
}
const ICONS = {
  mic: 'M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z|M5 11a7 7 0 0 0 14 0|M12 18v3',
  micoff: 'M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z|M5 11a7 7 0 0 0 14 0|M12 18v3|M4 4l16 16',
  video: 'M3 7h11a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H3z|M16 11l5-3v8l-5-3',
  videooff: 'M3 7h11a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H3z|M16 11l5-3v8l-5-3|M4 4l16 16',
  screen: 'M3 5h18v11H3z|M8 20h8|M12 16v4',
  mesh: 'M12 3c4 0 6 3 6 8s-2 10-6 10-6-5-6-10 2-8 6-8z|M6 11h12|M12 3v18|M8 7h8|M8 15h8',
  more: 'M5 12h1|M11.5 12h1|M18 12h1',
  end: 'M3 15c5-5 13-5 18 0l-2 3-4-2v-2c-2-1-4-1-6 0v2l-4 2z',
  chat: 'M4 5h16v11H9l-5 4z',
  file: 'M6 3h8l4 4v14H6z|M14 3v4h4',
  doc: 'M6 3h8l4 4v14H6z|M14 3v4h4|M9 12h6|M9 16h6',
  face: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z|M9 10h.01|M15 10h.01|M9 15c1.5 1.5 4.5 1.5 6 0',
  send: 'M4 12l16-8-6 16-2-7z',
  plus: 'M12 5v14|M5 12h14',
  close: 'M6 6l12 12|M18 6L6 18',
  copy: 'M8 8h11v12H8z|M5 16V4h11',
  user: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z|M4 21c0-4 4-6 8-6s8 2 8 6',
  calendar: 'M4 6h16v14H4z|M4 10h16|M8 3v4|M16 3v4',
  check: 'M5 12l5 5 9-10',
  full: 'M4 9V4h5|M20 9V4h-5|M4 15v5h5|M20 15v5h-5',
  menu: 'M4 7h16|M4 12h16|M4 17h16',
  alert: 'M12 4l9 16H3z|M12 10v4|M12 17h.01',
  text: 'M3 19l5-13 5 13|M5 14h6|M15 19v-6a2 2 0 0 1 4 0v6|M15 16h4',
  download: 'M12 4v11|M7 11l5 5 5-5|M5 20h14',
  upload: 'M12 16V5|M7 9l5-5 5 5|M5 20h14',
  camera: 'M4 8h4l2-3h4l2 3h4v11H4z|M12 17a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z',
  logout: 'M10 4H4v16h6|M15 8l5 4-5 4|M9 12h11',
  audio: 'M5 14v-3a7 7 0 0 1 14 0v3|M5 14h3v5H5z|M16 14h3v5h-3z',
  list: 'M8 6h12|M8 12h12|M8 18h12|M4 6h.01|M4 12h.01|M4 18h.01',
  video2: 'M4 5h7v14H4z|M13 5h7v14h-7z',
  card: 'M4 5h16v14H4z|M4 10h16|M7 15h4'
};
function icon(name) {
  const NS = 'http://www.w3.org/2000/svg';
  const s = document.createElementNS(NS, 'svg');
  s.setAttribute('viewBox', '0 0 24 24');
  s.setAttribute('class', 'ic');
  s.setAttribute('aria-hidden', 'true');
  for (const d of (ICONS[name] || '').split('|')) {
    const p = document.createElementNS(NS, 'path');
    p.setAttribute('d', d);
    s.appendChild(p);
  }
  return s;
}
const pad2 = n => String(n).padStart(2, '0');
const fmtTime = ts => { const d = new Date(ts * 1000); return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); };
const fmtDate = ts => { const d = new Date(ts * 1000); return pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + '.' + d.getFullYear(); };
const fmtDT = ts => fmtDate(ts) + ' ' + fmtTime(ts);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const median = arr => { if (!arr.length) return null; const s = arr.slice().sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const POSE_NAMES = { frontal: 'Анфас', left45: 'Поворот влево 45', right45: 'Поворот вправо 45', left90: 'Левый профиль 90', right90: 'Правый профиль 90', free: 'Свободный' };

/* ===== Единое состояние с подпиской ===== */
const State = {
  v: { role: null, view: '', status: 'planned', rtc: 'idle', quality: null, net: true, faceConsent: false, faceMode: 'off', peerHere: false },
  subs: [],
  set(p) {
    const old = Object.assign({}, this.v);
    Object.assign(this.v, p);
    for (const f of this.subs) { try { f(this.v, p, old); } catch (e) { console.error(e); } }
  },
  sub(f) { this.subs.push(f); }
};
const Cfg = { center: 'Медицинский центр', mediapipe: false, auth: true, secure: false };

/* ===== Уведомления ===== */
const Toast = {
  show(text, kind, ms) {
    const box = $('#toasts');
    const t = h('div', { class: 'toast' + (kind === 'err' ? ' err' : ''), role: 'status', text });
    box.append(t);
    requestAnimationFrame(() => t.classList.add('on'));
    setTimeout(() => { t.classList.remove('on'); setTimeout(() => t.remove(), 250); }, ms || 3500);
  },
  err(e) { this.show(typeof e === 'string' ? e : (e && e.message) || 'Ошибка', 'err', 5000); }
};

/* ===== Общий rAF-цикл ===== */
const Loop = {
  fns: new Set(), running: false,
  add(f) { this.fns.add(f); this.start(); return f; },
  del(f) { this.fns.delete(f); },
  start() {
    if (this.running) return;
    this.running = true;
    const tick = t => {
      if (!this.running) return;
      for (const f of this.fns) { try { f(t); } catch (e) { console.error(e); } }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  },
  stop() { this.running = false; this.fns.clear(); }
};

/* ===== Ui ===== */
const Ui = {
  sheets: [],
  sheet(opt) {
    const root = $('#sheets');
    const scrim = h('div', { class: 'scrim' });
    const body = h('div', { class: 'stack' });
    const sheet = h('div', { class: 'sheet' + (opt.wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-label': opt.title || 'Диалог' });
    const api = { el: body, close: () => { Ui.sheets = Ui.sheets.filter(s => s !== api); scrim.classList.remove('on'); sheet.classList.remove('on'); setTimeout(() => { scrim.remove(); sheet.remove(); if (!Ui.sheets.length) root.style.pointerEvents = 'none'; }, 220); if (opt.onClose) opt.onClose(); } };
    if (opt.title) sheet.append(h('div', { class: 'rowb', style: 'margin-bottom:12px' }, h('h2', { text: opt.title }),
      opt.noClose ? null : h('button', { class: 'ibtn', 'aria-label': 'Закрыть', onclick: api.close }, icon('close'))));
    sheet.append(body);
    if (opt.actions && opt.actions.length) {
      sheet.append(h('div', { class: 'row wrap', style: 'margin-top:16px;justify-content:flex-end' },
        opt.actions.map(a => h('button', { class: 'btn ' + (a.kind || 'sec'), onclick: async () => { if (a.onClick) { const r = await a.onClick(api); if (r === false) return; } if (!a.keep) api.close(); } }, a.label))));
    }
    if (!opt.noClose) scrim.addEventListener('click', api.close);
    root.style.pointerEvents = 'auto';
    root.append(scrim, sheet);
    requestAnimationFrame(() => { scrim.classList.add('on'); sheet.classList.add('on'); const f = sheet.querySelector('input,select,textarea,button.pri,button'); if (f) f.focus(); });
    this.sheets.push(api);
    return api;
  },
  closeAll() { for (const s of this.sheets.slice()) s.close(); },
  confirm(title, text, okLabel, danger) {
    return new Promise(res => {
      let done = false;
      const fin = v => { if (!done) { done = true; res(v); } };
      const s = this.sheet({
        title, onClose: () => fin(false),
        actions: [{ label: 'Отмена', kind: 'sec', onClick: () => fin(false) }, { label: okLabel || 'Подтвердить', kind: danger ? 'dng' : 'pri', onClick: () => fin(true) }]
      });
      s.el.append(h('p', { text, style: 'margin:0' }));
    });
  },
  field(label, input, id) {
    id = id || 'f' + Math.random().toString(36).slice(2, 8);
    input.id = id;
    return h('div', { class: 'field' }, h('label', { for: id, text: label }), input);
  },
  input(type, ph, val) { const i = h('input', { class: 'inp', type: type || 'text', placeholder: ph || '' }); if (val != null) i.value = val; return i; },
  statusChip(s) {
    const m = { planned: ['Запланирован', 'chip'], waiting: ['Пациент ожидает', 'chip o'], in_progress: ['Идёт', 'chip g'], completed: ['Завершён', 'chip'] };
    const x = m[s] || m.planned;
    return h('span', { class: x[1], text: x[0] });
  },
  async copy(text, msg) {
    try { await navigator.clipboard.writeText(text); Toast.show(msg || 'Скопировано'); }
    catch (e) {
      const t = h('textarea', { style: 'position:fixed;opacity:0' }); t.value = text; document.body.append(t); t.select();
      try { document.execCommand('copy'); Toast.show(msg || 'Скопировано'); } catch (e2) { Toast.err('Не удалось скопировать'); }
      t.remove();
    }
  },
  segmented(items, cur, onChange, label) {
    const box = h('div', { class: 'seg', role: 'group', 'aria-label': label || '' });
    const btns = items.map(([val, text]) => h('button', { 'aria-pressed': String(val === cur), onclick: () => { cur = val; btns.forEach((b, i) => b.setAttribute('aria-pressed', String(items[i][0] === cur))); onChange(val); } }, text));
    box.append(...btns);
    return box;
  },
  download(name, text, mime) {
    const a = h('a', { href: URL.createObjectURL(new Blob([text], { type: mime || 'application/json' })), download: name });
    document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  },
  big() { const on = !document.documentElement.classList.contains('big'); document.documentElement.classList.toggle('big', on); try { localStorage.setItem('mars_big', on ? '1' : '0'); } catch (e) { /* без хранилища */ } },
  sound() {
    try {
      const AC = window.AudioContext || window.webkitAudioContext; if (!AC) return;
      Ui.ac = Ui.ac || new AC();
      const o = Ui.ac.createOscillator(), g = Ui.ac.createGain();
      o.type = 'sine'; o.frequency.value = 660; g.gain.value = 0.04;
      o.connect(g); g.connect(Ui.ac.destination); o.start(); o.stop(Ui.ac.currentTime + 0.18);
    } catch (e) { /* звук необязателен */ }
  },
  closeAudio() { try { if (Ui.ac) Ui.ac.close(); } catch (e) { /* ignore */ } Ui.ac = null; }
};

/* ===== Api ===== */
class ApiErr extends Error { constructor(status, code, msg) { super(msg); this.status = status; this.code = code; } }
const Api = {
  room: null, token: null,
  async req(method, path, opt) {
    opt = opt || {};
    const hd = Object.assign({}, opt.headers || {});
    let body = opt.raw;
    if (opt.json !== undefined) { hd['Content-Type'] = 'application/json'; body = JSON.stringify(opt.json); }
    const tok = opt.token === false ? null : (opt.token || this.token);
    if (tok && path.startsWith('/rooms')) hd['Authorization'] = 'Bearer ' + tok;
    let res;
    try { res = await fetch(path, { method, headers: hd, body, signal: opt.signal, credentials: 'same-origin', keepalive: !!opt.keepalive }); }
    catch (e) { if (e && e.name === 'AbortError') throw e; throw new ApiErr(0, 'network', 'Нет связи с сервером'); }
    if (opt.blob && res.ok) return res.blob();
    let data = null; try { data = await res.json(); } catch (e) { /* не JSON */ }
    if (!res.ok) throw new ApiErr(res.status, (data && data.error) || 'error', (data && data.message) || ('Ошибка ' + res.status));
    return data;
  },
  get(p, o) { return this.req('GET', p, o); },
  post(p, json, o) { return this.req('POST', p, Object.assign({}, o, { json })); },
  ev(type, data) { return this.post('/rooms/' + this.room + '/events', { type, data: data || {} }); },
  /* событие при закрытии вкладки */
  bye() { if (this.room && this.token) { try { this.req('POST', '/rooms/' + this.room + '/events', { json: { type: 'presence', data: { state: 'left' } }, keepalive: true }).catch(() => { }); } catch (e) { /* ignore */ } } }
};

/* ===== Auth ===== */
const Auth = {
  doctor: null,
  async init() {
    const me = await Api.get('/auth/me', { token: false });
    Cfg.center = me.center; Cfg.auth = me.auth_required; this.doctor = me.doctor;
    return me.doctor;
  },
  showLogin(msg) {
    State.set({ role: null, view: 'login' });
    Header.update();
    const user = Ui.input('text'), pw = Ui.input('password');
    user.autocomplete = 'username'; pw.autocomplete = 'current-password';
    const err = h('p', { class: 'sm', style: 'color:var(--badt);margin:0;min-height:20px', role: 'alert', text: msg || '' });
    const go = async e => {
      e.preventDefault(); err.textContent = '';
      try {
        const r = await Api.post('/auth/login', { login: user.value.trim(), password: pw.value }, { token: false });
        this.doctor = r.doctor; App.afterLogin();
      } catch (ex) { err.textContent = ex.status === 429 ? 'Слишком много попыток. Подождите несколько минут.' : ex.message; }
    };
    $('#main').replaceChildren(h('div', { class: 'center-col' },
      h('h1', { text: 'Вход для врача' }), h('p', { class: 'mut', text: Cfg.center }),
      h('form', { class: 'stack', style: 'width:min(360px,100%)', onsubmit: go },
        Ui.field('Логин', user), Ui.field('Пароль', pw), err,
        h('button', { class: 'btn pri lg', type: 'submit', text: 'Войти' })),
      h('button', { class: 'btn txt', onclick: () => Session.askCode(), text: 'Я пациент: ввести код' })));
    user.focus();
  },
  async logout() {
    try { await Api.post('/auth/logout', {}, { token: false }); } catch (e) { /* уже вышли */ }
    this.doctor = null; App.teardown(); history.replaceState(null, '', location.pathname); this.showLogin();
  }
};

/* ===== Приёмы (врач) ===== */
const Appointments = {
  timer: null,
  dayStart() { const d = new Date(); d.setHours(0, 0, 0, 0); return Math.floor(d / 1000); },
  async show() {
    State.set({ view: 'appointments' }); Header.update();
    clearInterval(this.timer);
    const list = h('div', { class: 'stack' }, h('div', { class: 'sk', style: 'height:64px' }), h('div', { class: 'sk', style: 'height:64px' }));
    $('#main').replaceChildren(h('div', { class: 'page wide' }, h('div', { class: 'rowb', style: 'margin-bottom:16px' }, h('h1', { text: 'Приёмы' }),
      h('button', { class: 'btn pri', onclick: () => this.newSheet() }, icon('plus'), 'Новый приём')), list));
    const load = async () => {
      if (State.v.view !== 'appointments') { clearInterval(this.timer); return; }
      try {
        const from = this.dayStart(), to = from + 14 * 86400;
        const r = await Api.get('/appointments?from=' + from + '&to=' + to);
        this.render(list, r.appointments);
      } catch (e) { if (e.status === 401) { Auth.showLogin('Сессия истекла'); } else list.replaceChildren(h('p', { class: 'mut', text: e.message })); }
    };
    await load(); this.timer = setInterval(load, 15000);
  },
  render(box, items) {
    if (!items.length) { box.replaceChildren(h('p', { class: 'mut', text: 'На ближайшие дни приёмов нет. Нажмите «Новый приём».' })); return; }
    const days = new Map();
    for (const a of items) { const k = fmtDate(a.scheduled); if (!days.has(k)) days.set(k, []); days.get(k).push(a); }
    const today = fmtDate(Math.floor(Date.now() / 1000));
    box.replaceChildren(...[...days].map(([k, arr]) => h('div', { class: 'stack' },
      h('h3', { text: k === today ? 'Сегодня, ' + k : k }),
      h('div', { class: 'list' }, arr.map(a => h('div', { class: 'li' },
        h('span', { class: 'mono', text: fmtTime(a.scheduled) }),
        h('div', { class: 'grow' }, h('div', { class: 't', text: a.patient_name || 'Без имени' }), h('div', { class: 'sm mut' }, 'Код ', h('span', { class: 'mono', text: a.code }))),
        Ui.statusChip(a.status),
        h('button', { class: 'ibtn', 'aria-label': 'Скопировать ссылку', title: 'Скопировать ссылку-приглашение', onclick: () => Ui.copy(a.link, 'Ссылка скопирована') }, icon('copy')),
        a.status === 'completed' ? null : h('button', { class: 'btn pri', onclick: () => Doctor.open(a.id) }, 'Открыть')))))));
  },
  async newSheet() {
    const name = Ui.input('text', 'Фамилия Имя Отчество'); name.setAttribute('list', 'ptlist'); name.autocomplete = 'off';
    const dl = h('datalist', { id: 'ptlist' });
    const map = new Map();
    const when = Ui.input('datetime-local'); const d = new Date(Date.now() + 3600e3); d.setMinutes(0, 0, 0);
    when.value = d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + 'T' + pad2(d.getHours()) + ':00';
    const email = Ui.input('email', 'необязательно');
    const err = h('p', { class: 'sm', style: 'color:var(--badt);margin:0', role: 'alert' });
    let t = null;
    name.addEventListener('input', () => {
      clearTimeout(t); t = setTimeout(async () => {
        if (name.value.trim().length < 2) return;
        try { const r = await Api.get('/patients?q=' + encodeURIComponent(name.value.trim())); map.clear(); dl.replaceChildren(); for (const p of r.patients) { map.set(p.full_name, p.id); dl.append(h('option', { value: p.full_name })); } } catch (e) { /* подсказки необязательны */ }
      }, 250);
    });
    const s = Ui.sheet({
      title: 'Новый приём', actions: [{ label: 'Отмена', kind: 'sec' }, {
        label: 'Создать', kind: 'pri', keep: true, onClick: async api => {
          err.textContent = '';
          if (!name.value.trim()) { err.textContent = 'Укажите ФИО пациента'; return false; }
          try {
            const body = { scheduled: Math.floor(new Date(when.value).getTime() / 1000), email: email.value.trim() };
            if (map.has(name.value.trim())) body.patient_id = map.get(name.value.trim()); else body.full_name = name.value.trim();
            const r = await Api.post('/appointments', body);
            api.close(); this.created(r); this.show();
          } catch (e) { err.textContent = e.message; return false; }
          return false;
        }
      }]
    });
    s.el.append(Ui.field('ФИО (начните вводить, чтобы выбрать существующего пациента)', name), dl, Ui.field('Дата и время', when), Ui.field('Email для приглашения', email), err);
  },
  created(r) {
    const s = Ui.sheet({ title: 'Приём создан', actions: [{ label: 'Готово', kind: 'pri' }] });
    s.el.append(h('p', { class: 'mut', style: 'margin:0', text: 'Передайте пациенту ссылку или код. Регистрация пациенту не нужна.' }),
      h('div', { class: 'list' },
        h('div', { class: 'li' }, h('div', { class: 'grow' }, h('div', { class: 'sm mut', text: 'Ссылка-приглашение' }), h('div', { class: 't mono sm', text: r.link })), h('button', { class: 'ibtn', 'aria-label': 'Скопировать ссылку', onclick: () => Ui.copy(r.link) }, icon('copy'))),
        h('div', { class: 'li' }, h('div', { class: 'grow' }, h('div', { class: 'sm mut', text: 'Код' }), h('div', { class: 'mono', style: 'font-size:22px', text: r.code })), h('button', { class: 'ibtn', 'aria-label': 'Скопировать код', onclick: () => Ui.copy(r.code) }, icon('copy')))),
      h('p', { class: 'sm mut', style: 'margin:0', text: r.email_sent ? 'Письмо с приглашением отправлено на email пациента.' : 'Автоматическая отправка писем не настроена (SMTP не задан).' }));
  }
};

/* ===== Media ===== */
const Media = {
  stream: null, cam: null, screen: null, micOn: true, camOn: true, audioOnly: false, sharing: false, sel: { a: '', v: '' }, ac: null,
  explain(e) {
    const n = e && e.name;
    if (!window.isSecureContext) return 'Камера и микрофон работают только на защищённом соединении (https://) или на localhost. Откройте страницу по https-адресу центра.';
    if (n === 'NotAllowedError' || n === 'SecurityError') return 'Доступ к камере или микрофону запрещён. Разрешите его в настройках браузера (значок замка рядом с адресом) и нажмите «Повторить».';
    if (n === 'NotFoundError' || n === 'OverconstrainedError') return 'Камера или микрофон не найдены. Подключите устройство или выберите другое.';
    if (n === 'NotReadableError') return 'Устройство занято другим приложением. Закройте его и нажмите «Повторить».';
    return 'Не удалось получить доступ к устройствам: ' + ((e && e.message) || n || 'неизвестная ошибка');
  },
  async start(opt) {
    opt = opt || {};
    this.stop();
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw Object.assign(new Error('API'), { name: 'SecurityError' });
    const audio = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
    if (this.sel.a) audio.deviceId = { exact: this.sel.a };
    const video = { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } };
    if (this.sel.v) video.deviceId = { exact: this.sel.v }; else video.facingMode = 'user';
    let s = null, vErr = null;
    if (!opt.audioOnly) {
      try { s = await navigator.mediaDevices.getUserMedia({ audio, video }); }
      catch (e) { vErr = e; if (e.name === 'NotAllowedError' || e.name === 'SecurityError') throw e; }
    }
    if (!s) {
      s = await navigator.mediaDevices.getUserMedia({ audio });
      this.audioOnly = true; this.camOn = false;
      if (vErr) Toast.show('Камера недоступна, включён режим только аудио', null, 5000);
    } else { this.audioOnly = false; this.camOn = true; }
    this.stream = s; this.cam = s.getVideoTracks()[0] || null; this.micOn = true;
    return s;
  },
  stop() {
    for (const st of [this.stream, this.screen]) if (st) st.getTracks().forEach(t => t.stop());
    this.stream = null; this.cam = null; this.screen = null; this.sharing = false;
    if (this.ac) { try { this.ac.close(); } catch (e) { /* ignore */ } this.ac = null; }
  },
  async devices() {
    try { const d = await navigator.mediaDevices.enumerateDevices(); return { a: d.filter(x => x.kind === 'audioinput'), v: d.filter(x => x.kind === 'videoinput') }; }
    catch (e) { return { a: [], v: [] }; }
  },
  async switchDevice(kind, id) {
    this.sel[kind] = id;
    const keepMic = this.micOn, keepCam = this.camOn;
    const old = this.stream;
    const ns = await this.start({ audioOnly: this.audioOnly });
    this.micOn = keepMic; ns.getAudioTracks().forEach(t => t.enabled = keepMic);
    if (this.cam) this.cam.enabled = keepCam, this.camOn = keepCam;
    if (Rtc.pc) { for (const t of ns.getTracks()) { const s = Rtc.senders[t.kind]; if (s && !(t.kind === 'video' && this.sharing)) await s.replaceTrack(t); } }
    CallUi.setLocal(ns); if (old) old.getTracks().forEach(t => t.stop());
  },
  toggleMic() { this.micOn = !this.micOn; if (this.stream) this.stream.getAudioTracks().forEach(t => t.enabled = this.micOn); return this.micOn; },
  toggleCam() { if (!this.cam) { Toast.show('Камера недоступна'); return false; } this.camOn = !this.camOn; this.cam.enabled = this.camOn; return this.camOn; },
  setAudioOnly(on) { if (!this.cam) return; this.cam.enabled = !on; this.camOn = !on; this.audioOnly = on; },
  async shareScreen() {
    if (!navigator.mediaDevices.getDisplayMedia) { Toast.err('Демонстрация экрана не поддерживается этим браузером'); return false; }
    try {
      const s = await navigator.mediaDevices.getDisplayMedia({ video: true });
      const t = s.getVideoTracks()[0];
      this.screen = s; this.sharing = true;
      t.onended = () => this.stopShare();
      if (Rtc.senders.video) await Rtc.senders.video.replaceTrack(t);
      CallUi.shareChanged(true); return true;
    } catch (e) { if (e.name !== 'NotAllowedError') Toast.err('Не удалось начать демонстрацию экрана'); return false; }
  },
  async stopShare() {
    if (!this.sharing) return;
    this.sharing = false;
    if (this.screen) this.screen.getTracks().forEach(t => t.stop());
    this.screen = null;
    if (Rtc.senders.video && this.cam) { try { await Rtc.senders.video.replaceTrack(this.cam); } catch (e) { /* ignore */ } }
    CallUi.shareChanged(false);
  },
  /* максимальное разрешение для съёмки лица: отдельное применение ограничений */
  async applyMax() {
    const t = this.cam; if (!t || !t.applyConstraints) return null;
    try {
      const c = t.getCapabilities ? t.getCapabilities() : {};
      await t.applyConstraints({ width: { ideal: (c.width && c.width.max) || 3840 }, height: { ideal: (c.height && c.height.max) || 2160 } });
    } catch (e) { /* остаёмся на текущем */ }
    return t.getSettings();
  },
  async restore720() { const t = this.cam; if (t && t.applyConstraints) { try { await t.applyConstraints({ width: { ideal: 1280 }, height: { ideal: 720 } }); } catch (e) { /* ignore */ } } }
};

/* ===== WebRTC ===== */
const Rtc = {
  pc: null, ice: [], queue: [], makingOffer: false, ignoreOffer: false, polite: false, timer: null, senders: {}, remote: null,
  setState(s) { if (State.v.rtc !== s) State.set({ rtc: s }); },
  async init() {
    const c = await Api.get('/rooms/' + Api.room + '/ice-config');
    this.ice = c.iceServers; this.polite = State.v.role === 'patient';
    this.setState(Media.stream ? 'media_ready' : 'idle');
  },
  create() {
    this.close(true);
    const pc = new RTCPeerConnection({ iceServers: this.ice });
    this.pc = pc; this.queue = []; this.senders = {}; this.makingOffer = false; this.ignoreOffer = false;
    if (Media.stream) for (const t of Media.stream.getTracks()) this.senders[t.kind] = pc.addTrack(t, Media.stream);
    if (!Media.stream || !Media.cam) pc.addTransceiver('video', { direction: 'recvonly' });
    pc.onicecandidate = e => { if (e.candidate) Api.ev('ice', { candidate: e.candidate.toJSON() }).catch(() => { }); };
    pc.ontrack = e => { this.remote = e.streams[0] || new MediaStream([e.track]); CallUi.setRemote(this.remote); };
    pc.onnegotiationneeded = () => { if (State.v.role === 'doctor' || this.started) this.offer(false); };
    pc.oniceconnectionstatechange = () => this.onIce(pc);
    this.setState('signaling');
    return pc;
  },
  started: false,
  async startAsDoctor() { this.started = true; this.create(); },
  /* полный перезапуск (например, пациент перезагрузил страницу) */
  async hardRestart() {
    try { await Api.ev('control', { action: 'rtc_reset' }); } catch (e) { /* пациента нет */ }
    this.started = true; this.create();
  },
  async offer(restart) {
    const pc = this.pc; if (!pc) return;
    try {
      this.makingOffer = true;
      const o = await pc.createOffer(restart ? { iceRestart: true } : undefined);
      if (pc.signalingState !== 'stable') return;
      await pc.setLocalDescription(o);
      await Api.ev('offer', { sdp: { type: o.type, sdp: o.sdp } });
      this.setState(State.v.rtc === 'reconnecting' ? 'reconnecting' : 'connecting');
    } catch (e) { if (!(e instanceof ApiErr && e.status === 410)) { console.error(e); Toast.err('Не удалось установить соединение, пробую ещё раз'); } }
    finally { this.makingOffer = false; }
  },
  async flush() {
    const q = this.queue; this.queue = [];
    for (const c of q) { try { await this.pc.addIceCandidate(c); } catch (e) { console.warn('ice', e); } }
  },
  async handle(ev) {
    const d = ev.data || {};
    try {
      if (ev.type === 'offer') {
        if (!this.pc) { this.started = true; this.create(); }
        const pc = this.pc;
        const collision = this.makingOffer || pc.signalingState !== 'stable';
        this.ignoreOffer = !this.polite && collision;
        if (this.ignoreOffer) return;
        await pc.setRemoteDescription(d.sdp); await this.flush();
        const a = await pc.createAnswer(); await pc.setLocalDescription(a);
        await Api.ev('answer', { sdp: { type: a.type, sdp: a.sdp } });
        this.setState('connecting');
      } else if (ev.type === 'answer') {
        if (!this.pc || this.pc.signalingState !== 'have-local-offer') return;
        await this.pc.setRemoteDescription(d.sdp); await this.flush();
      } else if (ev.type === 'ice') {
        if (!this.pc || !this.pc.remoteDescription) { this.queue.push(d.candidate); return; }
        try { await this.pc.addIceCandidate(d.candidate); } catch (e) { if (!this.ignoreOffer) console.warn('ice', e); }
      }
    } catch (e) { console.error(e); Toast.err('Ошибка установки соединения'); }
  },
  onIce(pc) {
    if (pc !== this.pc) return;
    const s = pc.iceConnectionState;
    clearTimeout(this.timer);
    if (s === 'connected' || s === 'completed') { this.setState('connected'); CallUi.callStarted(); }
    else if (s === 'disconnected') { this.setState('reconnecting'); this.timer = setTimeout(() => { if (pc === this.pc && pc.iceConnectionState === 'disconnected') this.restartIce(); }, 3000); }
    else if (s === 'failed') { this.setState('reconnecting'); this.restartIce(); }
  },
  restartIce() {
    if (!this.pc) return;
    if (State.v.role === 'doctor') { try { this.pc.restartIce(); } catch (e) { /* старые браузеры */ } this.offer(true); }
    else Api.ev('control', { action: 'restart_ice' }).catch(() => { });
  },
  close(keep) {
    clearTimeout(this.timer);
    if (this.pc) { const pc = this.pc; this.pc = null; pc.onicecandidate = pc.ontrack = pc.onnegotiationneeded = pc.oniceconnectionstatechange = null; try { pc.close(); } catch (e) { /* ignore */ } }
    this.queue = []; this.senders = {}; this.remote = null;
    if (!keep) { this.started = false; this.setState('closed'); }
  }
};

/* ===== Качество связи ===== */
const Quality = {
  timer: null, prev: null, bad: 0,
  start() { this.stop(); this.timer = setInterval(() => this.poll(), 2000); },
  stop() { clearInterval(this.timer); this.timer = null; this.prev = null; this.bad = 0; State.set({ quality: null }); },
  async poll() {
    const pc = Rtc.pc; if (!pc || State.v.rtc !== 'connected') return;
    let lost = 0, recv = 0, rtt = null;
    try {
      const st = await pc.getStats();
      st.forEach(r => {
        if (r.type === 'inbound-rtp' && !r.isRemote) { lost += r.packetsLost || 0; recv += r.packetsReceived || 0; }
        if (r.type === 'candidate-pair' && r.nominated && r.currentRoundTripTime != null) rtt = r.currentRoundTripTime;
      });
    } catch (e) { return; }
    const p = this.prev; this.prev = { lost, recv };
    let loss = 0; if (p) { const dl = lost - p.lost, dr = recv - p.recv; loss = dl + dr > 0 ? Math.max(0, dl) / (dl + dr) : 0; }
    let level = 'good';
    if (loss > 0.06 || (rtt != null && rtt > 0.5)) level = 'bad'; else if (loss > 0.02 || (rtt != null && rtt > 0.25)) level = 'mid';
    State.set({ quality: { level, loss, rtt } });
    if (level === 'bad') { if (++this.bad === 2) Toast.show('Связь плохая. Включите режим «Только аудио» через кнопку «Ещё».', null, 7000); } else this.bad = 0;
  }
};

/* ===== Поллинг событий ===== */
const Poller = {
  last: 0, run: false, ctl: null, fail: 0, seen: new Set(), handler: null,
  async start(handler) {
    this.stop(); this.run = true; this.handler = handler; this.ctl = new AbortController(); this.fail = 0; this.seen = new Set();
    try {
      const r = await Api.get('/rooms/' + Api.room + '/events?after=0&history=1', { signal: this.ctl.signal });
      this.last = r.last || 0; for (const ev of r.events) this.dispatch(ev);
    } catch (e) { if (this.fatal(e)) return; }
    this.loop();
  },
  fatal(e) {
    if (e && (e.status === 410 || e.status === 401 || e.status === 404)) { this.run = false; App.roomGone(e); return true; }
    return false;
  },
  async loop() {
    while (this.run) {
      try {
        const r = await Api.get('/rooms/' + Api.room + '/events?after=' + this.last + '&wait=15', { signal: this.ctl.signal });
        if (this.fail) { this.fail = 0; App.setOffline(false); }
        for (const ev of r.events) { this.last = Math.max(this.last, ev.id); this.dispatch(ev); }
        if (!r.events.length && r.last > this.last) this.last = r.last;
      } catch (e) {
        if (!this.run || (e && e.name === 'AbortError')) return;
        if (this.fatal(e)) return;
        this.fail++; if (this.fail >= 2) App.setOffline(true);
        await sleep(Math.min(10000, 1000 * Math.pow(2, this.fail)));
      }
    }
  },
  dispatch(ev) {
    if (this.seen.has(ev.id)) return;
    this.seen.add(ev.id); if (this.seen.size > 2000) this.seen = new Set([...this.seen].slice(-1000));
    this.last = Math.max(this.last, ev.id);
    try { this.handler(ev); } catch (e) { console.error(e); }
  },
  stop() { this.run = false; if (this.ctl) { try { this.ctl.abort(); } catch (e) { /* ignore */ } } this.ctl = null; }
};

/* ===== Пациент: код, согласие, проверка оборудования, ожидание ===== */
const CONSENT_TEXT = 'Я даю согласие на проведение консультации в дистанционной форме (видеосвязь) и на обработку моих персональных данных в объёме, необходимом для оказания медицинской помощи. Я понимаю, что дистанционная консультация не заменяет очный осмотр. Текст согласия задаётся центром.'; // шаблон заменяется юристом центра
const Session = {
  code: null, hb: null,
  key() { return 'mars_pt_' + this.code; },
  askCode(msg) {
    State.set({ role: 'patient', view: 'code' }); Header.update();
    const inp = Ui.input('text', 'Код из приглашения'); inp.maxLength = 8; inp.className += ' mono'; inp.style.cssText = 'text-align:center;font-size:22px;text-transform:uppercase';
    const err = h('p', { class: 'sm', style: 'color:var(--badt);margin:0;min-height:20px', role: 'alert', text: msg || '' });
    const go = e => { e.preventDefault(); const c = inp.value.toUpperCase().replace(/[^A-Z0-9]/g, ''); if (c.length < 4) { err.textContent = 'Введите код из приглашения'; return; } this.start(c); };
    $('#main').replaceChildren(h('div', { class: 'center-col' }, h('h1', { text: 'Консультация' }), h('p', { class: 'mut', text: Cfg.center }),
      h('form', { class: 'stack', style: 'width:min(340px,100%)', onsubmit: go }, Ui.field('Код консультации', inp), err, h('button', { class: 'btn pri lg', type: 'submit', text: 'Продолжить' })),
      Auth.doctor || Cfg.auth ? h('button', { class: 'btn txt', onclick: () => Auth.showLogin(), text: 'Вход для врача' }) : null));
    inp.focus();
  },
  async start(code) {
    this.code = code.toUpperCase().replace(/[^A-Z0-9]/g, '');
    State.set({ role: 'patient', view: 'join' }); Header.update();
    let info;
    try { info = await Api.get('/rooms/' + this.code + '/info', { token: false }); }
    catch (e) { this.askCode(e.status === 404 ? 'Комната не найдена. Проверьте код.' : e.status === 410 ? 'Эта консультация уже завершена.' : e.message); return; }
    let saved = null; try { saved = sessionStorage.getItem(this.key()); } catch (e) { /* нет хранилища */ }
    if (saved) { Api.room = this.code; Api.token = saved; return this.join(); }
    this.showConsent(info);
  },
  showConsent(info) {
    const cb = h('input', { type: 'checkbox', id: 'consent', style: 'width:24px;height:24px;flex:none' });
    const btn = h('button', { class: 'btn pri lg', disabled: true, onclick: () => this.join(true) }, 'Продолжить');
    cb.addEventListener('change', () => { btn.disabled = !cb.checked; });
    $('#main').replaceChildren(h('div', { class: 'page' }, h('div', { class: 'stack' },
      h('h1', { text: info.center }),
      h('div', { class: 'list' },
        h('div', { class: 'li' }, icon('user'), h('div', { class: 'grow' }, h('div', { class: 'sm mut', text: 'Врач' }), h('div', { text: info.doctor || 'Врач центра' }))),
        h('div', { class: 'li' }, icon('calendar'), h('div', { class: 'grow' }, h('div', { class: 'sm mut', text: 'Время' }), h('div', { text: fmtDT(info.scheduled) })))),
      h('h3', { text: 'Согласие' }),
      h('p', { style: 'margin:0', text: CONSENT_TEXT }),
      h('label', { class: 'row', for: 'consent', style: 'min-height:44px;gap:12px' }, cb, h('span', { text: 'Я согласен(на) на дистанционную консультацию и обработку персональных данных' })),
      btn)));
  },
  async join(withConsent) {
    try {
      const r = await Api.post('/rooms/' + this.code + '/join', { consent: true }, { token: Api.token || undefined });
      Api.room = this.code; Api.token = r.token;
      try { sessionStorage.setItem(this.key(), r.token); } catch (e) { /* ignore */ }
    } catch (e) {
      Api.token = null;
      if (e.status === 409) this.askCode('В комнате уже есть пациент. Если это вы, обратитесь в центр.');
      else this.askCode(e.status === 410 ? 'Эта консультация уже завершена.' : e.status === 404 ? 'Комната не найдена.' : e.message);
      return;
    }
    this.precheck();
  },
  async precheck() {
    State.set({ view: 'precheck' }); Header.update();
    const vid = h('video', { autoplay: true, muted: true, playsinline: true, class: 'mirror', style: 'width:100%;aspect-ratio:4/3;object-fit:cover;background:#000;border-radius:14px' });
    const msg = h('div', { class: 'banner warn hidden', role: 'alert' });
    const meter = h('div', { class: 'meter', role: 'progressbar', 'aria-label': 'Уровень микрофона' }, h('i'));
    const selV = h('select', { class: 'inp' }), selA = h('select', { class: 'inp' });
    const go = h('button', { class: 'btn pri lg', disabled: true }, 'Войти в комнату ожидания');
    const retry = h('button', { class: 'btn sec hidden' }, 'Повторить');
    const noVid = h('button', { class: 'btn sec hidden' }, 'Продолжить без видео');
    let raf = null, an = null;
    const stopMeter = () => { if (raf) Loop.del(raf); raf = null; };
    const startMeter = stream => {
      stopMeter();
      try {
        const AC = window.AudioContext || window.webkitAudioContext; Media.ac = new AC();
        const src = Media.ac.createMediaStreamSource(stream); an = Media.ac.createAnalyser(); an.fftSize = 512; src.connect(an);
        const buf = new Uint8Array(an.fftSize);
        raf = Loop.add(() => { an.getByteTimeDomainData(buf); let m = 0; for (const b of buf) m = Math.max(m, Math.abs(b - 128)); meter.firstChild.style.width = Math.min(100, m / 64 * 100) + '%'; });
      } catch (e) { /* без индикатора */ }
    };
    const fillDevices = async () => {
      const d = await Media.devices();
      for (const [sel, list, k, nm] of [[selV, d.v, 'v', 'Камера'], [selA, d.a, 'a', 'Микрофон']]) {
        sel.replaceChildren(); list.forEach((x, i) => sel.append(h('option', { value: x.deviceId, text: x.label || (nm + ' ' + (i + 1)) })));
        const cur = (k === 'v' ? Media.cam : Media.stream && Media.stream.getAudioTracks()[0]); const id = cur && cur.getSettings().deviceId; if (id) sel.value = id;
      }
    };
    const open = async opt => {
      msg.classList.add('hidden'); retry.classList.add('hidden'); noVid.classList.add('hidden');
      try {
        const s = await Media.start(opt); vid.srcObject = s; startMeter(s); await fillDevices(); go.disabled = false;
        if (Media.audioOnly) { msg.textContent = 'Камера недоступна: консультация будет только по аудио.'; msg.classList.remove('hidden'); }
      } catch (e) {
        msg.textContent = Media.explain(e); msg.classList.remove('hidden'); retry.classList.remove('hidden'); if (window.isSecureContext) noVid.classList.remove('hidden');
      }
    };
    retry.onclick = () => open(); noVid.onclick = () => open({ audioOnly: true });
    selV.onchange = async () => { Media.sel.v = selV.value; await open(); };
    selA.onchange = async () => { Media.sel.a = selA.value; await open(); };
    go.onclick = () => { stopMeter(); this.enterWaiting(); };
    $('#main').replaceChildren(h('div', { class: 'page' }, h('div', { class: 'stack' }, h('h1', { text: 'Проверка оборудования' }),
      !window.isSecureContext ? h('div', { class: 'banner bad', role: 'alert', text: 'Страница открыта без защищённого соединения: камера и микрофон будут недоступны. Откройте ссылку по https://.' }) : null,
      vid, msg, Ui.field('Камера', selV), Ui.field('Микрофон', selA), h('div', { class: 'field' }, h('label', { text: 'Уровень микрофона: скажите что-нибудь' }), meter),
      h('div', { class: 'row wrap' }, retry, noVid), go)));
    await open();
  },
  async enterWaiting() {
    try { await Rtc.init(); } catch (e) { Toast.err(e); }
    let proto = null; try { proto = await Api.get('/rooms/' + Api.room + '/protocol'); } catch (e) { if (Poller.fatal(e)) return; }
    State.set({ view: 'call', status: proto ? proto.status : 'planned', peerHere: false });
    App.proto = proto || {};
    CallUi.build();
    Poller.start(Events.handle);
    const beat = () => { if (State.v.status !== 'in_progress' || State.v.rtc !== 'connected') Api.ev('presence', { state: State.v.status === 'in_progress' ? 'online' : 'waiting' }).catch(() => { }); };
    beat(); clearInterval(this.hb); this.hb = setInterval(beat, 10000);
  }
};

/* ===== Врач: открытие комнаты ===== */
const Doctor = {
  async open(id) {
    State.set({ role: 'doctor', view: 'loading' }); Header.update();
    $('#main').replaceChildren(h('div', { class: 'center-col' }, h('div', { class: 'sk', style: 'width:240px;height:24px' }), h('p', { class: 'mut', text: 'Открываю комнату' })));
    clearInterval(Appointments.timer);
    try {
      const r = await Api.post('/rooms', { appointment_id: id });
      Api.room = r.room; Api.token = r.token; this.apptId = id;
      history.replaceState(null, '', location.pathname + '#appt=' + id);
    } catch (e) { Toast.err(e); if (e.status === 401) Auth.showLogin('Сессия истекла'); else Appointments.show(); return; }
    let proto = {};
    try { proto = await Api.get('/rooms/' + Api.room + '/protocol'); } catch (e) { Toast.err(e); Appointments.show(); return; }
    App.proto = proto;
    try { await Media.start(); }
    catch (e) { try { await Media.start({ audioOnly: true }); Toast.show('Камера недоступна: аудио только'); } catch (e2) { Toast.show(Media.explain(e2), 'err', 8000); } }
    try { await Rtc.init(); } catch (e) { Toast.err(e); }
    State.set({ view: 'call', status: proto.status, peerHere: proto.status === 'waiting', faceConsent: false });
    CallUi.build(); Record.load(proto);
    Poller.start(Events.handle);
    if (proto.status === 'waiting') Events.patientWaiting(true);
    if (proto.status === 'in_progress') { CallUi.setStatus('in_progress'); Rtc.hardRestart(); }
  }
};
/* ===== Состояние сервисов ===== */
const Services = {
  st: null, timer: null,
  down() { return this.st && this.st.services ? Object.keys(this.st.services).filter(k => !this.st.services[k].up) : []; },
  async poll() {
    try { const r = await fetch('/status', { cache: 'no-store' }); this.st = r.ok ? await r.json() : null; } catch (e) { this.st = null; }
    Header.update();
  },
  start() { this.poll(); this.timer = setInterval(() => this.poll(), 15000); }
};


/* ===== Шапка ===== */
const Header = {
  el: null,
  update() {
    if (!this.el) return;
    const v = State.v, inRoom = !!Api.room && v.view === 'call';
    const kids = [h('div', { class: 'brand' }, Cfg.center, ' ', h('span', { text: 'M.A.R.S.' })), h('div', { class: 'sp' })];
    const down = Services.down();
    if (down.length) kids.push(h('div', { class: 'net', role: 'status', title: 'Недоступны: ' + down.join(', ') }, h('span', { class: 'dot bad' }), h('span', { class: 'only-wide', text: 'Сервисы: ' + down.length })));
    kids.push(h('div', { class: 'net', role: 'status' }, h('span', { class: 'dot ' + (v.net ? 'ok' : 'bad') }), h('span', { class: 'only-wide', text: v.net ? 'В сети' : 'Нет связи' })));
    if (inRoom) kids.push(h('div', { class: 'codebox' }, h('span', { class: 'mut', text: 'Код' }), h('span', { class: 'mono', text: Api.room }),
      h('button', { class: 'ibtn', style: 'width:32px;height:32px', 'aria-label': 'Скопировать код сессии', title: 'Скопировать код', onclick: () => Ui.copy(Api.room, 'Код скопирован') }, icon('copy'))));
    kids.push(h('button', { class: 'ibtn', 'aria-label': 'Меню', title: 'Меню', onclick: () => this.menu() }, icon('menu')));
    this.el.replaceChildren(...kids);
  },
  menu() {
    const v = State.v, inRoom = !!Api.room && v.view === 'call', doc = v.role === 'doctor';
    const s = Ui.sheet({ title: 'Меню' });
    const item = (ic, text, fn, danger) => h('button', { class: 'li', style: 'width:100%;text-align:left' + (danger ? ';color:var(--badt)' : ''), onclick: async () => { s.close(); await fn(); } }, icon(ic), h('span', { class: 'grow', text }));
    const list = h('div', { class: 'list' });
    if (doc || (Auth.doctor && v.view !== 'join')) list.append(item('calendar', 'Приёмы', () => App.toAppointments()));
    if (Auth.doctor && Auth.doctor.admin) list.append(item('list', 'Журнал аудита', () => App.auditSheet()));
    list.append(item('text', 'Крупный шрифт', () => Ui.big()));
    if (inRoom && doc) {
      list.append(item('user', 'Освободить место пациента', async () => { try { await Api.ev('control', { action: 'reset_patient' }); Toast.show('Пациент сможет войти заново по ссылке'); } catch (e) { Toast.err(e); } }));
      list.append(item('alert', 'Экстренное завершение', () => App.emergency(), true));
    }
    if (Auth.doctor && Cfg.auth) list.append(item('logout', 'Выйти', () => Auth.logout()));
    s.el.append(list);
  }
};

/* ===== Экран звонка ===== */
const CallUi = {
  r: {}, t0: 0, timerFn: null, panes: {}, tabBtn: {}, curTab: 'chat', mq: null,
  build() {
    const isDoc = State.v.role === 'doctor', R = this.r = {};
    this.panes = {}; this.tabBtn = {}; this.t0 = 0;
    R.remote = h('video', { autoplay: true, playsinline: true, 'aria-label': 'Видео собеседника' });
    R.ovR = h('canvas', { class: 'ov' });
    R.stage = h('div', { class: 'vstage' }, R.remote, R.ovR);
    R.local = h('video', { autoplay: true, playsinline: true, class: 'mirror', 'aria-label': 'Ваше видео' }); R.local.muted = true;
    R.ovL = h('canvas', { class: 'ov mirror' });
    R.self = h('div', { class: 'selfview', 'aria-label': 'Самовидение, можно перетаскивать' }, R.local, R.ovL);
    R.qdot = h('span', { class: 'dot' }); R.qtxt = h('span', { class: 'sm', text: '' }); R.timer = h('span', { class: 'mono sm', text: '00:00' });
    const peer = isDoc ? (App.proto.appointment && App.proto.appointment.patient_name) || 'Пациент' : (App.proto.appointment && App.proto.appointment.doctor) || 'Врач';
    R.vtop = h('div', { class: 'vtop' }, h('span', { class: 'grow', text: peer }), R.qdot, R.qtxt, R.timer);
    R.vmsg = h('div', { class: 'vmsg' });
    R.hint = h('div', { class: 'hint', role: 'status', 'aria-live': 'polite' });
    R.pq = h('div', { class: 'pqual hidden' });
    const cb = (name, label, fn, pressed) => h('button', { class: 'cb', 'aria-label': label, title: label, 'aria-pressed': pressed ? 'true' : 'false', onclick: fn }, icon(name));
    R.bMic = cb('mic', 'Микрофон (M)', () => this.toggleMic());
    R.bCam = cb('video', 'Камера (V)', () => this.toggleCam());
    R.bScr = cb('screen', 'Демонстрация экрана', async e => { if (Media.sharing) await Media.stopShare(); else await Media.shareScreen(); });
    R.bMesh = cb('mesh', isDoc ? 'Face Mesh: сетка (F)' : 'Задания для лица', () => { if (isDoc) FaceMesh.toggle(); else FaceMesh.openTasks(); });
    R.bMore = cb('more', 'Ещё', () => this.moreSheet());
    R.bEnd = cb('end', isDoc ? 'Завершить приём' : 'Выйти', () => this.endClick()); R.bEnd.classList.add('end');
    const bar = [R.bMic, R.bCam, R.bScr, R.bMesh];
    if (!isDoc) { R.bSide = cb('chat', 'Чат и файлы', () => { R.layout.classList.toggle('noside'); R.bSide.setAttribute('aria-pressed', String(!R.layout.classList.contains('noside'))); if (window.matchMedia('(max-width:700px)').matches) this.showTab('chat'); }); bar.push(R.bSide); }
    bar.push(R.bMore, R.bEnd);
    R.bar = h('div', { class: 'callbar', role: 'toolbar', 'aria-label': 'Управление звонком' }, bar);
    R.wrap = h('div', { class: 'callwrap' }, R.stage, R.self, R.vtop, R.vmsg, R.pq, R.hint, R.bar);
    R.center = h('div', { class: 'center' }, R.wrap);
    /* панели */
    const tabs = isDoc ? [['card', 'Карточка', 'user'], ['chat', 'Чат', 'chat'], ['files', 'Файлы', 'file'], ['concl', 'Заключение', 'doc'], ['face', 'Лицо', 'face']]
      : [['chat', 'Чат', 'chat'], ['files', 'Файлы', 'file'], ['face', 'Задания', 'face']];
    const content = {
      chat: Chat.build(), files: Files.build(),
      face: isDoc ? FaceMesh.doctorPane() : FaceMesh.patientPane()
    };
    if (isDoc) { R.cardBody = Record.cardPane(); content.card = R.cardBody; content.concl = Record.conclPane(); }
    for (const [k] of tabs) this.panes[k] = h('section', { class: 'pane', id: 'pane-' + k, role: 'tabpanel' }, k === 'card' ? null : content[k]);
    if (isDoc) this.panes.card.append(R.cardBody);
    R.tabs = h('div', { class: 'tabs', role: 'tablist' }, tabs.map(([k, label]) => { const b = h('button', { role: 'tab', 'aria-selected': 'false', onclick: () => this.showTab(k) }, label); this.tabBtn[k] = b; return b; }));
    R.rside = h('aside', { class: 'rside' }, R.tabs, tabs.map(([k]) => this.panes[k]));
    R.pbar = h('nav', { class: 'pbar', 'aria-label': 'Разделы' }, [h('button', { 'aria-selected': 'true', 'data-t': 'video', onclick: () => this.showTab('video') }, icon('video'), 'Видео')].concat(tabs.map(([k, label, ic]) => h('button', { 'aria-selected': 'false', 'data-t': k, onclick: () => this.showTab(k) }, icon(ic), label))));
    R.lside = isDoc ? h('aside', { class: 'lside' }) : null;
    R.layout = h('div', { class: 'dlayout' + (isDoc ? '' : ' pat noside'), 'data-ptab': 'video' }, R.lside, R.center, R.rside, R.pbar);
    $('#main').replaceChildren(R.layout);
    if (isDoc) {
      this.mq = window.matchMedia('(min-width:1100px)');
      this.mq.onchange = () => this.place(); this.place();
    }
    this.showTab(isDoc && !this.mq.matches ? 'card' : 'chat');
    R.layout.dataset.ptab = 'video'; this.markPbar('video');
    if (Media.stream) this.setLocal(Media.stream);
    if (Rtc.remote) this.setRemote(Rtc.remote);
    this.makeDraggable(R.self, R.wrap);
    this.shareChanged(false); this.syncBtns();
    FaceMesh.mount({ role: State.v.role, remote: R.remote, local: R.local, ovR: R.ovR, ovL: R.ovL, stage: R.stage, wrap: R.wrap, hint: R.hint, pq: R.pq });
    if (isDoc) { Files.history(); }
    if (this.timerFn) Loop.del(this.timerFn);
    let last = '';
    this.timerFn = Loop.add(() => {
      if (!this.t0) return;
      const s = Math.floor((Date.now() - this.t0) / 1000), txt = pad2(Math.floor(s / 60)) + ':' + pad2(s % 60);
      if (txt !== last) { last = txt; R.timer.textContent = txt; }
    });
    this.refresh();
  },
  place() {
    const R = this.r; if (!R.lside) return;
    if (this.mq.matches) { R.lside.append(R.cardBody); this.tabBtn.card.classList.add('hidden'); R.pbar.querySelector('[data-t=card]').classList.remove('hidden'); if (this.curTab === 'card') this.showTab('chat'); }
    else { this.panes.card.append(R.cardBody); this.tabBtn.card.classList.remove('hidden'); }
  },
  showTab(name) {
    const R = this.r;
    if (name === 'video') { R.layout.dataset.ptab = 'video'; this.markPbar('video'); return; }
    if (!this.panes[name]) return;
    this.curTab = name;
    for (const k in this.panes) { this.panes[k].classList.toggle('on', k === name); this.tabBtn[k].setAttribute('aria-selected', String(k === name)); }
    R.layout.dataset.ptab = name; this.markPbar(name);
    if (R.layout.classList.contains('pat')) { if (window.matchMedia('(max-width:700px)').matches === false) { R.layout.classList.remove('noside'); if (R.bSide) R.bSide.setAttribute('aria-pressed', 'true'); } }
    if (name === 'chat') Chat.focusEnd();
  },
  markPbar(n) { for (const b of this.r.pbar.querySelectorAll('button')) b.setAttribute('aria-selected', String(b.dataset.t === n)); },
  setRemote(stream) {
    const R = this.r; if (!R.remote) return;
    R.remote.srcObject = stream; const p = R.remote.play(); if (p && p.catch) p.catch(() => { });
    this.refresh();
  },
  setLocal(stream) { const R = this.r; if (!R.local) return; R.local.srcObject = stream; const p = R.local.play(); if (p && p.catch) p.catch(() => { }); this.syncBtns(); },
  syncBtns() {
    const R = this.r; if (!R.bMic) return;
    R.bMic.replaceChildren(icon(Media.micOn ? 'mic' : 'micoff')); R.bMic.setAttribute('aria-pressed', String(!Media.micOn));
    R.bCam.replaceChildren(icon(Media.camOn ? 'video' : 'videooff')); R.bCam.setAttribute('aria-pressed', String(!Media.camOn));
  },
  toggleMic() { Media.toggleMic(); this.syncBtns(); },
  toggleCam() { Media.toggleCam(); this.syncBtns(); },
  shareChanged(on) { if (this.r.bScr) this.r.bScr.setAttribute('aria-pressed', String(!!on)); },
  callStarted() { if (!this.t0) { this.t0 = Date.now(); } Quality.start(); this.refresh(); },
  setStatus(s) { State.set({ status: s }); },
  refresh() {
    const R = this.r; if (!R.vmsg) return;
    const v = State.v, isDoc = v.role === 'doctor';
    let title = '', sub = '', btn = false, show = true;
    if (v.status === 'completed') { title = 'Консультация завершена'; sub = isDoc ? 'Заполните заключение и отправьте его пациенту.' : 'Спасибо. Документы от врача появятся в чате и в разделе «Файлы».'; }
    else if (v.status === 'in_progress') {
      if (v.rtc === 'connected') show = false;
      else { title = v.rtc === 'reconnecting' ? 'Восстанавливаю связь' : 'Подключение'; sub = isDoc ? (v.peerHere ? 'Устанавливаю соединение с пациентом' : 'Ждём подключения пациента') : 'Ожидаем подключения врача'; }
    } else if (isDoc) { title = v.peerHere ? 'Пациент ожидает' : 'Ожидаем пациента'; sub = v.peerHere ? 'Укажите адрес и телефон пациента и начните приём' : 'Пациент ещё не вошёл в комнату'; btn = true; }
    else { title = 'Врач скоро подключится'; sub = 'Оставайтесь на этой странице'; }
    R.vmsg.classList.toggle('hidden', !show);
    if (show) R.vmsg.replaceChildren(h('h2', { text: title }), h('p', { style: 'margin:0;opacity:.8', text: sub }),
      btn ? h('button', { class: 'btn pri lg', onclick: () => this.start() }, 'Начать приём') : null);
    const q = v.quality, L = { good: ['ok', 'Связь: хорошая'], mid: ['mid', 'Связь: средняя'], bad: ['bad', 'Связь: плохая'] }[q ? q.level : ''];
    R.qdot.className = 'dot ' + (L ? L[0] : ''); R.qtxt.textContent = L ? L[1] : (v.rtc === 'reconnecting' ? 'Переподключение' : '');
  },
  async start() {
    if (!Record.locOk()) { Toast.show('Укажите адрес и телефон пациента'); this.showTab('card'); Record.focusLoc(); return; }
    try {
      await Record.saveLocation();
      await Api.ev('control', { action: 'start' });
      State.set({ status: 'in_progress' });
      await Rtc.startAsDoctor();
    } catch (e) { Toast.err(e); }
  },
  peerLeft() { Toast.show('Собеседник отключился', null, 5000); State.set({ peerHere: false }); this.refresh(); },
  async endClick() {
    if (State.v.role === 'doctor') {
      if (State.v.status === 'completed') return;
      if (!(await Ui.confirm('Завершить приём?', 'Видеосвязь будет закрыта. Затем можно заполнить заключение.', 'Завершить', true))) return;
      try { await Api.ev('control', { action: 'end' }); } catch (e) { Toast.err(e); return; }
      Events.ended(); this.showTab('concl');
      Ui.sheet({ title: 'Приём завершён', actions: [{ label: 'Позже', kind: 'sec' }, { label: 'Заполнить заключение', kind: 'pri', onClick: () => { this.showTab('concl'); } }] }).el.append(h('p', { style: 'margin:0', text: 'Заполните заключение консультации и отправьте его пациенту.' }));
    } else {
      if (!(await Ui.confirm('Покинуть консультацию?', 'Вы сможете вернуться по той же ссылке, пока врач не завершит приём.', 'Выйти', true))) return;
      try { await Api.ev('presence', { state: 'left' }); } catch (e) { /* ignore */ }
      App.teardown();
      $('#main').replaceChildren(h('div', { class: 'center-col' }, h('h1', { text: 'Вы вышли из консультации' }), h('p', { class: 'mut', text: 'Чтобы вернуться, откройте ссылку-приглашение ещё раз.' }),
        h('button', { class: 'btn pri lg', onclick: () => location.reload() }, 'Вернуться')));
    }
  },
  async moreSheet() {
    const s = Ui.sheet({ title: 'Ещё' });
    const d = await Media.devices();
    const mk = (label, list, kind) => {
      const sel = h('select', { class: 'inp' }); list.forEach((x, i) => sel.append(h('option', { value: x.deviceId, text: x.label || (label + ' ' + (i + 1)) })));
      const cur = kind === 'v' ? Media.cam : (Media.stream && Media.stream.getAudioTracks()[0]); const id = cur && cur.getSettings().deviceId; if (id) sel.value = id;
      sel.onchange = async () => { try { await Media.switchDevice(kind, sel.value); } catch (e) { Toast.show(Media.explain(e), 'err', 6000); } };
      return Ui.field(label, sel);
    };
    s.el.append(mk('Камера', d.v, 'v'), mk('Микрофон', d.a, 'a'));
    const ao = h('button', { class: 'btn sec', onclick: () => { Media.setAudioOnly(Media.camOn); this.syncBtns(); ao.textContent = Media.camOn ? 'Включить режим «Только аудио»' : 'Выключить режим «Только аудио»'; } }, icon('audio'), Media.camOn ? 'Включить режим «Только аудио»' : 'Выключить режим «Только аудио»');
    const fs = h('button', { class: 'btn sec', onclick: () => { s.close(); this.fullscreen(); } }, icon('full'), 'Полный экран');
    const big = h('button', { class: 'btn sec', onclick: () => Ui.big() }, icon('text'), 'Крупный шрифт');
    s.el.append(h('div', { class: 'stack' }, ao, fs, big));
  },
  fullscreen() {
    const el = this.r.wrap; if (!el) return;
    if (document.fullscreenElement) { document.exitFullscreen(); return; }
    const f = el.requestFullscreen || el.webkitRequestFullscreen; if (f) { const p = f.call(el); if (p && p.catch) p.catch(() => Toast.show('Полный экран недоступен')); } else Toast.show('Полный экран недоступен в этом браузере');
  },
  makeDraggable(el, box) {
    let sx = 0, sy = 0, ox = 0, oy = 0, drag = false;
    el.addEventListener('pointerdown', e => { drag = true; const b = box.getBoundingClientRect(), r = el.getBoundingClientRect(); ox = r.left - b.left; oy = r.top - b.top; sx = e.clientX; sy = e.clientY; el.setPointerCapture(e.pointerId); el.style.right = 'auto'; el.style.left = ox + 'px'; el.style.top = oy + 'px'; el.style.cursor = 'grabbing'; });
    el.addEventListener('pointermove', e => { if (!drag) return; const b = box.getBoundingClientRect(); el.style.left = clamp(ox + e.clientX - sx, 0, b.width - el.offsetWidth) + 'px'; el.style.top = clamp(oy + e.clientY - sy, 0, b.height - el.offsetHeight) + 'px'; });
    const up = () => { drag = false; el.style.cursor = 'grab'; };
    el.addEventListener('pointerup', up); el.addEventListener('pointercancel', up);
  }
};
State.sub((v, p) => { if ('rtc' in p || 'status' in p || 'quality' in p || 'peerHere' in p) CallUi.refresh(); if ('net' in p) Header.update(); });

/* ===== События ===== */
const Events = {
  handle(ev) {
    const t = ev.type, d = ev.data || {};
    if (t === 'kill') return App.killed();
    if (t === 'offer' || t === 'answer' || t === 'ice') return Rtc.handle(ev);
    if (t === 'chat') return Chat.add(ev);
    if (t === 'file') return Files.add(ev);
    if (t === 'document') return Chat.addDoc(ev);
    if (t === 'presence') return Events.presence(d);
    if (t === 'control') return Events.control(d);
    if (t === 'face') return FaceMesh.onFace(d);
    if (t === 'measure') return FaceMesh.onMeasure(d);
  },
  presence(d) {
    const doc = State.v.role === 'doctor';
    if (d.state === 'waiting') { const was = State.v.peerHere; if (doc) this.patientWaiting(was); else State.set({ peerHere: true }); }
    else if (d.state === 'online') { State.set({ peerHere: true }); if (doc && State.v.status === 'in_progress' && !(Rtc.pc && State.v.rtc === 'connected')) { Rtc.hardRestart(); Toast.show('Пациент подключился заново'); } }
    else if (d.state === 'left') { CallUi.peerLeft(); }
  },
  patientWaiting(silent) {
    const first = !State.v.peerHere;
    State.set({ peerHere: true, status: State.v.status === 'in_progress' ? 'in_progress' : 'waiting' });
    if (!silent && first) { Ui.sound(); Toast.show('Пациент ожидает в комнате', null, 6000); }
  },
  control(d) {
    const doc = State.v.role === 'doctor';
    switch (d.action) {
      case 'start': if (!doc) { State.set({ status: 'in_progress' }); } break;
      case 'end': this.ended(); break;
      case 'rtc_reset': if (!doc) Rtc.close(true); break;
      case 'restart_ice': if (doc) { Rtc.offer(true); } break;
      case 'reset_patient': break;
      case 'face_consent': if (doc) FaceMesh.setConsent(!!d.granted); break;
      default: if (!doc) FaceMesh.patientRequest(d); break;
    }
  },
  ended() {
    Rtc.close(); Quality.stop(); FaceMesh.stopAll(); Media.stop(); clearInterval(Session.hb);
    State.set({ status: 'completed', rtc: 'closed' });
    if (CallUi.r.remote) { CallUi.r.remote.srcObject = null; CallUi.r.local.srcObject = null; }
    CallUi.refresh();
  }
};

/* ===== Чат ===== */
const Chat = {
  seen: new Set(), list: null, input: null,
  build() {
    this.seen = new Set();
    this.list = h('div', { class: 'msgs', role: 'log', 'aria-live': 'polite', 'aria-label': 'Сообщения' });
    this.input = h('textarea', { class: 'inp', rows: 1, placeholder: 'Сообщение', 'aria-label': 'Текст сообщения', style: 'min-height:44px;height:44px;resize:none' });
    this.input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.send(); } });
    return h('div', { class: 'stack', style: 'flex:1;min-height:0' }, this.list,
      h('div', { class: 'sheetrow' }, this.input, h('button', { class: 'ibtn', style: 'background:var(--acc);color:#fff', 'aria-label': 'Отправить', onclick: () => this.send() }, icon('send'))));
  },
  async send() {
    const text = this.input.value.trim(); if (!text) return;
    this.input.value = '';
    try { await Api.ev('chat', { text }); } catch (e) { this.input.value = text; Toast.err(e); }
  },
  push(ev, body) {
    if (!this.list) return;
    const mine = ev.from === State.v.role;
    const m = h('div', { class: 'msg' + (mine ? ' me' : '') }, body, h('span', { class: 'tm', text: fmtTime(ev.ts) }));
    this.list.append(m); this.focusEnd();
    if (!mine && CallUi.curTab !== 'chat' && State.v.view === 'call') Toast.show('Новое сообщение');
    return m;
  },
  focusEnd() { if (this.list) this.list.scrollTop = this.list.scrollHeight; },
  add(ev) {
    const d = ev.data || {}; const k = 'c' + d.mid; if (this.seen.has(k)) return; this.seen.add(k);
    this.push(ev, h('span', { text: d.text || '' }));
  },
  addFile(ev, node) { this.push(ev, node); },
  addDoc(ev) {
    const d = ev.data || {}; const k = 'd' + d.mid; if (this.seen.has(k)) return; this.seen.add(k);
    this.push(ev, h('div', { class: 'stack', style: 'gap:6px' }, h('strong', { text: 'Заключение консультации' }), h('span', { class: 'sm', text: d.doctor ? 'Врач: ' + d.doctor : '' }),
      h('div', { class: 'row wrap' }, h('button', { class: 'btn sec', onclick: () => Print.conclusion(d) }, icon('doc'), 'Открыть и распечатать'),
        h('button', { class: 'btn sec', onclick: () => Ui.download('zaklyuchenie.json', JSON.stringify(d, null, 2)) }, icon('download'), 'JSON'))));
    Files.addDocRow(ev);
  }
};

/* ===== Файлы ===== */
const EXT_OK = ['.pdf', '.jpg', '.jpeg', '.png', '.csv', '.xlsx', '.xls'];
const Files = {
  box: null, seen: new Set(), hist: null,
  build() {
    this.seen = new Set();
    const inp = h('input', { type: 'file', class: 'hidden', accept: EXT_OK.join(','), onchange: () => { for (const f of inp.files) this.upload(f); inp.value = ''; } });
    this.box = h('div', { class: 'list' });
    const drop = h('div', { class: 'stack', style: 'flex:1;min-height:0;overflow:auto' },
      h('button', { class: 'btn sec', onclick: () => inp.click() }, icon('upload'), 'Прикрепить файл'),
      h('p', { class: 'note', style: 'margin:0', text: 'PDF, JPEG, PNG, CSV, XLSX, XLS, до 20 МБ.' }), inp, this.box);
    drop.addEventListener('dragover', e => e.preventDefault());
    drop.addEventListener('drop', e => { e.preventDefault(); for (const f of e.dataTransfer.files) this.upload(f); });
    return drop;
  },
  history() { /* история файлов и снимков для левой панели строится в Record.cardPane */ },
  upload(file) {
    const ext = (file.name.match(/\.[^.]+$/) || [''])[0].toLowerCase();
    if (!EXT_OK.includes(ext)) { Toast.err('Недопустимый тип файла. Разрешены: PDF, JPEG, PNG, CSV, XLSX, XLS'); return; }
    if (file.size > 20 * 1024 * 1024) { Toast.err('Файл больше 20 МБ'); return; }
    if (file.size === 0) { Toast.err('Файл пустой'); return; }
    const prog = h('progress', { max: 100, value: 0, style: 'width:100%' });
    const row = h('div', { class: 'li' }, icon('upload'), h('div', { class: 'grow' }, h('div', { class: 't', text: file.name }), prog));
    this.box.prepend(row);
    const fr = new FileReader();
    fr.onerror = () => { row.remove(); Toast.err('Не удалось прочитать файл'); };
    fr.onload = () => {
      const b64 = String(fr.result).split(',')[1] || '';
      const x = new XMLHttpRequest();
      x.open('POST', '/rooms/' + Api.room + '/events');
      x.setRequestHeader('Content-Type', 'application/json'); x.setRequestHeader('Authorization', 'Bearer ' + Api.token);
      x.upload.onprogress = e => { if (e.lengthComputable) prog.value = e.loaded / e.total * 100; };
      x.onerror = () => { row.remove(); Toast.err('Нет связи при отправке файла'); };
      x.onload = () => {
        row.remove();
        if (x.status !== 202) { let m = 'Файл не принят'; try { m = JSON.parse(x.responseText).message || m; } catch (e) { /* ignore */ } Toast.err(m); }
      };
      x.send(JSON.stringify({ type: 'file', data: { name: file.name, b64 } }));
    };
    fr.readAsDataURL(file);
  },
  sizeText(n) { return n > 1048576 ? (n / 1048576).toFixed(1) + ' МБ' : Math.max(1, Math.round(n / 1024)) + ' КБ'; },
  async download(m) {
    try {
      const b = await Api.req('GET', '/rooms/' + Api.room + '/files/' + m.id, { blob: true });
      const a = h('a', { href: URL.createObjectURL(b), download: m.name }); document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 800);
    } catch (e) { Toast.err(e); }
  },
  add(ev) {
    const m = ev.data || {}; if (this.seen.has('f' + m.id)) return; this.seen.add('f' + m.id);
    const dl = h('button', { class: 'ibtn', 'aria-label': 'Скачать ' + m.name, onclick: () => this.download(m) }, icon('download'));
    if (this.box) this.box.prepend(h('div', { class: 'li' }, icon('file'), h('div', { class: 'grow' }, h('div', { class: 't', text: m.name }), h('div', { class: 'sm mut', text: this.sizeText(m.size) + ', ' + fmtTime(ev.ts) })), dl));
    const chatNode = h('div', { class: 'stack', style: 'gap:4px' }, h('div', { class: 'row' }, icon('file'), h('span', { text: m.name })), h('span', { class: 'sm', text: this.sizeText(m.size) }),
      h('button', { class: 'btn sec', onclick: () => this.download(m) }, icon('download'), 'Скачать'));
    if (/^image\//.test(m.mime || '')) {
      const img = h('img', { class: 'pv', alt: 'Предпросмотр: ' + m.name }); chatNode.append(img);
      Api.req('GET', '/rooms/' + Api.room + '/files/' + m.id, { blob: true }).then(b => { img.src = URL.createObjectURL(b); }).catch(() => img.remove());
    }
    Chat.addFile(ev, chatNode);
  },
  addDocRow(ev) {
    const d = ev.data || {}; if (!this.box) return;
    this.box.prepend(h('div', { class: 'li' }, icon('doc'), h('div', { class: 'grow' }, h('div', { class: 't', text: 'Заключение консультации' }), h('div', { class: 'sm mut', text: fmtTime(ev.ts) })),
      h('button', { class: 'ibtn', 'aria-label': 'Открыть заключение', onclick: () => Print.conclusion(d) }, icon('doc'))));
  }
};

/* ===== Печатные формы ===== */
const Print = {
  go(node) {
    const pa = $('#printArea'); pa.replaceChildren(node);
    const after = () => { pa.replaceChildren(); window.removeEventListener('afterprint', after); };
    window.addEventListener('afterprint', after);
    setTimeout(() => window.print(), 80);
  },
  async conclusion(d) {
    // Сначала сервис reports; если он недоступен, печатаем формой из браузера
    try {
      const r = await Api.post('/rooms/' + Api.room + '/render-conclusion', d);
      if (r && r.html) { this.frame(r.html); return; }
    } catch (e) { /* запасной путь ниже */ }
    this.conclusionLocal(d);
  },
  frame(html) {
    const f = h('iframe', { style: 'position:fixed;width:0;height:0;border:0', 'aria-hidden': 'true', srcdoc: html });
    f.onload = () => { try { f.contentWindow.focus(); f.contentWindow.print(); } catch (e) { /* ignore */ } setTimeout(() => f.remove(), 60000); };
    document.body.append(f);
  },
  conclusionLocal(d) {
    const c = d.conclusion || {}, row = (t, v) => [h('h2', { text: t }), h('p', { style: 'white-space:pre-wrap', text: v || '-' })];
    this.go(h('div', null, h('h1', { text: d.center || Cfg.center }), h('h2', { text: 'Заключение консультации' }),
      h('table', null, h('tr', null, h('th', { text: 'Пациент' }), h('td', { text: d.patient || '' })), h('tr', null, h('th', { text: 'Дата' }), h('td', { text: d.date || '' })), h('tr', null, h('th', { text: 'Врач' }), h('td', { text: d.doctor || '' }))),
      row('Жалобы', c.complaints), row('Анамнез', c.anamnesis), row('Диагноз', (c.diagnosis || '') + (c.icd ? ' (МКБ-10: ' + c.icd + ')' : '')), row('Рекомендации', c.recommendations),
      row('Повторный контакт', c.followup),
      h('p', { class: 'sm', text: 'Документ сформирован по результатам дистанционной консультации. Это заключение консультации, а не электронный рецепт.' }),
      h('div', { class: 'sign', text: 'Подпись врача ______________________ / ' + (d.doctor || '') + ' /' })));
  }
};

/* ===== Карточка, заметки, заключение (врач) ===== */
const ICD = [
  ['A09', 'Инфекционный гастроэнтерит и колит'], ['B34.9', 'Вирусная инфекция неуточнённая'], ['E03.9', 'Гипотиреоз неуточнённый'], ['E11.9', 'Сахарный диабет 2 типа без осложнений'],
  ['E66.9', 'Ожирение неуточнённое'], ['E78.5', 'Гиперлипидемия неуточнённая'], ['F32.9', 'Депрессивный эпизод неуточнённый'], ['F41.2', 'Смешанное тревожное и депрессивное расстройство'],
  ['G43.9', 'Мигрень неуточнённая'], ['G47.0', 'Нарушения засыпания и поддержания сна'], ['I10', 'Эссенциальная гипертензия'], ['I20.9', 'Стенокардия неуточнённая'],
  ['I25.1', 'Атеросклеротическая болезнь сердца'], ['I50.9', 'Сердечная недостаточность неуточнённая'], ['I83.9', 'Варикозное расширение вен нижних конечностей'],
  ['J00', 'Острый назофарингит'], ['J02.9', 'Острый фарингит неуточнённый'], ['J03.9', 'Острый тонзиллит неуточнённый'], ['J06.9', 'Острая инфекция верхних дыхательных путей'],
  ['J18.9', 'Пневмония неуточнённая'], ['J20.9', 'Острый бронхит неуточнённый'], ['J30.4', 'Аллергический ринит неуточнённый'], ['J45.9', 'Астма неуточнённая'],
  ['K21.9', 'Гастроэзофагеальный рефлюкс без эзофагита'], ['K29.7', 'Гастрит неуточнённый'], ['K58.9', 'Синдром раздражённого кишечника'], ['K80.2', 'Камни жёлчного пузыря без холецистита'],
  ['L20.9', 'Атопический дерматит неуточнённый'], ['L70.0', 'Угри обыкновенные'], ['L81.9', 'Нарушение пигментации неуточнённое'], ['L90.5', 'Рубцовые состояния и фиброз кожи'],
  ['M25.5', 'Боль в суставе'], ['M54.2', 'Боль в области шеи'], ['M54.5', 'Боль внизу спины'], ['N39.0', 'Инфекция мочевыводящих путей'], ['N95.1', 'Менопауза'],
  ['R50.9', 'Лихорадка неуточнённая'], ['R51', 'Головная боль'], ['Z09', 'Последующее наблюдение после лечения'], ['Z71.9', 'Консультирование неуточнённое']
];
const ICD_RE = /^[A-Z]\d{2}(\.\d{1,2})?$/;
const Record = {
  f: {}, loc: {}, notes: null, c: {}, timers: {}, lastConcl: null,
  deb(k, fn, ms) { clearTimeout(this.timers[k]); this.timers[k] = setTimeout(fn, ms || 800); },
  cardPane() {
    const mk = (key, label, el) => { this.f[key] = el; el.addEventListener('input', () => this.deb('card', () => this.saveCard())); return Ui.field(label, el); };
    const sel = (opts) => { const s = h('select', { class: 'inp' }); for (const o of opts) s.append(h('option', { value: o, text: o || 'Не указано' })); return s; };
    this.loc = { address: Ui.input('text', 'Город, улица, дом, квартира'), phone: Ui.input('tel', '+7 ...') };
    for (const k in this.loc) this.loc[k].addEventListener('input', () => this.deb('loc', () => this.saveLocation().catch(() => { })));
    this.notes = h('textarea', { class: 'inp', placeholder: 'Видны только врачу', rows: 4 });
    this.notes.addEventListener('input', () => this.deb('notes', () => Api.post('/rooms/' + Api.room + '/protocol', { notes: this.notes.value }).catch(Toast.err)));
    const hist = Files.hist = h('div', { class: 'stack' });
    return h('div', { class: 'stack' },
      h('div', { class: 'rowb' }, h('h3', { text: 'Пациент' }), h('span', { class: 'chip', text: 'со слов пациента' })),
      mk('full_name', 'ФИО', Ui.input('text')), mk('birth', 'Дата рождения', Ui.input('date')),
      mk('sex', 'Пол', sel(['', 'Женский', 'Мужской'])), mk('blood', 'Группа крови', sel(['', 'O(I) Rh+', 'O(I) Rh-', 'A(II) Rh+', 'A(II) Rh-', 'B(III) Rh+', 'B(III) Rh-', 'AB(IV) Rh+', 'AB(IV) Rh-'])),
      mk('allergies', 'Аллергии', h('textarea', { class: 'inp', rows: 2 })), mk('chronic', 'Хронические заболевания', h('textarea', { class: 'inp', rows: 2 })),
      mk('meds', 'Принимаемые препараты', h('textarea', { class: 'inp', rows: 2 })), mk('complaints', 'Жалобы', h('textarea', { class: 'inp', rows: 3 })),
      h('h3', { text: 'Местонахождение пациента' }),
      Ui.field('Адрес (обязательно)', this.loc.address), Ui.field('Телефон (обязательно)', this.loc.phone),
      h('div', { class: 'banner warn', style: 'border-radius:10px', text: 'Экстренные службы: 103 / 112' }),
      h('h3', { text: 'Приватные заметки' }), this.notes,
      h('p', { class: 'note', style: 'margin:0', text: 'Заметки и карточка пациенту не передаются.' }), hist);
  },
  load(p) {
    const c = p.card || {};
    for (const k in this.f) this.f[k].value = c[k] || '';
    this.loc.address.value = (p.location && p.location.address) || ''; this.loc.phone.value = (p.location && p.location.phone) || '';
    this.notes.value = p.notes || '';
    this.fillConcl(p.conclusion || {});
    this.lastConcl = p.conclusion || null;
    FaceMesh.loadProto(p);
  },
  locOk() { return this.loc.address.value.trim().length > 2 && this.loc.phone.value.trim().length > 4; },
  focusLoc() { (this.loc.address.value.trim() ? this.loc.phone : this.loc.address).focus(); },
  saveLocation() { return Api.post('/rooms/' + Api.room + '/protocol', { location: { address: this.loc.address.value.trim(), phone: this.loc.phone.value.trim() } }); },
  collectCard() { const o = {}; for (const k in this.f) o[k] = this.f[k].value; return o; },
  async saveCard() { try { await Api.ev('ehr', { card: this.collectCard() }); } catch (e) { Toast.err(e); } },
  conclPane() {
    const mk = (k, label, el) => { this.c[k] = el; return Ui.field(label, el); };
    const icd = Ui.input('text', 'Например J06.9'); icd.setAttribute('list', 'icdlist'); icd.maxLength = 6; icd.style.textTransform = 'uppercase';
    const dl = h('datalist', { id: 'icdlist' }, ICD.map(([c, n]) => h('option', { value: c, label: n })));
    const hintEl = h('div', { class: 'note', 'aria-live': 'polite', text: '' });
    icd.addEventListener('input', () => {
      const v = icd.value.toUpperCase().trim(); const f = ICD.find(x => x[0] === v);
      hintEl.textContent = !v ? '' : f ? f[1] : (ICD_RE.test(v) ? 'Код корректного формата' : 'Формат кода: A00 или A00.0');
      icd.setAttribute('aria-invalid', String(!!v && !ICD_RE.test(v)));
    });
    this.c.icd = icd;
    return h('div', { class: 'stack' }, h('h3', { text: 'Заключение консультации' }),
      mk('complaints', 'Жалобы', h('textarea', { class: 'inp', rows: 3 })), mk('anamnesis', 'Анамнез', h('textarea', { class: 'inp', rows: 3 })),
      mk('diagnosis', 'Диагноз', h('textarea', { class: 'inp', rows: 2 })), Ui.field('Код МКБ-10', icd), dl, hintEl,
      mk('recommendations', 'Рекомендации', h('textarea', { class: 'inp', rows: 4 })), mk('followup', 'Дата повторного контакта', Ui.input('date')),
      h('div', { class: 'row wrap' }, h('button', { class: 'btn pri', onclick: () => this.saveConcl() }, 'Сохранить'),
        h('button', { class: 'btn sec', onclick: () => this.printConcl() }, icon('doc'), 'Печать'),
        h('button', { class: 'btn sec', onclick: () => this.sendConcl() }, icon('send'), 'Отправить пациенту'),
        h('button', { class: 'btn sec', onclick: () => this.exportJson() }, icon('download'), 'JSON для МИС')),
      h('p', { class: 'note', style: 'margin:0', text: 'Это заключение консультации, а не электронный рецепт.' }));
  },
  fillConcl(c) { for (const k of ['complaints', 'anamnesis', 'diagnosis', 'icd', 'recommendations', 'followup']) if (this.c[k]) this.c[k].value = c[k] || ''; },
  collectConcl() { const o = {}; for (const k of ['complaints', 'anamnesis', 'diagnosis', 'icd', 'recommendations', 'followup']) o[k] = (this.c[k].value || '').trim(); o.icd = o.icd.toUpperCase(); return o; },
  async saveConcl() {
    const c = this.collectConcl();
    if (c.icd && !ICD_RE.test(c.icd)) { Toast.err('Код МКБ-10 должен иметь вид A00 или A00.0'); this.c.icd.focus(); return false; }
    try { await Api.post('/rooms/' + Api.room + '/protocol', { conclusion: c }); this.lastConcl = c; Toast.show('Заключение сохранено'); return true; }
    catch (e) { Toast.err(e); return false; }
  },
  payload() {
    const a = App.proto.appointment || {};
    return { center: Cfg.center, doctor: (Auth.doctor && Auth.doctor.name) || a.doctor || '', patient: this.f.full_name.value || a.patient_name || '', date: new Date().toISOString().slice(0, 10), conclusion: this.collectConcl() };
  },
  printConcl() { Print.conclusion(this.payload()); },
  async sendConcl() {
    if (!(await this.saveConcl())) return;
    try { await Api.ev('document', {}); Toast.show('Заключение отправлено пациенту'); } catch (e) { Toast.err(e); }
  },
  exportJson() {
    const p = this.payload(); const a = App.proto.appointment || {};
    Ui.download('konsultaciya-' + (a.code || 'mars') + '.json', JSON.stringify({ schema: 'mars-consultation/1', center: p.center, appointment: { id: a.id, code: a.code, scheduled: a.scheduled }, doctor: p.doctor, patient: this.collectCard(), location: { address: this.loc.address.value, phone: this.loc.phone.value }, conclusion: p.conclusion }, null, 2));
  }
};

/* ===== Приложение ===== */
const App = {
  proto: {}, dead: false, banner: null,
  shell() {
    Header.el = h('header', { class: 'top' });
    this.banner = h('div', { class: 'banner bad hidden', role: 'alert', text: 'Нет связи с сервером. Пробую восстановить подключение.' });
    $('#app').replaceChildren(Header.el, this.banner, h('main', { id: 'main' }));
    Header.update(); Services.start();
  },
  async init() {
    try { if (localStorage.getItem('mars_big') === '1') document.documentElement.classList.add('big'); } catch (e) { /* ignore */ }
    this.shell();
    window.addEventListener('online', () => this.setOffline(false));
    window.addEventListener('offline', () => this.setOffline(true));
    window.addEventListener('pagehide', () => { if (State.v.view === 'call' && !this.dead) Api.bye(); });
    window.addEventListener('unhandledrejection', e => { e.preventDefault(); console.error(e.reason); if (e.reason && e.reason.name !== 'AbortError') Toast.err(e.reason instanceof ApiErr ? e.reason.message : 'Не удалось выполнить действие'); });
    document.addEventListener('keydown', e => this.key(e));
    try {
      const hl = await Api.get('/health', { token: false });
      Cfg.mediapipe = hl.mediapipe; Cfg.secure = hl.secure; Cfg.center = hl.center; Cfg.auth = hl.auth_required;
      const doc = await Auth.init();
      Header.update();
      const code = new URLSearchParams(location.search).get('code');
      if (code) { Session.start(code); return; }
      if (doc) this.afterLogin(); else Auth.showLogin();
    } catch (e) {
      $('#main').replaceChildren(h('div', { class: 'center-col' }, h('h2', { text: 'Сервер недоступен' }), h('p', { class: 'mut', text: e.message }), h('button', { class: 'btn pri', onclick: () => location.reload() }, 'Повторить')));
    }
  },
  afterLogin() {
    Header.update();
    const m = location.hash.match(/appt=(\d+)/);
    if (m) Doctor.open(parseInt(m[1], 10)); else Appointments.show();
  },
  key(e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'Escape') { if (Ui.sheets.length) { Ui.sheets[Ui.sheets.length - 1].close(); return; } if (document.fullscreenElement) document.exitFullscreen(); return; }
    if (e.target.closest && e.target.closest('input,textarea,select,[contenteditable]')) return;
    if (State.v.view !== 'call' || this.dead) return;
    const k = e.key.toLowerCase();
    if (k === 'm' || k === 'ь') CallUi.toggleMic();
    else if (k === 'v' || k === 'м') CallUi.toggleCam();
    else if (k === 'f' || k === 'а') FaceMesh.cycleMode();
  },
  setOffline(off) { State.set({ net: !off }); this.banner.classList.toggle('hidden', !off); },
  toAppointments() {
    if (State.v.view === 'call' && State.v.role === 'doctor') { Api.ev('presence', { state: 'left' }).catch(() => { }); }
    this.teardown(); history.replaceState(null, '', location.pathname); Appointments.show();
  },
  teardown() {
    Poller.stop(); clearInterval(Session.hb); clearInterval(Appointments.timer);
    Rtc.close(); Quality.stop(); FaceMesh.stopAll(); Media.stop(); Ui.closeAudio();
    Api.room = null; Api.token = null; State.set({ view: '', rtc: 'idle', status: 'planned', peerHere: false, faceConsent: false });
    if (CallUi.timerFn) Loop.del(CallUi.timerFn);
    Header.update();
  },
  async emergency() {
    if (!(await Ui.confirm('Экстренное завершение', 'Консультация будет немедленно прервана у обоих участников, комната закроется. Продолжить?', 'Завершить немедленно', true))) return;
    try { await Api.ev('kill', {}); } catch (e) { if (!(e instanceof ApiErr && e.status === 410)) { Toast.err(e); return; } }
    this.killed();
  },
  killed() {
    if (this.dead) return; this.dead = true;
    const doc = State.v.role === 'doctor';
    try { sessionStorage.removeItem('mars_pt_' + Session.code); } catch (e) { /* ignore */ }
    Poller.stop(); clearInterval(Session.hb); clearInterval(Appointments.timer);
    Rtc.close(); Quality.stop(); FaceMesh.stopAll(); Media.stop(); Ui.closeAudio(); Ui.closeAll(); Loop.stop();
    Api.room = null; Api.token = null; Chat.list = null; Files.box = null; CallUi.r = {};
    $('#printArea').replaceChildren();
    State.set({ view: 'killed', rtc: 'closed', status: 'completed', peerHere: false });
    Header.update();
    $('#main').replaceChildren(h('div', { class: 'center-col' }, h('h1', { text: 'Консультация прервана' }),
      h('p', { class: 'mut', text: doc ? 'Комната закрыта у обоих участников. Данные на экране очищены.' : 'Врач завершил сеанс. Данные на экране очищены.' }),
      doc ? h('button', { class: 'btn pri lg', onclick: () => { this.dead = false; history.replaceState(null, '', location.pathname); Appointments.show(); } }, 'К списку приёмов') : null));
  },
  roomGone(e) {
    if (this.dead) return;
    if (e && e.status === 410) { this.killed(); return; }
    Poller.stop(); clearInterval(Session.hb); Rtc.close(); Quality.stop(); FaceMesh.stopAll(); Media.stop();
    $('#main').replaceChildren(h('div', { class: 'center-col' }, h('h2', { text: 'Сеанс недоступен' }),
      h('p', { class: 'mut', text: 'Комната не найдена или доступ к ней потерян (возможно, сервер перезапущен).' }),
      h('button', { class: 'btn pri', onclick: () => { try { sessionStorage.removeItem('mars_pt_' + Session.code); } catch (x) { /* ignore */ } location.reload(); } }, 'Начать заново')));
  },
  async auditSheet() {
    const s = Ui.sheet({ title: 'Журнал аудита', wide: true });
    s.el.append(h('div', { class: 'sk', style: 'height:80px' }));
    try {
      const r = await Api.get('/audit?limit=300');
      s.el.replaceChildren(h('div', { style: 'overflow:auto;max-height:60dvh' }, h('table', { class: 'tb' },
        h('tr', null, ['Время', 'Участник', 'Действие', 'Комната', 'Деталь'].map(t => h('th', { text: t }))),
        r.items.map(i => h('tr', null, h('td', { text: fmtDT(i.ts) }), h('td', { text: i.actor }), h('td', { text: i.action }), h('td', { class: 'mono', text: i.room }), h('td', { text: i.detail }))))));
    } catch (e) { s.el.replaceChildren(h('p', { text: e.message })); }
  }
};

/* ===== Face Mesh: ядро ===== */
const LM = {
  OVAL: [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109],
  REYE: [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246], LEYE: [263, 249, 390, 373, 374, 380, 381, 382, 362, 398, 384, 385, 386, 387, 388, 466],
  BROWS: [[70, 63, 105, 66, 107], [46, 53, 52, 65, 55], [300, 293, 334, 296, 336], [276, 283, 282, 295, 285]],
  LIPO: [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267, 0, 37, 39, 40, 185], LIPI: [78, 95, 88, 178, 87, 14, 317, 402, 318, 324, 308, 415, 310, 311, 312, 13, 82, 81, 80, 191],
  RIRIS: [469, 470, 471, 472], LIRIS: [474, 475, 476, 477],
  RC: 468, LC: 473, REI: 133, LEI: 362, REO: 33, LEO: 263, RM: 61, LM: 291, RB: 105, LB: 334, NOSE: 1, CHIN: 152, BRIDGE: 6, RALA: 129, LALA: 358, RCH: 234, LCH: 454, TOP: 10
};
const FACE_MAX = 2; /* 2 лица: чтобы выбрать наибольшее и предупредить о втором */
/* положительный yaw: пациент повернул голову к своему левому плечу (знак проверяется автоматически по положению носа) */
const POSE_YAW = { frontal: 0, left45: 45, right45: -45, left90: 90, right90: -90 };
const fitMap = (vw, vh, cw, ch, fit) => { const s = fit === 'cover' ? Math.max(cw / vw, ch / vh) : Math.min(cw / vw, ch / vh); return { s, ox: (cw - vw * s) / 2, oy: (ch - vh * s) / 2 }; };
const deg = r => r * 180 / Math.PI;

const FaceMesh = {
  role: null, consent: false, mode: 'off', coach: false, autocrop: false, secondary: false, tol: 5, poses: ['frontal', 'left45', 'right45', 'left90', 'right90'],
  trk: null, trk2: null, els: {}, cal: null, latest: {}, info: null, unavailable: '', busy: 0, subs: new Set(), lastAssess: 0, lastSend: 0, motion: [], timers: [], photos: [], dpr: 1, sizes: new Map(), accent: '#007AFF', accT: 0,
  Loader: {}, Overlay: {}, Pose: {}, Symmetry: {}, Mobility: {}, Capture: {}, Compare: {}, Calibrate: {}, Privacy: {}, Report: {}, Remote: {}
};

/* --- Remote: расчёт метрик в сервисе vision через очередь заданий gateway.
   Если сервис недоступен или не успел ответить, используется расчёт на устройстве. --- */
FaceMesh.Remote = {
  WAIT: 6000, down: false,
  /* компактный кадр: только нужные точки и коэффициенты */
  pack(cur, idx, names) {
    const lm = {}, bs = {};
    if (idx) for (const i of idx) { const p = cur.lm[i]; if (p) lm[i] = [p.x, p.y, p.z]; }
    if (cur.bs) for (const c of cur.bs) if (!names || names.includes(c.categoryName)) bs[c.categoryName] = c.score;
    return { lm, mat: cur.mat ? Array.from(cur.mat) : null, bs, vw: cur.vw, vh: cur.vh, multi: !!cur.multi };
  },
  key() { return crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2, 12); },
  /* результат vision или null; ошибка расчёта (например, мало кадров) пробрасывается */
  async run(kind, input, signal) {
    if (!Api.room) return null;
    let job;
    try { job = await Api.post('/rooms/' + Api.room + '/analyze', { kind, key: this.key(), input }, { signal }); }
    catch (e) { if (e && e.name === 'AbortError') throw e; if (!(e && e.status === 503)) console.warn('vision', e); return null; }
    const t0 = performance.now();
    while (job.status === 'queued' || job.status === 'running') {
      if (performance.now() - t0 > this.WAIT) return null;
      await new Promise(r => setTimeout(r, 400));
      job = await Api.get('/rooms/' + Api.room + '/jobs/' + job.id, { signal }).catch(e => { if (e && e.name === 'AbortError') throw e; return job; });
    }
    if (job.status === 'done') return job.result;
    throw new Error((job.error && job.error.message) || 'Ошибка анализа');
  },
  async pick(kind, input, local, signal, downMsg) {
    const r = await this.run(kind, input, signal);
    if (r) { this.down = false; r.source = 'vision'; return r; }
    if (!this.down) Toast.show(downMsg || 'Сервис анализа временно недоступен: расчёт выполнен на этом устройстве');
    this.down = true; local.source = 'device';
    return local;
  }
};

/* --- Loader: локальные файлы MediaPipe с /vendor --- */
FaceMesh.Loader = {
  mod: null, fileset: null, conn: null,
  async load() {
    if (this.mod) return this.mod;
    if (!Cfg.mediapipe) throw new Error('Файлы MediaPipe не найдены на сервере');
    this.mod = await import('/vendor/vision_bundle.mjs');
    this.fileset = await this.mod.FilesetResolver.forVisionTasks('/vendor/wasm');
    this.conn = this.mod.FaceLandmarker.FACE_LANDMARKS_TESSELATION || null;
    return this.mod;
  },
  async create(running) {
    const m = await this.load();
    const mk = delegate => m.FaceLandmarker.createFromOptions(this.fileset, {
      baseOptions: { modelAssetPath: '/vendor/face_landmarker.task', delegate },
      runningMode: running || 'VIDEO', numFaces: FACE_MAX, outputFaceBlendshapes: true, outputFacialTransformationMatrixes: true
    });
    try { return await mk('GPU'); }
    catch (e) { console.warn('GPU недоступен, использую CPU', e); return await mk('CPU'); }
  }
};

/* --- Трекер: цикл detectForVideo --- */
class FMTracker {
  constructor(video, opt) {
    this.v = video; this.fps = opt.fps || 24; this.maxFps = opt.fps || 24; this.minFps = 10; this.ema = 0; this.last = 0; this.lastTs = 0;
    this.run = false; this.pending = false; this.cur = null; this.lastSeen = 0; this.faces = 0; this.errors = 0; this.onFrame = opt.onFrame || null; this.l = null; this.h = 0;
  }
  async start() {
    if (this.run) return;
    this.run = true;
    try { this.l = await FaceMesh.Loader.create('VIDEO'); } catch (e) { this.run = false; throw e; }
    if (!this.run) { try { this.l.close(); } catch (e) { /* ignore */ } return; }
    this.kick();
  }
  kick() {
    if (!this.run || this.pending) return;
    this.pending = true;
    const cb = () => { this.pending = false; this.tick(); };
    if (this.v.requestVideoFrameCallback) this.h = this.v.requestVideoFrameCallback(cb); else this.h = requestAnimationFrame(cb);
  }
  lost() { return !this.cur || performance.now() - this.lastSeen > 1500; }
  tick() {
    if (!this.run) return;
    const v = this.v;
    if (document.hidden || v.readyState < 2 || v.paused || !v.videoWidth) { setTimeout(() => this.kick(), 250); return; }
    const now = performance.now();
    if (now - this.last < 1000 / this.fps - 2) { this.kick(); return; }
    this.last = now;
    try {
      const ts = Math.max(now, this.lastTs + 1); this.lastTs = ts;
      const r = this.l.detectForVideo(v, ts);
      const dt = performance.now() - now;
      this.ema = this.ema * 0.8 + dt * 0.2;
      if (this.ema > 1000 / this.fps * 0.8 && this.fps > this.minFps) this.fps = Math.max(this.minFps, this.fps - 2);
      else if (this.ema < 1000 / this.fps * 0.35 && this.fps < this.maxFps) this.fps = Math.min(this.maxFps, this.fps + 1);
      this.consume(r, now);
      this.errors = 0;
    } catch (e) {
      console.error('Face Mesh', e);
      if (++this.errors > 5) { this.stop(); FaceMesh.fail('Модуль Face Mesh остановлен из-за ошибки обработки видео'); return; }
    }
    this.kick();
  }
  consume(r, now) {
    const faces = r.faceLandmarks || []; this.faces = faces.length;
    if (!faces.length) { this.cur = null; return; }
    let idx = 0, best = -1;
    faces.forEach((f, i) => { let a = 1, b = 0, c = 1, d = 0; for (const p of f) { if (p.x < a) a = p.x; if (p.x > b) b = p.x; if (p.y < c) c = p.y; if (p.y > d) d = p.y; } const ar = (b - a) * (d - c); if (ar > best) { best = ar; idx = i; } });
    const mats = r.facialTransformationMatrixes, bs = r.faceBlendshapes;
    this.cur = { lm: faces[idx], bs: bs && bs[idx] ? bs[idx].categories : null, mat: mats && mats[idx] ? mats[idx].data : null, t: now, vw: this.v.videoWidth, vh: this.v.videoHeight, multi: faces.length > 1 };
    this.lastSeen = now;
    if (this.onFrame) this.onFrame(this.cur, this);
  }
  stop() {
    this.run = false;
    try { if (this.v.cancelVideoFrameCallback && this.h) this.v.cancelVideoFrameCallback(this.h); } catch (e) { /* ignore */ }
    try { if (this.l) this.l.close(); } catch (e) { /* ignore */ }
    this.l = null; this.cur = null;
  }
}

/* --- Pose: углы, качество позы, тренер --- */
FaceMesh.Pose = {
  flip: 1, agree: 0, disagree: 0, cv1: null, cv2: null,
  rot(mat) {
    const sx = Math.hypot(mat[0], mat[1], mat[2]) || 1, sy = Math.hypot(mat[4], mat[5], mat[6]) || 1, sz = Math.hypot(mat[8], mat[9], mat[10]) || 1;
    return [[mat[0] / sx, mat[4] / sy, mat[8] / sz], [mat[1] / sx, mat[5] / sy, mat[9] / sz], [mat[2] / sx, mat[6] / sy, mat[10] / sz]];
  },
  euler(cur) {
    if (!cur || !cur.mat) return null;
    const R = this.rot(cur.mat);
    let yaw = deg(Math.atan2(R[0][2], R[2][2])), pitch = deg(Math.asin(clamp(-R[1][2], -1, 1))), roll = deg(Math.atan2(R[1][0], R[1][1]));
    /* самопроверка знака yaw по положению кончика носа между внешними уголками глаз */
    const lm = cur.lm, wid = lm[LM.LEO].x - lm[LM.REO].x;
    if (Math.abs(wid) > 1e-3) {
      const yl = (lm[LM.NOSE].x - (lm[LM.REO].x + lm[LM.LEO].x) / 2) / wid;
      if (Math.abs(yl) > 0.12 && Math.abs(yaw) > 10) { if (Math.sign(yl) === Math.sign(yaw)) this.agree++; else this.disagree++; if (this.agree + this.disagree >= 8) this.flip = this.disagree > this.agree ? -1 : 1; if (this.agree + this.disagree > 200) { this.agree = this.agree >> 1; this.disagree = this.disagree >> 1; } }
    }
    return { yaw: yaw * this.flip, pitch, roll };
  },
  geom(cur) {
    const lm = cur.lm; let a = 1, b = 0, c = 1, d = 0;
    for (const i of LM.OVAL) { const p = lm[i]; if (p.x < a) a = p.x; if (p.x > b) b = p.x; if (p.y < c) c = p.y; if (p.y > d) d = p.y; }
    return { x0: a, x1: b, y0: c, y1: d, size: Math.abs(lm[LM.CHIN].y - lm[LM.TOP].y), cx: (a + b) / 2, cy: (c + d) / 2 };
  },
  stats(video, g) {
    const W = video.videoWidth, H = video.videoHeight; if (!W) return null;
    if (!this.cv2) { this.cv2 = document.createElement('canvas'); this.cv2.width = this.cv2.height = 96; this.cv1 = document.createElement('canvas'); }
    const x2 = this.cv2.getContext('2d', { willReadFrequently: true });
    const sx = clamp(g.x0 * W, 0, W - 2), sy = clamp(g.y0 * H, 0, H - 2), sw = clamp((g.x1 - g.x0) * W, 2, W - sx), sh = clamp((g.y1 - g.y0) * H, 2, H - sy);
    x2.drawImage(video, sx, sy, sw, sh, 0, 0, 96, 96);
    const d = x2.getImageData(0, 0, 96, 96).data, gr = new Float32Array(9216);
    let sum = 0, sq = 0, over = 0;
    for (let i = 0, j = 0; i < d.length; i += 4, j++) { const y = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]; gr[j] = y; sum += y; sq += y * y; if (y > 245) over++; }
    const n = 9216, mean = sum / n, std = Math.sqrt(Math.max(0, sq / n - mean * mean));
    let ls = 0, lq = 0, ln = 0;
    for (let y = 1; y < 95; y++) for (let x = 1; x < 95; x++) { const i = y * 96 + x, l = -4 * gr[i] + gr[i - 1] + gr[i + 1] + gr[i - 96] + gr[i + 96]; ls += l; lq += l * l; ln++; }
    const sharp = lq / ln - (ls / ln) * (ls / ln);
    const x1 = this.cv1.getContext('2d', { willReadFrequently: true }); const w1 = 80, h1 = Math.max(8, Math.round(80 * H / W)); this.cv1.width = w1; this.cv1.height = h1;
    x1.drawImage(video, 0, 0, w1, h1); const e = x1.getImageData(0, 0, w1, h1).data; let bs = 0, bn = 0; const band = Math.floor(w1 * 0.12);
    for (let y = 0; y < h1; y++) for (let x = 0; x < w1; x++) { if (x < band || x >= w1 - band) { const i = (y * w1 + x) * 4; bs += 0.299 * e[i] + 0.587 * e[i + 1] + 0.114 * e[i + 2]; bn++; } }
    return { mean, std, over: over / n, sharp, bg: bn ? bs / bn : mean };
  },
  moving() {
    const m = FaceMesh.motion; if (m.length < 4) return false;
    let a = 1, b = 0, c = 1, d = 0; for (const p of m) { if (p.x < a) a = p.x; if (p.x > b) b = p.x; if (p.y < c) c = p.y; if (p.y > d) d = p.y; }
    return Math.max(b - a, d - c) > 0.02;
  },
  assess(cur, video, tol) {
    const e = this.euler(cur), g = this.geom(cur), st = this.stats(video, g);
    if (!e) return null;
    const aMax = Math.max(Math.abs(e.yaw), Math.abs(e.pitch), Math.abs(e.roll));
    let hint = '', level = 'good';
    if (aMax > tol * 2.5) { hint = 'Повернитесь к камере'; level = 'bad'; }
    else if (g.size < 0.2) { hint = 'Подвиньтесь ближе'; level = 'bad'; }
    else if (g.size < 0.3) { hint = 'Подвиньтесь ближе'; level = 'medium'; }
    else if (g.size > 0.8) { hint = 'Отодвиньтесь от камеры'; level = 'medium'; }
    else if (Math.abs(g.cx - 0.5) > 0.18 || Math.abs(g.cy - 0.5) > 0.18) { hint = 'Расположитесь по центру кадра'; level = 'medium'; }
    else if (st && st.mean < 70) { hint = 'Добавьте света спереди'; level = 'medium'; }
    else if (st && st.bg - st.mean > 45) { hint = 'Уберите свет за спиной'; level = 'medium'; }
    else if (st && st.over > 0.08) { hint = 'Слишком яркий свет на лице'; level = 'medium'; }
    else if (this.moving()) { hint = 'Держите голову неподвижно'; level = 'medium'; }
    else if (st && st.sharp < 12) { hint = 'Изображение размыто, держите голову неподвижно'; level = 'medium'; }
    else if (aMax > tol) { hint = 'Выровняйте голову'; level = 'medium'; }
    return Object.assign({ level, hint, tol, size: g.size, cx: g.cx, cy: g.cy, t: performance.now() }, e, st ? { mean: st.mean, over: st.over, sharp: st.sharp, bg: st.bg } : {});
  },
  inTol(e, tol) { return !!e && Math.abs(e.yaw) <= tol && Math.abs(e.pitch) <= tol && Math.abs(e.roll) <= tol; }
};

/* --- Overlay: сетка и контуры, HiDPI, зеркало --- */
FaceMesh.Overlay = {
  ro: null,
  init() {
    const F = FaceMesh; this.ro && this.ro.disconnect();
    this.ro = new ResizeObserver(es => { for (const en of es) this.fit(en.target); });
    for (const c of [F.els.ovR, F.els.ovL]) if (c) { this.ro.observe(c); this.fit(c); }
  },
  fit(c) { const dpr = window.devicePixelRatio || 1; const w = Math.max(1, Math.round(c.clientWidth * dpr)), h2 = Math.max(1, Math.round(c.clientHeight * dpr)); if (c.width !== w || c.height !== h2) { c.width = w; c.height = h2; } },
  poly(ctx, lm, idx, close, m, vw, vh) {
    ctx.beginPath();
    idx.forEach((i, k) => { const x = m.ox + lm[i].x * vw * m.s, y = m.oy + lm[i].y * vh * m.s; if (k) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    if (close) ctx.closePath(); ctx.stroke();
  },
  /* рисует сетку или контуры в контексте ctx; m: {s,ox,oy} - отображение кадра */
  draw(ctx, cur, vw, vh, mode, m, color) {
    const lm = cur.lm; ctx.lineWidth = 1; ctx.strokeStyle = color; ctx.lineJoin = 'round';
    if (mode === 'grid' && FaceMesh.Loader.conn) {
      ctx.globalAlpha = 0.45; ctx.beginPath();
      for (const c of FaceMesh.Loader.conn) { const a = lm[c.start], b = lm[c.end]; if (!a || !b) continue; ctx.moveTo(m.ox + a.x * vw * m.s, m.oy + a.y * vh * m.s); ctx.lineTo(m.ox + b.x * vw * m.s, m.oy + b.y * vh * m.s); }
      ctx.stroke(); ctx.globalAlpha = 1; return;
    }
    ctx.globalAlpha = 0.9;
    this.poly(ctx, lm, LM.OVAL, true, m, vw, vh); this.poly(ctx, lm, LM.REYE, true, m, vw, vh); this.poly(ctx, lm, LM.LEYE, true, m, vw, vh);
    this.poly(ctx, lm, LM.LIPO, true, m, vw, vh); this.poly(ctx, lm, LM.LIPI, true, m, vw, vh);
    for (const b of LM.BROWS) this.poly(ctx, lm, b, false, m, vw, vh);
    if (lm.length > 477) { this.poly(ctx, lm, LM.RIRIS, true, m, vw, vh); this.poly(ctx, lm, LM.LIRIS, true, m, vw, vh); }
    ctx.globalAlpha = 1;
  },
  frame() {
    const F = FaceMesh, t = performance.now();
    if (t - F.accT > 1500) { F.accT = t; F.accent = getComputedStyle(document.documentElement).getPropertyValue('--acc').trim() || '#007AFF'; }
    const dpr = window.devicePixelRatio || 1;
    const items = [[F.els.ovR, F.els.remote, 'remote', 'contain'], [F.els.ovL, F.els.local, 'local', 'cover']];
    for (const [c, v, which, fit] of items) {
      if (!c || !v) continue;
      const ctx = c.getContext('2d'); ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, c.width, c.height);
      if (F.mode === 'off') continue;
      const trk = F.trackerOf(which); if (!trk || trk.lost() || !trk.cur) continue;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const cur = trk.cur, m = fitMap(cur.vw, cur.vh, c.width / dpr, c.height / dpr, fit);
      this.draw(ctx, cur, cur.vw, cur.vh, F.mode, m, F.accent);
    }
  }
};

/* --- Privacy: скрытие лица пикселизацией до сохранения --- */
FaceMesh.Privacy = {
  hide(ctx, cur, vw, vh) {
    const lm = cur.lm; const cv = ctx.canvas;
    ctx.save(); ctx.beginPath();
    LM.OVAL.forEach((i, k) => { const x = lm[i].x * vw, y = lm[i].y * vh; if (k) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    ctx.closePath(); ctx.clip();
    let a = vw, b = 0; for (const i of LM.OVAL) { a = Math.min(a, lm[i].x * vw); b = Math.max(b, lm[i].x * vw); }
    const bs = Math.max(10, Math.round((b - a) / 10));
    const tmp = document.createElement('canvas'); tmp.width = Math.max(1, Math.round(vw / bs)); tmp.height = Math.max(1, Math.round(vh / bs));
    const tc = tmp.getContext('2d'); tc.drawImage(cv, 0, 0, tmp.width, tmp.height);
    ctx.imageSmoothingEnabled = false; ctx.drawImage(tmp, 0, 0, tmp.width, tmp.height, 0, 0, vw, vh); ctx.imageSmoothingEnabled = true;
    ctx.restore();
  }
};

/* --- Symmetry: метрики после приведения к анфасу --- */
FaceMesh.Symmetry = {
  IDX: [468, 473, 133, 362, 33, 263, 61, 291, 105, 334, 1, 152, 234, 454],
  derot(cur) {
    const R = FaceMesh.Pose.rot(cur.mat), A = [[R[0][0], -R[0][1], -R[0][2]], [-R[1][0], R[1][1], R[1][2]], [-R[2][0], R[2][1], R[2][2]]];
    const pts = this.IDX.map(i => ({ i, x: cur.lm[i].x * cur.vw, y: cur.lm[i].y * cur.vh, z: cur.lm[i].z * cur.vw }));
    const c = pts.reduce((o, p) => ({ x: o.x + p.x / pts.length, y: o.y + p.y / pts.length, z: o.z + p.z / pts.length }), { x: 0, y: 0, z: 0 });
    const P = {};
    for (const p of pts) { const dx = p.x - c.x, dy = p.y - c.y, dz = p.z - c.z; P[p.i] = { x: A[0][0] * dx + A[1][0] * dy + A[2][0] * dz, y: A[0][1] * dx + A[1][1] * dy + A[2][1] * dz }; }
    return P;
  },
  one(cur) {
    const P = this.derot(cur), R = P[468], L = P[473];
    const ex = L.x - R.x, ey = L.y - R.y, ipd = Math.hypot(ex, ey); if (ipd < 5) return null;
    const ux = ex / ipd, uy = ey / ipd, nx = -uy, ny = ux, Mx = (R.x + L.x) / 2, My = (R.y + L.y) / 2;
    const along = p => ((p.x - Mx) * ux + (p.y - My) * uy) / ipd, down = p => ((p.x - Mx) * nx + (p.y - My) * ny) / ipd;
    const v = { ipd, tilt: deg(Math.atan2(ey, ex)), nose: along(P[1]), chin: along(P[152]) };
    v.eoR = -along(P[33]); v.eoL = along(P[263]); v.eiR = -along(P[133]); v.eiL = along(P[362]);
    v.mR = -along(P[61]); v.mL = along(P[291]); v.bR = -down(P[105]); v.bL = -down(P[334]);
    v.cR = -down(P[61]); v.cL = -down(P[291]); v.wR = -along(P[234]); v.wL = along(P[454]);
    return v;
  },
  PAIRS: [['eo', 'Внешние уголки глаз: расстояние до средней линии', 'eoR', 'eoL'], ['ei', 'Внутренние уголки глаз: расстояние до средней линии', 'eiR', 'eiL'], ['mouth', 'Уголки рта: расстояние до средней линии', 'mR', 'mL'],
    ['brow', 'Высота бровей над линией глаз', 'bR', 'bL'], ['mcor', 'Высота уголков рта относительно линии глаз', 'cR', 'cL'], ['half', 'Ширина половин лица', 'wR', 'wL']],
  build(samples, cal) {
    const med = k => median(samples.map(s => s[k])), ipdPx = med('ipd');
    const k = cal && cal.ipd_mm ? cal.ipd_mm : null;
    const rows = this.PAIRS.map(([id, label, a, b]) => {
      const r = med(a), l = med(b), o = { id, label, right: r, left: l, diff: r - l };
      if (k) { o.right_mm = r * k; o.left_mm = l * k; o.diff_mm = (r - l) * k; }
      return o;
    });
    const singles = [{ id: 'tilt', label: 'Наклон линии глаз', value: med('tilt'), unit: 'deg' }, { id: 'nose', label: 'Смещение кончика носа от средней линии', value: med('nose'), unit: 'ipd' }, { id: 'chin', label: 'Смещение подбородка от средней линии', value: med('chin'), unit: 'ipd' }];
    if (k) for (const s of singles) if (s.unit === 'ipd') s.value_mm = s.value * k;
    const out = { kind: 'symmetry', pose_ok: true, samples: samples.length, ipd_px: ipdPx, tolerance: FaceMesh.tol, rows, singles, ts: Math.floor(Date.now() / 1000) };
    if (k) out.calibration = cal;
    return out;
  },
  /* окно 2.5 с: в выборку попадают только кадры с позой в допуске */
  run(onProgress, signal) {
    const F = FaceMesh, trk = F.trk, secs = 2.5, need = 12;
    return new Promise((res, rej) => {
      if (!trk) return rej(new Error('Модуль не запущен'));
      const S = [], FR = []; let t0 = 0, fail = null; const t00 = performance.now();
      const fn = cur => {
        if (signal && signal.aborted) { done(new Error('Отменено')); return; }
        const e = F.Pose.euler(cur);
        if (!F.Pose.inTol(e, F.tol) || cur.multi && false) { t0 = 0; S.length = 0; FR.length = 0; onProgress(0, 'Выровняйте голову: ' + (e ? 'yaw ' + e.yaw.toFixed(0) + ', pitch ' + e.pitch.toFixed(0) + ', roll ' + e.roll.toFixed(0) : '')); return; }
        if (!t0) t0 = performance.now();
        const v = this.one(cur); if (v) { S.push(v); FR.push(F.Remote.pack(cur, this.IDX, [])); }
        const p = Math.min(1, (performance.now() - t0) / (secs * 1000)); onProgress(p, 'Держите позу');
        if (p >= 1) { if (S.length >= need) done(null); else { t0 = 0; S.length = 0; FR.length = 0; } }
      };
      const tm = setInterval(() => { if (performance.now() - t00 > 25000) done(new Error('Не удалось получить устойчивую позу в допуске за отведённое время')); else if (trk.lost()) { t0 = 0; S.length = 0; FR.length = 0; onProgress(0, 'Лицо не найдено'); } }, 400);
      /* кадры для сервиса vision: неперечисляемое поле, в протокол не попадает */
      const done = err => { clearInterval(tm); F.subs.delete(fn); if (err) return rej(err); const out = this.build(S, F.cal); Object.defineProperty(out, '_frames', { value: FR }); res(out); };
      F.subs.add(fn);
    });
  }
};

/* --- Mobility: подвижность по blendshapes --- */
FaceMesh.Mobility = {
  TASKS: [
    { id: 'smile', label: 'Улыбка', cmd: 'Широко улыбнитесь и держите улыбку', r: 'mouthSmileRight', l: 'mouthSmileLeft' },
    { id: 'brow', label: 'Подъём бровей', cmd: 'Поднимите брови как можно выше', r: 'browOuterUpRight', l: 'browOuterUpLeft' },
    { id: 'eye', label: 'Закрытие глаз', cmd: 'Крепко зажмурьте глаза', r: 'eyeBlinkRight', l: 'eyeBlinkLeft' },
    { id: 'cheek', label: 'Надувание щёк', cmd: 'Надуйте щёки и держите', single: 'cheekPuff' },
    { id: 'mouth', label: 'Открытие рта', cmd: 'Широко откройте рот', single: 'jawOpen' },
    { id: 'lips', label: 'Поджатие губ', cmd: 'Плотно сожмите губы', r: 'mouthPressRight', l: 'mouthPressLeft' }
  ],
  val(cur, name) { if (!cur.bs) return 0; const c = cur.bs.find(x => x.categoryName === name); return c ? c.score : 0; },
  /* одно задание: максимум коэффициента за 4 с в кадрах с допустимой позой */
  runTask(task, onProgress, signal) {
    const F = FaceMesh, secs = 4, tol = Math.max(F.tol * 2, 10);
    return new Promise((res, rej) => {
      if (!F.trk) return rej(new Error('Модуль не запущен'));
      const t0 = performance.now(), FR = [], names = task.single ? [task.single] : [task.r, task.l]; let mr = 0, ml = 0, ms = 0, n = 0;
      const fn = cur => {
        if (signal && signal.aborted) { fin(new Error('Отменено')); return; }
        if (FR.length < 400) FR.push(F.Remote.pack(cur, [LM.REO, LM.LEO, LM.NOSE], names));
        const e = F.Pose.euler(cur);
        if (!F.Pose.inTol(e, tol)) { onProgress((performance.now() - t0) / (secs * 1000), 'Смотрите в камеру'); return; }
        n++;
        if (task.single) ms = Math.max(ms, this.val(cur, task.single)); else { mr = Math.max(mr, this.val(cur, task.r)); ml = Math.max(ml, this.val(cur, task.l)); }
        onProgress((performance.now() - t0) / (secs * 1000), task.cmd);
      };
      const tm = setInterval(() => { if (performance.now() - t0 >= secs * 1000) fin(null); }, 100);
      const fin = err => {
        clearInterval(tm); F.subs.delete(fn);
        if (err) return rej(err);
        if (n < 10) return rej(new Error('Недостаточно кадров с лицом в допустимой позе. Повторите задание.'));
        const o = { id: task.id, label: task.label };
        if (task.single) o.single = ms; else { o.right = mr; o.left = ml; o.ratio = Math.max(mr, ml) > 0.05 ? Math.min(mr, ml) / Math.max(mr, ml) : null; }
        Object.defineProperty(o, '_frames', { value: FR });
        res(o);
      };
      F.subs.add(fn);
    });
  }
};

/* --- Calibrate: масштаб в миллиметрах --- */
FaceMesh.Calibrate = {
  CARD_W: 85.6, CARD_H: 53.98, IRIS_MM: 11.7,
  ipdPx(cur) { const v = FaceMesh.Symmetry.one(cur); return v ? v.ipd : null; },
  irisPx(cur) {
    const P = []; const d = ids => { let best = 0; for (const a of ids) for (const b of ids) best = Math.max(best, Math.hypot((cur.lm[a].x - cur.lm[b].x) * cur.vw, (cur.lm[a].y - cur.lm[b].y) * cur.vh)); return best; };
    if (cur.lm.length < 478) return null; P.push(d(LM.RIRIS), d(LM.LIRIS)); return (P[0] + P[1]) / 2;
  },
  fromIris(samples) {
    const iris = median(samples.map(s => s.iris)), ipd = median(samples.map(s => s.ipd));
    if (!iris || !ipd) return null;
    const mmpp = this.IRIS_MM / iris;
    return { source: 'iris', label: 'по радужке (приблизительно, погрешность выше)', approx: true, mm_per_px: mmpp, ipd_mm: ipd * mmpp, ts: Math.floor(Date.now() / 1000) };
  },
  /* pts: 4 угла карты по порядку: левый верх, правый верх, правый низ, левый низ (в пикселях кадра) */
  fromCard(pts, ipdPx) {
    const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
    const w = (dist(pts[0], pts[1]) + dist(pts[3], pts[2])) / 2, hh = (dist(pts[0], pts[3]) + dist(pts[1], pts[2])) / 2;
    if (w < 20 || hh < 12) return null;
    const mw = this.CARD_W / w, mh = this.CARD_H / hh, mmpp = (mw + mh) / 2, dev = Math.abs(mw - mh) / mmpp;
    return { source: 'card', label: 'по банковской карте 85,6 мм', approx: false, mm_per_px: mmpp, ipd_mm: ipdPx * mmpp, skew: dev, ts: Math.floor(Date.now() / 1000) };
  }
};

/* ===== Face Mesh: управление, панели, съёмка, сравнение, отчёт ===== */
const SIGN_SYM = 'Числа для документирования, не диагноз';
const SIGN_MOB = 'Наблюдение для врача, не оценка по клинической шкале';
const fx = (v, n) => (v == null || !isFinite(v)) ? '-' : (v >= 0 ? '' : '−') + Math.abs(v).toFixed(n == null ? 3 : n);

Object.assign(FaceMesh, {
  blobs: new Map(), hist: null, remotePose: null, frameEl: null, cs: { s: 1, x: 0, y: 0 }, tg: { s: 1, x: 0, y: 0 }, starting: false, pp: null, dp: null, dTab: 'metrics', cmp: { pose: 'frontal', before: null, after: null }, ctl: null,
  trackerOf(which) { const doc = this.role === 'doctor'; return which === 'remote' ? (doc ? this.trk : this.trk2) : (doc ? this.trk2 : this.trk); },
  mainVideo() { return this.role === 'doctor' ? this.els.remote : this.els.local; },
  otherVideo() { return this.role === 'doctor' ? this.els.local : this.els.remote; },
  fail(msg) {
    this.unavailable = msg || 'Модуль Face Mesh недоступен';
    this.stopTrackers(); this.mode = 'off';
    Toast.show(this.unavailable, 'err', 7000);
    if (CallUi.r.bMesh) CallUi.r.bMesh.classList.add('hidden');
    this.refreshPanes();
  },
  mount(els) {
    this.stopAll();
    this.els = els; this.role = els.role; this.mode = 'off'; this.photos = []; this.latest = {}; this.info = null; this.cal = null; this.consent = false; this.coach = false; this.hist = null; this.remotePose = null;
    this.unavailable = Cfg.mediapipe ? '' : 'Модуль Face Mesh отключён: на сервере нет файлов MediaPipe. Видеозвонок работает без него.';
    if (this.unavailable && CallUi.r.bMesh) CallUi.r.bMesh.classList.add('hidden');
    const fp = App.proto.face || {}; this.tol = fp.tolerance || 5; this.poses = (fp.poses && fp.poses.length) ? fp.poses : ['frontal', 'left45', 'right45', 'left90', 'right90'];
    this.frameEl = h('div', { class: 'frame-ok hidden' }); els.wrap.append(this.frameEl);
    this.Overlay.init();
    this.frameFn = Loop.add(() => { this.Overlay.frame(); this.autoFrame(); });
    if (this.role === 'patient') {
      try { this.consent = sessionStorage.getItem('mars_fc_' + Api.room) === '1'; } catch (e) { this.consent = false; }
      this.sendConsent(); this.timers.push(setInterval(() => this.sendConsent(), 10000));
    }
    this.timers.push(setInterval(() => this.statusTick(), 500));
    this.vis = () => { if (!document.hidden) { for (const t of [this.trk, this.trk2]) if (t) t.kick(); } };
    document.addEventListener('visibilitychange', this.vis);
    if (this.role === 'doctor') this.loadPhotos().catch(() => { });
    this.refreshPanes();
  },
  stopTrackers() { for (const k of ['trk', 'trk2']) { if (this[k]) { this[k].stop(); this[k] = null; } } this.motion = []; if (this.els && this.els.ovR) { for (const c of [this.els.ovR, this.els.ovL]) if (c) c.getContext('2d').clearRect(0, 0, c.width, c.height); } },
  stopAll() {
    this.stopTrackers();
    this.timers.forEach(clearInterval); this.timers = [];
    if (this.frameFn) { Loop.del(this.frameFn); this.frameFn = null; }
    if (this.vis) { document.removeEventListener('visibilitychange', this.vis); this.vis = null; }
    this.subs.clear(); this.busy = 0; this.coach = false; this.autocrop = false; this.mode = 'off';
    if (this.Overlay.ro) { this.Overlay.ro.disconnect(); this.Overlay.ro = null; }
    for (const u of this.blobs.values()) URL.revokeObjectURL(u); this.blobs.clear();
    if (this.Compare.l) { try { this.Compare.l.close(); } catch (e) { /* ignore */ } this.Compare.l = null; }
    if (this.ctl) { try { this.ctl.abort(); } catch (e) { /* ignore */ } this.ctl = null; }
    this.cs = { s: 1, x: 0, y: 0 }; this.tg = { s: 1, x: 0, y: 0 };
    if (this.els && this.els.stage) this.els.stage.style.transform = '';
    if (this.frameEl) { this.frameEl.remove(); this.frameEl = null; }
  },
  /* --- согласие --- */
  setConsent(g) {
    if (this.role !== 'doctor' || this.consent === g) return;
    this.consent = g; State.set({ faceConsent: g });
    if (!g) { this.stopTrackers(); this.mode = 'off'; this.coach = false; this.autocrop = false; }
    this.refreshPanes();
  },
  sendConsent() { if (this.role === 'patient' && Api.room) Api.ev('face', { kind: 'consent', granted: this.consent }).catch(() => { }); },
  ensureConsent() {
    if (this.unavailable) { Toast.show(this.unavailable, null, 6000); return Promise.resolve(false); }
    if (this.consent) return Promise.resolve(true);
    return new Promise(res => {
      let done = false; const fin = v => { if (!done) { done = true; res(v); } };
      const s = Ui.sheet({
        title: 'Согласие на анализ лица', onClose: () => fin(false),
        actions: [{ label: 'Не согласен(на)', kind: 'sec', onClick: () => fin(false) }, {
          label: 'Согласен(на)', kind: 'pri', onClick: async () => {
            this.consent = true;
            try { sessionStorage.setItem('mars_fc_' + Api.room, '1'); } catch (e) { /* ignore */ }
            try { await Api.ev('control', { action: 'face_consent', granted: true }); } catch (e) { Toast.err(e); }
            this.sendConsent(); this.refreshPanes(); fin(true);
          }
        }]
      });
      s.el.append(h('p', { style: 'margin:0', text: 'Что анализируется: изображение с вашей камеры обрабатывается локально, на вашем устройстве.' }),
        h('p', { style: 'margin:0', text: 'Что передаётся врачу: числовые результаты и снимки, которые вы подтверждаете сами.' }),
        h('p', { style: 'margin:0', text: 'Что не сохраняется: поток видео. Он не записывается и не отправляется для анализа на сервер.' }),
        h('p', { class: 'note', style: 'margin:0', text: 'Согласие можно отозвать в любой момент в разделе «Задания».' }));
    });
  },
  async revoke() {
    this.consent = false; this.stopTrackers(); this.mode = 'off'; this.coach = false;
    try { sessionStorage.removeItem('mars_fc_' + Api.room); } catch (e) { /* ignore */ }
    try { await Api.ev('control', { action: 'face_consent', granted: false }); } catch (e) { Toast.err(e); }
    this.sendConsent(); this.els.hint.textContent = ''; this.els.pq.classList.add('hidden'); this.refreshPanes();
  },
  /* --- режимы --- */
  async setMode(m) {
    if (this.unavailable) { Toast.show(this.unavailable); return; }
    if (m !== 'off' && !this.consent) {
      if (this.role === 'doctor') { Toast.show('Пациент не дал согласие на анализ лица'); this.refreshPanes(); return; }
      if (!(await this.ensureConsent())) { this.refreshPanes(); return; }
    }
    this.mode = m; this.refreshPanes(); await this.sync();
  },
  toggle() { this.setMode(this.mode === 'off' ? 'grid' : 'off'); },
  cycleMode() { this.setMode({ off: 'grid', grid: 'contour', contour: 'off' }[this.mode]); },
  async sync() {
    if (this.unavailable) return;
    if (this.starting) { this.again = true; return; }
    const need = this.consent && (this.mode !== 'off' || this.coach || this.autocrop || this.busy > 0);
    const need2 = this.consent && this.secondary && this.mode !== 'off';
    this.starting = true;
    try {
      if (need && !this.trk) { this.trk = new FMTracker(this.mainVideo(), { fps: 24, onFrame: c => this.onFrame(c) }); await this.trk.start(); }
      else if (!need && this.trk) { this.trk.stop(); this.trk = null; this.info = null; this.showInfo(); }
      if (need2 && !this.trk2 && this.trk) { this.trk2 = new FMTracker(this.otherVideo(), { fps: 10 }); await this.trk2.start(); }
      else if (!need2 && this.trk2) { this.trk2.stop(); this.trk2 = null; }
    } catch (e) { console.error(e); this.trk = null; this.trk2 = null; this.starting = false; this.fail('Не удалось запустить Face Mesh: ' + ((e && e.message) || 'ошибка инициализации') + '. Видеозвонок продолжает работать.'); return; }
    this.starting = false;
    if (this.again) { this.again = false; return this.sync(); }
  },
  onFrame(cur) {
    const now = performance.now(), n = cur.lm[LM.NOSE];
    this.motion.push({ t: now, x: n.x, y: n.y }); while (this.motion.length && now - this.motion[0].t > 600) this.motion.shift();
    if (now - this.lastAssess > 250) {
      this.lastAssess = now;
      try { this.info = this.Pose.assess(cur, this.mainVideo(), this.tol); this.info.multi = cur.multi; this.info.lost = false; this.showInfo(); this.sendInfo(now); } catch (e) { console.error(e); }
    }
    for (const f of this.subs) f(cur);
  },
  statusTick() {
    const t = this.trk; if (!t || !this.info) return;
    const lost = t.lost();
    if (lost && !this.info.lost) { this.info = Object.assign({}, this.info, { lost: true, level: 'bad', hint: 'Лицо не найдено' }); this.showInfo(); this.sendInfo(performance.now(), true); }
  },
  showInfo() {
    const i = this.info, pq = this.els.pq, hint = this.els.hint; if (!pq) return;
    if (!i || !this.trk) { pq.classList.add('hidden'); hint.textContent = ''; if (this.frameEl) this.frameEl.classList.add('hidden'); return; }
    const lv = i.lost ? 'bad' : i.level;
    pq.classList.remove('hidden');
    pq.replaceChildren(h('span', { class: 'dot ' + { good: 'ok', medium: 'mid', bad: 'bad' }[lv] }), h('span', { text: (this.role === 'doctor' ? 'Поза по сжатому видео: ' : 'Поза: ') + { good: 'хорошая', medium: 'средняя', bad: 'плохая' }[lv] }));
    let txt = i.lost ? 'Лицо не найдено' : (i.multi ? 'В кадре несколько лиц: используется самое крупное. ' : '') + i.hint;
    if (this.role === 'patient' && (this.coach || this.busy)) hint.textContent = txt; else hint.textContent = '';
    if (this.frameEl) this.frameEl.classList.toggle('hidden', !(lv === 'good' && (this.coach || this.busy)));
  },
  sendInfo(now, force) {
    if (this.role !== 'patient' || !(this.coach || this.busy) || !this.info) return;
    if (!force && now - this.lastSend < 500) return; this.lastSend = now;
    const i = this.info;
    Api.ev('face', { kind: 'pose', level: i.lost ? 'bad' : i.level, yaw: Math.round(i.yaw || 0), pitch: Math.round(i.pitch || 0), roll: Math.round(i.roll || 0), hint: i.lost ? 'Лицо не найдено' : i.hint, lost: !!i.lost, multi: !!i.multi, tol: this.tol }).catch(() => { });
  },
  autoFrame() {
    if (this.role !== 'doctor' || !this.els.stage) return;
    const st = this.els.stage, cw = st.clientWidth, ch = st.clientHeight;
    if (!this.autocrop || !this.trk || this.trk.lost() || !this.trk.cur) { this.tg = { s: 1, x: 0, y: 0 }; }
    else {
      const cur = this.trk.cur, g = this.Pose.geom(cur), m = fitMap(cur.vw, cur.vh, cw, ch, 'contain');
      const fx0 = m.ox + g.cx * cur.vw * m.s, fy0 = m.oy + g.cy * cur.vh * m.s, fh = g.size * cur.vh * m.s;
      const s = clamp(ch * 0.5 / Math.max(fh, 1), 1, 2.2);
      const tx = clamp(-s * (fx0 - cw / 2), -(s - 1) * cw / 2, (s - 1) * cw / 2), ty = clamp(-s * (fy0 - ch / 2), -(s - 1) * ch / 2, (s - 1) * ch / 2);
      if (Math.abs(s - this.tg.s) > 0.06 || Math.abs(tx - this.tg.x) > cw * 0.03 || Math.abs(ty - this.tg.y) > ch * 0.03) this.tg = { s, x: tx, y: ty };
    }
    const c = this.cs, k = 0.08;
    const ns = c.s + (this.tg.s - c.s) * k, nx = c.x + (this.tg.x - c.x) * k, ny = c.y + (this.tg.y - c.y) * k;
    if (Math.abs(ns - c.s) > 0.0005 || Math.abs(nx - c.x) > 0.05 || Math.abs(ny - c.y) > 0.05) { this.cs = { s: ns, x: nx, y: ny }; st.style.transform = 'translate(' + nx.toFixed(2) + 'px,' + ny.toFixed(2) + 'px) scale(' + ns.toFixed(4) + ')'; }
  },
  /* --- события --- */
  onFace(d) {
    if (this.role !== 'doctor') return;
    if (d.kind === 'consent') { this.setConsent(!!d.granted); }
    else if (d.kind === 'pose') { this.remotePose = d; this.updatePoseRow(); }
    else if (d.kind === 'photo') { if (!this.photos.find(p => p.id === d.id)) { this.photos.push({ id: d.id, pose: d.pose, ts: d.ts }); Toast.show('Получен снимок: ' + (POSE_NAMES[d.pose] || d.pose)); this.refreshHistory(); if (this.dTab === 'capture') this.renderDoctor(); } }
  },
  onMeasure(d) {
    if (this.role !== 'doctor') return;
    if (d.kind === 'calibration') { this.cal = d.calibration; Toast.show('Получена калибровка размеров'); }
    else if (d.kind === 'symmetry' || d.kind === 'mobility') { this.latest[d.kind] = d; Toast.show(d.kind === 'symmetry' ? 'Получены метрики симметрии' : 'Получены результаты подвижности'); this.hist = null; }
    this.renderDoctor();
  },
  patientRequest(d) {
    if (d.action === 'set_face') { if (d.poses && d.poses.length) this.poses = d.poses; if (d.tolerance) this.tol = d.tolerance; this.refreshPanes(); return; }
    const kinds = { symmetry: 'измерение симметрии лица', mobility: 'задания на подвижность лица', capture: 'стандартную фотосъёмку лица', calibrate: 'калибровку размеров' };
    let what = '', run = null;
    if (d.action === 'request_measure' && kinds[d.kind]) { what = kinds[d.kind]; run = () => this.startKind(d.kind); }
    else if (d.action === 'request_retake') { what = 'повторить снимок: ' + (POSE_NAMES[d.pose] || ''); run = () => this.Capture.open([d.pose]); }
    if (!run) return;
    Ui.sound();
    const s = Ui.sheet({ title: 'Просьба врача', actions: [{ label: 'Позже', kind: 'sec' }, { label: 'Начать', kind: 'pri', onClick: () => { run(); } }] });
    s.el.append(h('p', { class: 'big-hint', text: 'Врач просит выполнить: ' + what }), h('p', { class: 'note', style: 'text-align:center;margin:0', text: 'Вы подтверждаете каждый результат перед отправкой.' }));
  },
  startKind(k) { if (k === 'symmetry') this.measureSym(); else if (k === 'mobility') this.measureMob(); else if (k === 'capture') this.Capture.open(this.poses); else if (k === 'calibrate') this.calibrateSheet(); },
  loadProto(p) {
    const f = p.face || {}; if (f.tolerance) this.tol = f.tolerance; if (f.poses && f.poses.length) this.poses = f.poses;
    this.refreshPanes();
  },
  /* --- вспомогательное: таблицы --- */
  symTable(p, forPrint) {
    const k = p.calibration && p.calibration.ipd_mm, rows = p.rows || [];
    const unit = k ? ' (МЗР / мм)' : ' (доли МЗР)';
    const src = (k ? ' Источник масштаба: ' + p.calibration.label + '.' : ' Калибровки нет: значения в мм не показываются.') + (p.source === 'device' ? ' Рассчитано на устройстве.' : p.source === 'vision' ? ' Рассчитано сервисом анализа.' : '');
    return h('div', { class: 'stack' },
      h('table', { class: 'tb' }, h('tr', null, h('th', { text: 'Метрика' + unit }), h('th', { class: 'n', text: 'Правая' }), h('th', { class: 'n', text: 'Левая' }), h('th', { class: 'n', text: 'Разность' })),
        rows.map(r => h('tr', null, h('td', { text: r.label }), h('td', { class: 'n', text: fx(r.right) + (k ? ' / ' + fx(r.right_mm, 1) : '') }), h('td', { class: 'n', text: fx(r.left) + (k ? ' / ' + fx(r.left_mm, 1) : '') }), h('td', { class: 'n', text: fx(r.diff) + (k ? ' / ' + fx(r.diff_mm, 1) : '') })))),
      h('table', { class: 'tb' }, (p.singles || []).map(s => h('tr', null, h('td', { text: s.label }), h('td', { class: 'n', text: s.unit === 'deg' ? fx(s.value, 1) + '°' : fx(s.value) + (k && s.value_mm != null ? ' / ' + fx(s.value_mm, 1) + ' мм' : '') })))),
      h('p', { class: 'note', style: 'margin:0', text: 'МЗР: межзрачковое расстояние. Положительное значение смещения: в сторону левой половины лица пациента. Кадров в выборке: ' + (p.samples || 0) + '.' + src }),
      h('p', { class: 'note', style: 'margin:0;font-weight:600', text: SIGN_SYM }));
  },
  mobTable(p) {
    return h('div', { class: 'stack' },
      h('table', { class: 'tb' }, h('tr', null, h('th', { text: 'Задание (максимум коэффициента 0-1)' }), h('th', { class: 'n', text: 'Правая' }), h('th', { class: 'n', text: 'Левая' }), h('th', { class: 'n', text: 'Соотношение' })),
        (p.tasks || []).map(t => t.single != null ? h('tr', null, h('td', { text: t.label + ' (обе стороны, модель не разделяет)' }), h('td', { class: 'n', text: fx(t.single, 2) }), h('td', { class: 'n', text: '-' }), h('td', { class: 'n', text: '-' }))
          : h('tr', null, h('td', { text: t.label }), h('td', { class: 'n', text: fx(t.right, 2) }), h('td', { class: 'n', text: fx(t.left, 2) }), h('td', { class: 'n', text: t.ratio == null ? '-' : fx(t.ratio, 2) })))),
      h('p', { class: 'note', style: 'margin:0;font-weight:600', text: SIGN_MOB }));
  },
  async sendResult(payload) {
    try { await Api.post('/rooms/' + Api.room + '/measurements', payload); await Api.ev('measure', payload); Toast.show('Результат отправлен врачу'); return true; }
    catch (e) { Toast.err(e); return false; }
  },
  /* лист с превью камеры для заданий пациента */
  taskSheet(title) {
    const vid = h('video', { autoplay: true, playsinline: true, class: 'mirror', style: 'width:100%;aspect-ratio:4/3;object-fit:cover;background:#000;border-radius:14px' }); vid.muted = true; vid.srcObject = Media.stream;
    const hint = h('div', { class: 'big-hint', role: 'status', 'aria-live': 'polite', text: '' });
    const prog = h('progress', { max: 1, value: 0, style: 'width:100%;height:12px' });
    const body = h('div', { class: 'stack' });
    const ctl = new AbortController();
    let closed = false;
    const s = Ui.sheet({ title, onClose: () => { if (closed) return; closed = true; ctl.abort(); this.busy = Math.max(0, this.busy - 1); this.sync(); } });
    s.el.append(vid, hint, prog, body);
    return { s, hint, prog, body, signal: ctl.signal, isClosed: () => closed };
  },
  async measureSym() {
    if (!(await this.ensureConsent())) return;
    this.busy++; await this.sync(); if (!this.trk) { this.busy--; return; }
    const T = this.taskSheet('Измерение симметрии');
    const go = async () => {
      T.body.replaceChildren();
      try {
        const loc = await this.Symmetry.run((p, msg) => { T.prog.value = p; T.hint.textContent = (this.trk && this.trk.lost()) ? 'Лицо не найдено' : msg; }, T.signal);
        if (T.isClosed()) return;
        T.hint.textContent = 'Расчёт...';
        const res = await this.Remote.pick('symmetry', { frames: loc._frames, tolerance: this.tol, calibration: this.cal }, loc, T.signal);
        if (T.isClosed()) return;
        T.hint.textContent = 'Готово'; T.prog.value = 1;
        T.body.replaceChildren(this.symTable(res), h('div', { class: 'row wrap' },
          h('button', { class: 'btn pri', onclick: async e => { e.target.disabled = true; if (await this.sendResult(res)) T.s.close(); else e.target.disabled = false; } }, 'Отправить врачу'),
          h('button', { class: 'btn sec', onclick: go }, 'Повторить')));
      } catch (e) { if (T.isClosed()) return; T.hint.textContent = ''; T.body.replaceChildren(h('p', { text: e.message }), h('button', { class: 'btn pri', onclick: go }, 'Повторить')); }
    };
    go();
  },
  async measureMob() {
    if (!(await this.ensureConsent())) return;
    this.busy++; await this.sync(); if (!this.trk) { this.busy--; return; }
    const T = this.taskSheet('Подвижность лица'), out = [];
    const finish = async () => {
      const loc = { kind: 'mobility', pose_ok: true, tolerance: Math.max(this.tol * 2, 10), tasks: out.slice(), ts: Math.floor(Date.now() / 1000) };
      T.hint.textContent = 'Расчёт...'; T.body.replaceChildren();
      let res;
      try { res = await this.Remote.pick('mobility', { tasks: out.map(t => ({ id: t.id, frames: t._frames })), tolerance: this.tol }, loc, T.signal); }
      catch (e) { if (T.isClosed()) return; T.hint.textContent = ''; T.body.replaceChildren(h('p', { text: e.message }), h('button', { class: 'btn pri', onclick: () => { out.length = 0; next(0); } }, 'Пройти заново')); return; }
      if (T.isClosed()) return;
      T.hint.textContent = 'Готово'; T.prog.value = 1;
      T.body.replaceChildren(this.mobTable(res), h('div', { class: 'row wrap' },
        h('button', { class: 'btn pri', onclick: async e => { e.target.disabled = true; if (await this.sendResult(res)) T.s.close(); else e.target.disabled = false; } }, 'Отправить врачу'),
        h('button', { class: 'btn sec', onclick: () => { out.length = 0; next(0); } }, 'Пройти заново')));
    };
    const next = i => {
      if (T.isClosed()) return;
      if (i >= this.Mobility.TASKS.length) return finish();
      const task = this.Mobility.TASKS[i];
      T.prog.value = 0; T.hint.textContent = task.label + '. Нажмите «Начать»';
      T.body.replaceChildren(h('button', { class: 'btn pri lg', onclick: async () => {
        T.body.replaceChildren(); T.hint.textContent = task.cmd;
        try { const r = await this.Mobility.runTask(task, (p, m) => { T.prog.value = Math.min(1, p); T.hint.textContent = m; }, T.signal); out.push(r); next(i + 1); }
        catch (e) { if (T.isClosed()) return; T.body.replaceChildren(h('p', { text: e.message }), h('button', { class: 'btn pri', onclick: () => next(i) }, 'Повторить задание')); }
      } }, 'Начать'), h('span', { class: 'note', text: 'Задание ' + (i + 1) + ' из ' + this.Mobility.TASKS.length }));
    };
    next(0);
  },
  async calibrateSheet() {
    if (!(await this.ensureConsent())) return;
    this.busy++; await this.sync(); if (!this.trk) { this.busy--; return; }
    const T = this.taskSheet('Калибровка размеров'), C = this.Calibrate;
    const saveCal = async cal => {
      this.cal = cal;
      if (await this.sendResult({ kind: 'calibration', calibration: cal, ts: cal.ts })) T.s.close();
    };
    const menu = () => {
      T.hint.textContent = ''; T.prog.value = 0;
      T.body.replaceChildren(
        h('p', { class: 'note', style: 'margin:0', text: 'Без калибровки показываются только безразмерные индексы. Калибровка нужна для значений в миллиметрах и действует только в анфасе.' }),
        h('button', { class: 'btn pri', onclick: card }, 'По банковской карте (85,6 мм)'),
        h('button', { class: 'btn sec', onclick: iris }, 'По радужке (приблизительно)'));
    };
    const iris = async () => {
      T.body.replaceChildren(); T.hint.textContent = 'Смотрите прямо в камеру';
      const S = []; const t0 = performance.now();
      const fn = cur => { const e = this.Pose.euler(cur); if (!this.Pose.inTol(e, this.tol)) { T.hint.textContent = 'Смотрите прямо в камеру'; return; } const ir = C.irisPx(cur), ip = C.ipdPx(cur); if (ir && ip) S.push({ iris: ir, ipd: ip }); T.prog.value = Math.min(1, S.length / 30); };
      this.subs.add(fn);
      const wait = setInterval(() => {
        if (T.isClosed() || S.length >= 30 || performance.now() - t0 > 15000) {
          clearInterval(wait); this.subs.delete(fn); if (T.isClosed()) return;
          const cal = S.length >= 12 ? C.fromIris(S) : null;
          if (!cal) { T.body.replaceChildren(h('p', { text: 'Не удалось получить устойчивый кадр в анфасе. Повторите.' }), h('button', { class: 'btn pri', onclick: iris }, 'Повторить')); return; }
          T.hint.textContent = 'Приблизительно, погрешность выше';
          T.body.replaceChildren(h('p', { style: 'margin:0', text: 'Межзрачковое расстояние: ' + cal.ipd_mm.toFixed(1) + ' мм (' + cal.label + ').' }),
            h('div', { class: 'row wrap' }, h('button', { class: 'btn pri', onclick: () => saveCal(cal) }, 'Сохранить'), h('button', { class: 'btn sec', onclick: menu }, 'Назад')));
        }
      }, 200);
    };
    const card = () => {
      T.hint.textContent = 'Держите карту у лба или щеки, лицо анфас';
      T.body.replaceChildren(h('p', { class: 'note', style: 'margin:0', text: 'Карту нужно держать в одной плоскости с лицом, ровно, лицом к камере. Нажмите «Сделать кадр», затем отметьте четыре угла карты.' }),
        h('button', { class: 'btn pri', onclick: shot }, 'Сделать кадр'), h('button', { class: 'btn sec', onclick: menu }, 'Назад'));
    };
    const shot = () => {
      const trk = this.trk, cur = trk && trk.cur, e = cur && this.Pose.euler(cur);
      if (!cur || trk.lost() || !this.Pose.inTol(e, this.tol)) { Toast.show('Нужен анфас в допуске: ' + (this.info && this.info.hint || 'смотрите прямо в камеру')); return; }
      const v = this.els.local, cv = document.createElement('canvas'); cv.width = v.videoWidth; cv.height = v.videoHeight; cv.getContext('2d').drawImage(v, 0, 0);
      cv.style.cssText = 'width:100%;border-radius:14px;touch-action:none;cursor:crosshair'; const pts = [];
      const names = ['левый верхний', 'правый верхний', 'правый нижний', 'левый нижний'];
      T.hint.textContent = 'Отметьте угол карты: ' + names[0];
      const ctx = cv.getContext('2d');
      cv.addEventListener('pointerdown', ev => {
        const r = cv.getBoundingClientRect(); const p = { x: (ev.clientX - r.left) * cv.width / r.width, y: (ev.clientY - r.top) * cv.height / r.height };
        if (pts.length >= 4) return; pts.push(p);
        ctx.fillStyle = '#FF9500'; ctx.beginPath(); ctx.arc(p.x, p.y, Math.max(5, cv.width / 200), 0, 7); ctx.fill();
        if (pts.length > 1) { ctx.strokeStyle = '#FF9500'; ctx.lineWidth = Math.max(2, cv.width / 500); ctx.beginPath(); ctx.moveTo(pts[pts.length - 2].x, pts[pts.length - 2].y); ctx.lineTo(p.x, p.y); ctx.stroke(); }
        if (pts.length < 4) { T.hint.textContent = 'Отметьте угол карты: ' + names[pts.length]; return; }
        const cal = C.fromCard(pts, C.ipdPx(cur));
        if (!cal) { T.hint.textContent = 'Углы отмечены неверно'; T.body.replaceChildren(h('button', { class: 'btn pri', onclick: card }, 'Повторить')); return; }
        T.hint.textContent = '';
        T.body.append(h('p', { style: 'margin:8px 0 0', text: 'Межзрачковое расстояние: ' + cal.ipd_mm.toFixed(1) + ' мм; масштаб ' + cal.mm_per_px.toFixed(4) + ' мм/пиксель.' + (cal.skew > 0.08 ? ' Пропорции карты искажены (' + (cal.skew * 100).toFixed(0) + '%): карта наклонена, точность снижена.' : '') }),
          h('div', { class: 'row wrap' }, h('button', { class: 'btn pri', onclick: () => saveCal(cal) }, 'Сохранить'), h('button', { class: 'btn sec', onclick: card }, 'Заново')));
      });
      T.body.replaceChildren(cv);
    };
    menu();
  },
  /* --- загрузка данных (врач) --- */
  async pid() {
    let p = App.proto.appointment && App.proto.appointment.patient_id;
    if (!p) { try { App.proto = Object.assign(App.proto, await Api.get('/rooms/' + Api.room + '/protocol')); p = App.proto.appointment.patient_id; } catch (e) { /* нет */ } }
    return p;
  },
  async loadPhotos() {
    const pid = await this.pid(); if (!pid) return [];
    const r = await Api.get('/patients/' + pid + '/photos'); this.allPhotos = r.photos;
    const aid = App.proto.appointment && App.proto.appointment.id;
    this.photos = r.photos.filter(p => p.appt_id === aid).map(p => ({ id: p.id, pose: p.pose, ts: p.ts }));
    this.refreshHistory(); return r.photos;
  },
  async loadHistory() {
    const pid = await this.pid(); if (!pid) return [];
    if (this.hist) return this.hist;
    const r = await Api.get('/patients/' + pid + '/measurements'); this.hist = r.measurements;
    const cals = this.hist.filter(m => m.kind === 'calibration'); if (cals.length && !this.cal) this.cal = cals[cals.length - 1].data.calibration;
    return this.hist;
  },
  async thumb(id) {
    if (this.blobs.has(id)) return this.blobs.get(id);
    const b = await Api.req('GET', '/photos/' + id, { blob: true }); const u = URL.createObjectURL(b); this.blobs.set(id, u); return u;
  },
  thumbEl(p, onclick) {
    const img = h('img', { alt: POSE_NAMES[p.pose] || p.pose, src: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=' });
    this.thumb(p.id).then(u => { img.src = u; }).catch(() => { });
    const fig = h('figure', null, onclick ? h('button', { style: 'display:block;width:100%', onclick, 'aria-label': 'Выбрать снимок ' + fmtDT(p.ts) }, img) : img, h('figcaption', { text: (POSE_NAMES[p.pose] || p.pose) + ', ' + fmtDT(p.ts) }));
    return fig;
  },
  refreshHistory() {
    const box = Files.hist; if (!box) return;
    box.replaceChildren(h('h3', { text: 'Снимки этого приёма' }), this.photos.length ? h('div', { class: 'thumbs' }, this.photos.map(p => this.thumbEl(p))) : h('p', { class: 'note', style: 'margin:0', text: 'Снимков пока нет.' }));
  },
  updatePoseRow() {
    const el = this.poseRow; if (!el) return; const d = this.remotePose;
    if (!d) { el.textContent = 'Поза пациента: нет данных'; return; }
    el.replaceChildren(h('span', { class: 'dot ' + { good: 'ok', medium: 'mid', bad: 'bad' }[d.level] }), ' Поза на устройстве пациента: ' + ({ good: 'хорошая', medium: 'средняя', bad: 'плохая' }[d.level]) + '. yaw ' + d.yaw + '°, pitch ' + d.pitch + '°, roll ' + d.roll + '°' + (d.hint ? '. ' + d.hint : ''));
  },
  refreshPanes() {
    if (CallUi.r.bMesh) CallUi.r.bMesh.setAttribute('aria-pressed', String(this.mode !== 'off'));
    if (this.role === 'patient') this.renderPatient(); else if (this.role === 'doctor') this.renderDoctor();
  },
  /* --- панель пациента --- */
  openTasks() { CallUi.showTab('face'); this.refreshPanes(); },
  patientPane() { this.pp = h('div', { class: 'stack' }); this.role = 'patient'; return this.pp; },
  renderPatient() {
    const box = this.pp; if (!box) return;
    if (this.unavailable) { box.replaceChildren(h('h3', { text: 'Задания для лица' }), h('p', { text: this.unavailable })); return; }
    if (!this.consent) {
      box.replaceChildren(h('h3', { text: 'Задания для лица' }), h('p', { text: 'Врач может попросить вас выполнить несколько простых заданий перед камерой: фотосъёмку и проверку подвижности лица. Для этого нужно отдельное согласие на анализ лица.' }),
        h('button', { class: 'btn pri lg', onclick: () => this.ensureConsent() }, 'Прочитать и дать согласие'));
      return;
    }
    const big = (label, fn, pressed) => h('button', { class: 'btn sec lg', style: 'justify-content:flex-start', 'aria-pressed': pressed == null ? null : String(pressed), onclick: fn }, label);
    const opts = h('div', { class: 'stack' },
      h('label', { class: 'row', style: 'min-height:44px' }, h('input', { type: 'checkbox', style: 'width:22px;height:22px', checked: this.hideFace || null, onchange: e => { this.hideFace = e.target.checked; this.refreshPanes(); } }), ' Скрыть лицо при сохранении'),
      h('label', { class: 'row', style: 'min-height:44px' }, h('input', { type: 'checkbox', style: 'width:22px;height:22px', checked: this.meshOnShot || null, disabled: this.hideFace || null, onchange: e => { this.meshOnShot = e.target.checked; } }), ' Накладывать сетку на снимок'));
    box.replaceChildren(h('h3', { text: 'Задания для лица' }), h('p', { class: 'note', style: 'margin:0', text: 'Видео обрабатывается на вашем устройстве. Врачу уходят только числа и снимки, которые вы подтвердили.' }),
      big(this.coach ? 'Выключить подсказки позы' : 'Подсказки позы', () => { this.coach = !this.coach; this.refreshPanes(); this.sync().then(() => { if (!this.coach) { this.showInfo(); } }); }, this.coach),
      big('Измерение симметрии', () => this.measureSym()), big('Подвижность лица', () => this.measureMob()),
      big('Фотосъёмка по ракурсам', () => this.Capture.open(this.poses)), big('Калибровка размеров', () => this.calibrateSheet()),
      h('div', { class: 'stack' }, h('span', { class: 'note', text: 'Показать сетку на видео' }), Ui.segmented([['off', 'Выкл'], ['grid', 'Сетка'], ['contour', 'Контуры']], this.mode, m => this.setMode(m), 'Сетка на видео')),
      opts, h('button', { class: 'btn txt', onclick: () => this.revoke() }, 'Отозвать согласие'));
  },
  /* --- панель врача --- */
  doctorPane() {
    this.role = 'doctor';
    this.dp = h('div', { class: 'stack' });
    return this.dp;
  },
  renderDoctor() {
    const box = this.dp; if (!box) return;
    if (this.unavailable) { box.replaceChildren(h('h3', { text: 'Лицо' }), h('p', { text: this.unavailable })); return; }
    const cons = this.consent ? h('span', { class: 'chip g', text: 'Пациент дал согласие' }) : h('span', { class: 'chip o', text: 'Пациент не дал согласие' });
    this.poseRow = h('div', { class: 'sm', role: 'status', 'aria-live': 'polite' }); this.updatePoseRow();
    const head = h('div', { class: 'stack' }, h('div', { class: 'rowb' }, h('h3', { text: 'Лицо' }), cons),
      Ui.segmented([['off', 'Выкл'], ['grid', 'Сетка'], ['contour', 'Контуры']], this.mode, m => this.setMode(m), 'Режим сетки (F)'),
      h('label', { class: 'row', style: 'min-height:44px' }, h('input', { type: 'checkbox', style: 'width:22px;height:22px', checked: this.autocrop || null, disabled: !this.consent || null, onchange: e => { this.autocrop = e.target.checked; this.sync(); } }), ' Автокадрирование по лицу (только у вас)'),
      h('label', { class: 'row', style: 'min-height:44px' }, h('input', { type: 'checkbox', style: 'width:22px;height:22px', checked: this.secondary || null, onchange: e => { this.secondary = e.target.checked; this.sync(); } }), ' Сетка и на самовидении'),
      this.poseRow,
      h('p', { class: 'note', style: 'margin:0', text: 'Сетка и поза по входящему видео построены по сжатому потоку и ориентировочны. Точные измерения выполняются на устройстве пациента.' }));
    const tabs = Ui.segmented([['metrics', 'Метрики'], ['capture', 'Съёмка'], ['compare', 'Сравнение'], ['report', 'Отчёт']], this.dTab, t => { this.dTab = t; this.renderDoctor(); }, 'Разделы');
    const body = h('div', { class: 'stack' });
    box.replaceChildren(head, tabs, body);
    ({ metrics: () => this.tabMetrics(body), capture: () => this.tabCapture(body), compare: () => this.tabCompare(body), report: () => this.tabReport(body) })[this.dTab]();
  },
  req(kind) {
    if (!this.consent) { Toast.show('Пациент не дал согласие на анализ лица'); return; }
    Api.ev('control', { action: 'request_measure', kind }).then(() => Toast.show('Запрос отправлен пациенту')).catch(Toast.err);
  },
  tabMetrics(body) {
    const L = this.latest;
    body.append(h('div', { class: 'row wrap' }, h('button', { class: 'btn sec', onclick: () => this.req('symmetry') }, 'Запросить симметрию'), h('button', { class: 'btn sec', onclick: () => this.req('mobility') }, 'Запросить подвижность'), h('button', { class: 'btn sec', onclick: () => this.req('calibrate') }, 'Запросить калибровку')));
    body.append(h('p', { class: 'note', style: 'margin:0', text: this.cal ? 'Калибровка: ' + this.cal.label + '; межзрачковое расстояние ' + this.cal.ipd_mm.toFixed(1) + ' мм.' : 'Калибровки нет: значения в миллиметрах не показываются.' }));
    if (L.symmetry) body.append(h('h3', { text: 'Симметрия (последнее измерение)' }), this.symTable(L.symmetry)); else body.append(h('p', { class: 'note', style: 'margin:0', text: 'Метрик симметрии в этом приёме пока нет.' }));
    if (L.mobility) body.append(h('h3', { text: 'Подвижность (последнее измерение)' }), this.mobTable(L.mobility));
    const chartBox = h('div', { class: 'stack' }); body.append(h('h3', { text: 'Динамика между приёмами' }), chartBox);
    this.loadHistory().then(hist => this.drawTrends(chartBox, hist)).catch(e => chartBox.replaceChildren(h('p', { class: 'note', text: e.message })));
  },
  /* ряды по приёмам: последнее измерение каждого приёма */
  series(hist, kind, pick) {
    const byAppt = new Map();
    for (const m of hist) if (m.kind === kind) byAppt.set(m.appt_id, m);
    const out = []; for (const m of byAppt.values()) { const v = pick(m.data); if (v != null && isFinite(v)) out.push({ x: m.ts, y: v }); }
    return out.sort((a, b) => a.x - b.x);
  },
  drawTrends(box, hist, forPrint) {
    const cvs = [];
    const symDefs = [['half', 'Ширина половин лица: разность'], ['brow', 'Высота бровей: разность'], ['mcor', 'Уголки рта: разность высот'], ['mouth', 'Уголки рта до средней линии: разность']];
    for (const [id, title] of symDefs) {
      const s = this.series(hist, 'symmetry', d => { const r = (d.rows || []).find(x => x.id === id); return r ? r.diff : null; });
      cvs.push([title + ' (доли МЗР)', [{ name: 'правая минус левая', points: s }]]);
    }
    const s1 = this.series(hist, 'symmetry', d => { const r = (d.singles || []).find(x => x.id === 'tilt'); return r ? r.value : null; }); cvs.push(['Наклон линии глаз (градусы)', [{ name: 'наклон', points: s1 }]]);
    const mob = id => this.series(hist, 'mobility', d => { const t = (d.tasks || []).find(x => x.id === id); return t && t.ratio != null ? t.ratio : null; });
    cvs.push(['Подвижность: соотношение сторон', [{ name: 'улыбка', points: mob('smile') }, { name: 'брови', points: mob('brow') }, { name: 'глаза', points: mob('eye') }, { name: 'губы', points: mob('lips') }]]);
    const nodes = []; const urls = [];
    for (const [title, ser] of cvs) {
      if (!ser.some(s => s.points.length)) continue;
      const cv = h('canvas', { style: 'width:100%;height:auto', 'aria-label': title }); this.Chart.line(cv, ser, { title, print: forPrint });
      nodes.push(cv); urls.push({ title, url: cv.toDataURL('image/png') });
    }
    if (box) box.replaceChildren(...(nodes.length ? nodes : [h('p', { class: 'note', style: 'margin:0', text: 'Данных по предыдущим приёмам пока нет. Графики появятся после нескольких измерений.' })]));
    return urls;
  },
  tabCapture(body) {
    const names = ['frontal', 'left45', 'right45', 'left90', 'right90'];
    const boxes = names.map(n => h('input', { type: 'checkbox', style: 'width:22px;height:22px', checked: this.poses.includes(n) || null, 'data-p': n }));
    const tol = Ui.input('number', '5', this.tol); tol.min = 1; tol.max = 15; tol.step = 0.5;
    const save = async () => {
      const poses = boxes.filter(b => b.checked).map(b => b.dataset.p); if (!poses.length) { Toast.err('Выберите хотя бы один ракурс'); return; }
      const t = clamp(parseFloat(tol.value) || 5, 1, 15);
      try { await Api.post('/rooms/' + Api.room + '/protocol', { face: { poses, tolerance: t } }); this.poses = poses; this.tol = t; await Api.ev('control', { action: 'set_face', poses, tolerance: t }); Toast.show('Набор ракурсов сохранён и отправлен пациенту'); } catch (e) { Toast.err(e); }
    };
    body.append(h('h3', { text: 'Набор ракурсов' }), h('div', { class: 'list' }, names.map((n, i) => h('label', { class: 'li' }, boxes[i], h('span', { class: 'grow', text: POSE_NAMES[n] })))),
      Ui.field('Допуск по углам, градусы (анфас: 5)', tol), h('button', { class: 'btn sec', onclick: save }, 'Сохранить набор'),
      h('button', { class: 'btn pri', onclick: () => this.req('capture') }, icon('camera'), 'Запросить съёмку у пациента'),
      h('h3', { text: 'Снимки этого приёма' }));
    if (!this.photos.length) body.append(h('p', { class: 'note', style: 'margin:0', text: 'Снимков пока нет.' }));
    else body.append(h('div', { class: 'thumbs' }, this.photos.map(p => { const f = this.thumbEl(p); f.append(h('button', { class: 'btn txt', style: 'min-height:32px;padding:0', onclick: () => { Api.ev('control', { action: 'request_retake', pose: p.pose }).then(() => Toast.show('Запрос на повторный снимок отправлен')).catch(Toast.err); } }, 'Повторить')); return f; })));
  },
  tabCompare(body) {
    const wrap = h('div', { class: 'stack' }); body.append(wrap, h('div', { class: 'sk', style: 'height:48px' }));
    this.loadPhotos().then(all => {
      body.replaceChildren(wrap);
      const poses = [...new Set(all.map(p => p.pose))].filter(p => all.filter(x => x.pose === p).length >= 2);
      if (!poses.length) { wrap.replaceChildren(h('h3', { text: 'Сравнение до и после' }), h('p', { class: 'note', style: 'margin:0', text: 'Нужно минимум два снимка одного ракурса одного пациента.' })); return; }
      if (!poses.includes(this.cmp.pose)) this.cmp = { pose: poses[0], before: null, after: null };
      const list = all.filter(p => p.pose === this.cmp.pose).sort((a, b) => a.ts - b.ts);
      const sel = (p) => { if (!this.cmp.before || (this.cmp.before && this.cmp.after)) { this.cmp.before = p; this.cmp.after = null; } else this.cmp.after = p; this.renderDoctor(); };
      wrap.replaceChildren(h('h3', { text: 'Сравнение до и после' }), Ui.segmented(poses.map(p => [p, POSE_NAMES[p] || p]), this.cmp.pose, p => { this.cmp = { pose: p, before: null, after: null }; this.renderDoctor(); }, 'Ракурс'),
        h('p', { class: 'note', style: 'margin:0', text: 'Нажмите снимок «до», затем снимок «после».' + (this.cmp.before ? ' До: ' + fmtDT(this.cmp.before.ts) + '.' : '') + (this.cmp.after ? ' После: ' + fmtDT(this.cmp.after.ts) + '.' : '') }),
        h('div', { class: 'thumbs' }, list.map(p => { const f = this.thumbEl(p, () => sel(p)); if ((this.cmp.before && this.cmp.before.id === p.id) || (this.cmp.after && this.cmp.after.id === p.id)) f.firstChild.classList.add('sel'); return f; })),
        h('button', { class: 'btn pri', disabled: !(this.cmp.before && this.cmp.after) || null, onclick: () => this.Compare.open(this.cmp.before, this.cmp.after, this.cmp.pose) }, icon('video2'), 'Сравнить'));
    }).catch(e => { body.replaceChildren(h('p', { text: e.message })); });
  },
  tabReport(body) {
    const out = h('div', { class: 'stack' });
    body.append(h('button', { class: 'btn pri', onclick: async e => { e.target.disabled = true; try { await this.Report.make(out); } catch (x) { Toast.err(x); } e.target.disabled = false; } }, icon('doc'), 'Сформировать отчёт'), out);
    if (this.lastReport) this.Report.show(out, this.lastReport);
  }
});

/* --- Chart: простой линейный график на canvas --- */
FaceMesh.Chart = {
  line(cv, series, o) {
    const W = cv.width = 560, H = cv.height = 240, ctx = cv.getContext('2d'), pr = o && o.print;
    const fg = pr ? '#000' : (getComputedStyle(document.body).color || '#000'), grid = pr ? '#bbb' : 'rgba(128,128,128,.35)';
    ctx.clearRect(0, 0, W, H); if (pr) { ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, W, H); }
    ctx.font = '13px sans-serif'; ctx.fillStyle = fg; ctx.textBaseline = 'top'; ctx.fillText(o.title || '', 8, 6);
    const pts = series.flatMap(s => s.points); if (!pts.length) return;
    let x0 = Math.min(...pts.map(p => p.x)), x1 = Math.max(...pts.map(p => p.x)), y0 = Math.min(...pts.map(p => p.y)), y1 = Math.max(...pts.map(p => p.y));
    if (x1 === x0) { x0 -= 86400; x1 += 86400; } if (y1 === y0) { y0 -= 0.05; y1 += 0.05; }
    const pad = (y1 - y0) * 0.1; y0 -= pad; y1 += pad;
    const L = 52, R = 12, T = 30, B = 44, pw = W - L - R, ph = H - T - B;
    const X = x => L + (x - x0) / (x1 - x0) * pw, Y = y => T + ph - (y - y0) / (y1 - y0) * ph;
    ctx.strokeStyle = grid; ctx.lineWidth = 1; ctx.fillStyle = fg; ctx.textBaseline = 'middle';
    for (let i = 0; i <= 4; i++) { const yv = y0 + (y1 - y0) * i / 4, yy = Y(yv); ctx.beginPath(); ctx.moveTo(L, yy); ctx.lineTo(W - R, yy); ctx.stroke(); ctx.textAlign = 'right'; ctx.fillText(yv.toFixed(Math.abs(y1 - y0) < 1 ? 2 : 1), L - 6, yy); }
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    const uniq = [...new Set(pts.map(p => p.x))].sort((a, b) => a - b); const step = Math.ceil(uniq.length / 5);
    uniq.forEach((x, i) => { if (i % step === 0) ctx.fillText(fmtDate(x), clamp(X(x), L + 20, W - R - 20), H - B + 8); });
    const cols = [pr ? '#0050B3' : (FaceMesh.accent || '#007AFF'), '#FF9500', '#34C759', '#AF52DE'];
    series.forEach((s, k) => {
      if (!s.points.length) return; ctx.strokeStyle = ctx.fillStyle = cols[k % cols.length]; ctx.lineWidth = 2; ctx.beginPath();
      s.points.forEach((p, i) => { if (i) ctx.lineTo(X(p.x), Y(p.y)); else ctx.moveTo(X(p.x), Y(p.y)); }); ctx.stroke();
      s.points.forEach(p => { ctx.beginPath(); ctx.arc(X(p.x), Y(p.y), 3.5, 0, 7); ctx.fill(); });
      if (series.length > 1) { ctx.textAlign = 'left'; ctx.textBaseline = 'top'; ctx.fillText(s.name, L + k * 90, H - 16); }
    });
  }
};

/* ===== Face Mesh: стандартизованная съёмка (пациент) ===== */
FaceMesh.Capture = {
  TEXT: {
    frontal: 'Смотрите прямо в камеру',
    left45: 'Поверните голову к левому плечу на 45 градусов',
    right45: 'Поверните голову к правому плечу на 45 градусов',
    left90: 'Профиль: поверните голову к левому плечу на 90 градусов',
    right90: 'Профиль: поверните голову к правому плечу на 90 градусов'
  },
  /* кодирование JPEG не больше лимита сервера (5 МБ) */
  async encode(cv) {
    const toBlob = (c, q) => new Promise(r => c.toBlob(r, 'image/jpeg', q));
    let c = cv, q = 0.92;
    for (let i = 0; i < 12; i++) {
      const b = await toBlob(c, q);
      if (b && b.size <= 4.6 * 1024 * 1024) return b;
      if (q > 0.6) q -= 0.1;
      else { const n = document.createElement('canvas'); n.width = Math.round(c.width * 0.8); n.height = Math.round(c.height * 0.8); n.getContext('2d').drawImage(c, 0, 0, n.width, n.height); c = n; }
    }
    throw new Error('Не удалось уменьшить снимок до допустимого размера');
  },
  /* снимок текущего кадра в полном разрешении видеоэлемента, без зеркала */
  grab(video, cur, hide, mesh) {
    const vw = video.videoWidth, vh = video.videoHeight;
    if (!vw || !vh) throw new Error('Камера не готова');
    const cv = document.createElement('canvas'); cv.width = vw; cv.height = vh;
    const ctx = cv.getContext('2d'); ctx.drawImage(video, 0, 0, vw, vh);
    const fresh = cur && performance.now() - cur.t < 700 && cur.vw === vw && cur.vh === vh;
    if (hide) {
      if (!fresh) throw new Error('Лицо не найдено в кадре: скрыть его перед сохранением невозможно. Снимок не сделан.');
      FaceMesh.Privacy.hide(ctx, cur, vw, vh);
    } else if (mesh && fresh) {
      const k = Math.max(1, vw / 640);
      ctx.save(); ctx.scale(k, k); FaceMesh.Overlay.draw(ctx, cur, vw, vh, 'contour', { s: 1 / k, ox: 0, oy: 0 }, '#34C759'); ctx.restore();
    }
    return cv;
  },
  async open(poses) {
    const F = FaceMesh;
    if (!(await F.ensureConsent())) return;
    if (!Media.stream || !Media.cam) { Toast.err('Для съёмки нужна включённая камера'); return; }
    const list = (poses && poses.length ? poses : ['frontal']).slice();
    F.busy++; await F.sync();
    if (!F.trk) { F.busy = Math.max(0, F.busy - 1); return; }
    let closed = false, idx = 0, stepToken = 0, hl = null;
    const vid = h('video', { autoplay: true, playsinline: true, class: 'mirror', style: 'position:absolute;inset:0;width:100%;height:100%;object-fit:cover' }); vid.muted = true; vid.srcObject = Media.stream;
    const ov = h('canvas', { style: 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none' });
    const count = h('div', { style: 'position:absolute;inset:0;display:grid;place-items:center;font-size:96px;font-weight:700;color:#fff;text-shadow:0 0 8px rgba(0,0,0,.7);pointer-events:none' });
    const stage = h('div', { style: 'position:relative;width:100%;aspect-ratio:3/4;max-height:62vh;background:#000;border-radius:14px;overflow:hidden;margin:0 auto' }, vid, ov, count);
    const title = h('div', { class: 'big-hint', role: 'status', 'aria-live': 'polite' });
    const sub = h('div', { class: 'note', style: 'text-align:center', 'aria-live': 'polite' });
    const prog = h('progress', { max: 1, value: 0, style: 'width:100%;height:12px' });
    const btns = h('div', { class: 'row wrap', style: 'justify-content:center' });
    const opts = h('div', { class: 'stack' },
      h('label', { class: 'row', style: 'min-height:44px' }, h('input', { type: 'checkbox', style: 'width:22px;height:22px', checked: F.hideFace || null, onchange: e => { F.hideFace = e.target.checked; mesh.disabled = F.hideFace; } }), ' Скрыть лицо (пикселизация до отправки)'));
    const mesh = h('input', { type: 'checkbox', style: 'width:22px;height:22px', checked: F.meshOnShot || null, disabled: F.hideFace || null, onchange: e => { F.meshOnShot = e.target.checked; } });
    opts.append(h('label', { class: 'row', style: 'min-height:44px' }, mesh, ' Наложить контуры на снимок'));
    const s = Ui.sheet({
      title: 'Фотосъёмка по ракурсам', wide: true, onClose: async () => {
        if (closed) return; closed = true; stepToken++; if (hl) Loop.del(hl); F.subs.delete(onCur);
        F.busy = Math.max(0, F.busy - 1); await Media.restore720(); F.sync();
      }
    });
    s.el.append(stage, title, sub, prog, btns, opts);
    let state = null; /* состояние шага */
    const pose = () => list[idx];
    /* подсказка направления и допуск */
    const evalPose = cur => {
      const p = pose(), tgt = POSE_YAW[p], e = F.Pose.euler(cur), tol = F.tol;
      const ey = e.yaw - tgt;
      const ok = Math.abs(ey) <= tol && Math.abs(e.pitch) <= tol && Math.abs(e.roll) <= tol;
      let hint = '';
      if (Math.abs(ey) > tol) hint = ey < 0 ? 'Поверните голову ещё левее (к левому плечу)' : 'Поверните голову ещё правее (к правому плечу)';
      else if (Math.abs(e.pitch) > tol) hint = e.pitch > 0 ? 'Опустите подбородок' : 'Приподнимите подбородок';
      else if (Math.abs(e.roll) > tol) hint = 'Не наклоняйте голову к плечу';
      return { ok, hint, e, ey };
    };
    const manualOnly = () => Math.abs(POSE_YAW[pose()]) > 60;
    const onCur = cur => {
      if (!state || state.busy || closed) return;
      if (manualOnly()) return;
      const r = evalPose(cur); state.last = r;
      const info = F.info;
      const good = r.ok && info && info.level !== 'bad' && !F.trk.lost() && !(cur.multi && false);
      const now = performance.now();
      if (good) {
        if (!state.since) state.since = now;
        const held = now - state.since;
        if (held >= 1000 && !state.counting) countdown(true);
        else if (!state.counting) { title.textContent = 'Держите позу'; prog.value = Math.min(1, held / 1000); }
      } else {
        state.since = 0; prog.value = 0;
        if (state.counting) { state.cancel = true; }
        if (!state.counting) title.textContent = info && info.hint && (!r.hint) ? info.hint : (r.hint || 'Выровняйте голову');
      }
    };
    F.subs.add(onCur);
    const countdown = async auto => {
      if (state.counting) return; state.counting = true; state.cancel = false; const tok = stepToken;
      for (let n = 3; n >= 1; n--) {
        count.textContent = String(n); title.textContent = 'Не двигайтесь';
        for (let t = 0; t < 10; t++) { await sleep(100); if (closed || tok !== stepToken) { count.textContent = ''; return; } if (auto && state.cancel) { state.counting = false; state.since = 0; count.textContent = ''; return; } }
      }
      count.textContent = '';
      if (closed || tok !== stepToken) return;
      state.counting = false; shoot();
    };
    const drawGuide = () => {
      if (closed) return;
      const w = ov.clientWidth, hh = ov.clientHeight; if (!w) return;
      const dpr = window.devicePixelRatio || 1; if (ov.width !== Math.round(w * dpr)) { ov.width = Math.round(w * dpr); ov.height = Math.round(hh * dpr); }
      const c = ov.getContext('2d'); c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, hh);
      const p = pose(), r = state && state.last, ok = r && r.ok;
      c.lineWidth = 3; c.strokeStyle = ok ? '#34C759' : 'rgba(255,255,255,.75)'; c.setLineDash(ok ? [] : [10, 8]);
      const prof = Math.abs(POSE_YAW[p]) > 60, narrow = Math.abs(POSE_YAW[p]) > 30 ? 0.26 : 0.34;
      c.beginPath(); c.ellipse(w / 2, hh * 0.47, w * (prof ? 0.2 : narrow), hh * 0.34, 0, 0, Math.PI * 2); c.stroke();
      c.setLineDash([]); c.lineWidth = 1.5; c.strokeStyle = 'rgba(255,255,255,.5)';
      c.beginPath(); c.moveTo(w / 2, hh * 0.1); c.lineTo(w / 2, hh * 0.84); c.moveTo(w * 0.25, hh * 0.4); c.lineTo(w * 0.75, hh * 0.4); c.stroke();
      if (r && !prof) { /* шкала отклонения по yaw: маркер уходит в ту же сторону, куда смещено лицо на зеркальном экране */
        const bw = w * 0.6, bx = (w - bw) / 2, by = hh * 0.93, off = clamp(r.ey / 30, -1, 1);
        c.strokeStyle = 'rgba(255,255,255,.6)'; c.lineWidth = 2; c.beginPath(); c.moveTo(bx, by); c.lineTo(bx + bw, by); c.stroke();
        c.fillStyle = ok ? '#34C759' : '#FF9500'; c.beginPath(); c.arc(w / 2 - off * bw / 2, by, 7, 0, Math.PI * 2); c.fill();
        c.fillStyle = '#fff'; c.fillRect(w / 2 - 1.5, by - 10, 3, 20);
      }
    };
    hl = Loop.add(drawGuide);
    const showStep = () => {
      stepToken++; state = { since: 0, counting: false, cancel: false, busy: false, last: null };
      const p = pose(); prog.value = 0; count.textContent = '';
      title.textContent = F.Capture.TEXT[p];
      sub.textContent = 'Ракурс ' + (idx + 1) + ' из ' + list.length + ': ' + POSE_NAMES[p] + (manualOnly() ? '. Профиль 90 градусов система не отслеживает: снимок делается вручную.' : '. Снимок сделается сам после удержания позы в допуске ' + F.tol + ' градусов.');
      btns.replaceChildren(
        h('button', { class: 'btn pri lg', onclick: () => { if (!state.busy) { state.since = 0; countdown(false); } } }, icon('camera'), 'Снять сейчас'),
        h('button', { class: 'btn sec', onclick: () => next() }, 'Пропустить ракурс'));
    };
    const next = () => { idx++; if (idx >= list.length) { title.textContent = 'Съёмка завершена'; sub.textContent = ''; prog.value = 1; btns.replaceChildren(h('button', { class: 'btn pri lg', onclick: () => s.close() }, 'Готово')); state = null; stepToken++; return; } showStep(); };
    const shoot = async () => {
      if (!state || state.busy) return; state.busy = true; const tok = stepToken;
      try {
        const cur = F.trk && F.trk.cur;
        const e = cur ? F.Pose.euler(cur) : null, tgt = POSE_YAW[pose()];
        const cv = this.grab(vid, cur, F.hideFace, F.meshOnShot);
        const blob = await this.encode(cv);
        if (closed || tok !== stepToken) return;
        const url = URL.createObjectURL(blob);
        const warn = (!manualOnly() && e && !(Math.abs(e.yaw - tgt) <= F.tol && Math.abs(e.pitch) <= F.tol && Math.abs(e.roll) <= F.tol)) ? 'Поза в момент снимка вне допуска. Лучше переснять.' : '';
        title.textContent = 'Проверьте снимок'; sub.textContent = warn || (F.hideFace ? 'Лицо скрыто пикселизацией: такой снимок нельзя использовать для сравнения.' : 'Нажмите «Отправить», если всё в порядке.');
        const img = h('img', { src: url, alt: 'Снимок', style: 'position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000' });
        stage.append(img);
        const done = () => { img.remove(); URL.revokeObjectURL(url); };
        btns.replaceChildren(
          h('button', { class: 'btn pri lg', onclick: async ev => {
            ev.target.disabled = true;
            try {
              await Api.req('POST', '/rooms/' + Api.room + '/frames', { raw: blob, headers: { 'Content-Type': 'image/jpeg', 'X-Pose': pose() } });
              Toast.show('Снимок отправлен врачу'); done(); next();
            } catch (er) { Toast.err(er); ev.target.disabled = false; }
          } }, 'Отправить'),
          h('button', { class: 'btn sec', onclick: () => { done(); showStep(); } }, 'Переснять'),
          h('button', { class: 'btn txt', onclick: () => { done(); next(); } }, 'Пропустить'));
        state.busy = true;
      } catch (er) { Toast.err(er); if (state) { state.busy = false; state.since = 0; state.counting = false; } }
    };
    Media.applyMax().then(() => { if (!closed) { title.textContent = F.Capture.TEXT[pose()]; } }).catch(() => { });
    showStep();
  }
};

/* ===== Face Mesh: сравнение до и после ===== */
FaceMesh.Compare = {
  l: null,
  RIGID: [33, 133, 362, 263, 6, 1, 168, 10],
  POINTS: [[105, 'Правая бровь'], [334, 'Левая бровь'], [33, 'Правый внешний угол глаза'], [263, 'Левый внешний угол глаза'], [1, 'Кончик носа'], [61, 'Правый угол рта'], [291, 'Левый угол рта'], [152, 'Подбородок']],
  async load(photo) {
    const url = await FaceMesh.thumb(photo.id);
    const img = new Image(); img.src = url; await img.decode();
    return img;
  },
  detect(img) {
    const r = this.l.detect(img);
    if (!r.faceLandmarks || !r.faceLandmarks.length) return null;
    const mats = r.facialTransformationMatrixes, bs = r.faceBlendshapes;
    let idx = 0, best = -1;
    r.faceLandmarks.forEach((f, i) => { let a = 1, b = 0, c = 1, d = 0; for (const p of f) { a = Math.min(a, p.x); b = Math.max(b, p.x); c = Math.min(c, p.y); d = Math.max(d, p.y); } if ((b - a) * (d - c) > best) { best = (b - a) * (d - c); idx = i; } });
    return { lm: r.faceLandmarks[idx], mat: mats && mats[idx] ? mats[idx].data : null, bs: bs && bs[idx] ? bs[idx].categories : null, vw: img.naturalWidth, vh: img.naturalHeight, t: performance.now(), multi: r.faceLandmarks.length > 1 };
  },
  /* подобие после -> до по жёстким точкам (метод наименьших квадратов, комплексная форма) */
  procrustes(A, B) {
    const pa = this.RIGID.map(i => ({ x: A.lm[i].x * A.vw, y: A.lm[i].y * A.vh })), pb = this.RIGID.map(i => ({ x: B.lm[i].x * B.vw, y: B.lm[i].y * B.vh }));
    const n = pa.length, ca = { x: pa.reduce((s, p) => s + p.x, 0) / n, y: pa.reduce((s, p) => s + p.y, 0) / n }, cb = { x: pb.reduce((s, p) => s + p.x, 0) / n, y: pb.reduce((s, p) => s + p.y, 0) / n };
    let re = 0, im = 0, den = 0;
    for (let i = 0; i < n; i++) { const ax = pa[i].x - ca.x, ay = pa[i].y - ca.y, bx = pb[i].x - cb.x, by = pb[i].y - cb.y; re += bx * ax + by * ay; im += bx * ay - by * ax; den += bx * bx + by * by; }
    const c = re / den, d = im / den;
    return { c, d, tx: ca.x - (c * cb.x - d * cb.y), ty: ca.y - (d * cb.x + c * cb.y), scale: Math.hypot(c, d), angle: deg(Math.atan2(d, c)) };
  },
  apply(T, x, y) { return { x: T.c * x - T.d * y + T.tx, y: T.d * x + T.c * y + T.ty }; },
  async open(before, after, pose) {
    const F = FaceMesh, self = this;
    if (F.unavailable) { Toast.show(F.unavailable); return; }
    let closed = false;
    const body = h('div', { class: 'stack' }, h('div', { class: 'sk', style: 'height:120px' }), h('p', { class: 'note', style: 'margin:0', text: 'Обработка снимков на вашем устройстве...' }));
    const s = Ui.sheet({ title: 'Сравнение: ' + (POSE_NAMES[pose] || pose), wide: true, onClose: () => { closed = true; if (self.l) { try { self.l.close(); } catch (e) { /* ignore */ } self.l = null; } } });
    s.el.append(body);
    try {
      const [ia, ib] = await Promise.all([this.load(before), this.load(after)]);
      if (closed) return;
      if (this.l) { try { this.l.close(); } catch (e) { /* ignore */ } }
      this.l = await F.Loader.create('IMAGE');
      const A = this.detect(ia), B = this.detect(ib);
      try { this.l.close(); } catch (e) { /* ignore */ } this.l = null;
      if (closed) return;
      if (!A || !B) { body.replaceChildren(h('p', { text: 'Лицо не найдено на ' + (!A && !B ? 'обоих снимках' : (!A ? 'снимке «до»' : 'снимке «после»')) + '. Если снимок был сохранён со скрытым лицом, сравнить его нельзя.' })); return; }
      const loc = this.calc(A, B, pose, F.tol, F.cal), side = (X, p) => ({ id: p.id, ts: p.ts, frame: F.Remote.pack(X, this.IDX, []) });
      const R = await F.Remote.pick('compare', { before: side(A, before), after: side(B, after), pose, tolerance: F.tol, calibration: F.cal }, loc, null,
        'Анализ временно недоступен, снимок сохранён и будет обработан позже. Сейчас показан расчёт на этом устройстве');
      if (closed) return;
      this.show(body, s, ia, ib, R, before, after, pose);
    } catch (e) { console.error(e); if (!closed) body.replaceChildren(h('p', { text: 'Не удалось выполнить сравнение: ' + ((e && e.message) || 'ошибка') })); }
  },
  IDX: [33, 133, 362, 263, 6, 1, 168, 10, 105, 334, 61, 291, 152, 468, 473],
  /* расчёт сравнения по точкам двух снимков; тот же результат отдаёт сервис vision (/v1/compare) */
  calc(A, B, pose, tol, cal) {
    const F = FaceMesh, tgt = POSE_YAW[pose], mm = pose === 'frontal' && cal && cal.ipd_mm ? cal : null;
    const chk = X => { const e = F.Pose.euler(X); if (!e) return { ok: false, e: null }; const dy = pose === 'frontal' ? Math.abs(e.yaw) : Math.abs(Math.abs(e.yaw) - Math.abs(tgt)); return { ok: dy <= tol && Math.abs(e.pitch) <= tol && Math.abs(e.roll) <= tol, e }; };
    const ca = chk(A), cb = chk(B), poseOk = ca.ok && cb.ok && !!A.mat && !!B.mat;
    const T = this.procrustes(A, B);
    /* нормировка: по МЗР для анфаса, по высоте лица для остальных ракурсов */
    const dist = (X, i, j) => Math.hypot((X.lm[i].x - X.lm[j].x) * X.vw, (X.lm[i].y - X.lm[j].y) * X.vh);
    const normName = pose === 'frontal' ? 'ipd' : 'face_height', norm = normName === 'ipd' ? dist(A, 468, 473) : dist(A, 10, 152);
    const rows = this.POINTS.map(([i, label]) => {
      const pa = { x: A.lm[i].x * A.vw, y: A.lm[i].y * A.vh }, pb = this.apply(T, B.lm[i].x * B.vw, B.lm[i].y * B.vh);
      const dx = (pb.x - pa.x) / norm, dy = (pb.y - pa.y) / norm;
      const o = { id: i, label, dx, dy, px: pa.x, py: pa.y, qx: pb.x, qy: pb.y };
      if (mm) { o.dx_mm = dx * mm.ipd_mm; o.dy_mm = dy * mm.ipd_mm; }
      return o;
    });
    return { kind: 'compare', pose, tolerance: tol, T, rows, ca, cb, pose_ok: poseOk, norm: normName, mm, multi: [!!A.multi, !!B.multi] };
  },
  show(body, sheet, ia, ib, R, before, after, pose) {
    const F = FaceMesh, tol = R.tolerance, mm = R.mm, T = R.T, rows = R.rows, ca = R.ca, cb = R.cb, poseOk = R.pose_ok, normName = R.norm;
    const W = ia.naturalWidth, H = ia.naturalHeight;
    const cv = document.createElement('canvas'); cv.width = W; cv.height = H; cv.setAttribute('aria-label', 'Сравнение снимков до и после');
    const ctx = cv.getContext('2d');
    const st = { mode: 'wipe', pos: 50, arrows: true };
    const render = () => {
      ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, W, H); ctx.globalAlpha = 1;
      ctx.drawImage(ia, 0, 0, W, H);
      const drawAfter = () => { ctx.setTransform(T.c, T.d, -T.d, T.c, T.tx, T.ty); ctx.drawImage(ib, 0, 0); ctx.setTransform(1, 0, 0, 1, 0, 0); };
      if (st.mode === 'blend') { ctx.globalAlpha = st.pos / 100; drawAfter(); ctx.globalAlpha = 1; }
      else {
        const x = W * st.pos / 100; ctx.save(); ctx.beginPath(); ctx.rect(x, 0, W - x, H); ctx.clip(); ctx.fillStyle = '#000'; ctx.fillRect(0, 0, W, H); drawAfter(); ctx.restore();
        ctx.strokeStyle = '#fff'; ctx.lineWidth = Math.max(2, W / 400); ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
      }
      if (st.arrows) {
        const lw = Math.max(2, W / 500); ctx.lineWidth = lw; ctx.strokeStyle = '#FF9500'; ctx.fillStyle = '#FF9500';
        for (const r of rows) {
          const len = Math.hypot(r.qx - r.px, r.qy - r.py); ctx.beginPath(); ctx.arc(r.px, r.py, lw * 1.8, 0, Math.PI * 2); ctx.fill();
          if (len < 3) continue;
          ctx.beginPath(); ctx.moveTo(r.px, r.py); ctx.lineTo(r.qx, r.qy); ctx.stroke();
          const a = Math.atan2(r.qy - r.py, r.qx - r.px), hs = lw * 5;
          ctx.beginPath(); ctx.moveTo(r.qx, r.qy); ctx.lineTo(r.qx - hs * Math.cos(a - 0.4), r.qy - hs * Math.sin(a - 0.4)); ctx.lineTo(r.qx - hs * Math.cos(a + 0.4), r.qy - hs * Math.sin(a + 0.4)); ctx.closePath(); ctx.fill();
        }
      }
    };
    const slider = h('input', { type: 'range', min: 0, max: 100, value: 50, style: 'width:100%', 'aria-label': 'Положение шторки или прозрачность', oninput: e => { st.pos = +e.target.value; render(); } });
    const unit = normName === 'ipd' ? (mm ? 'мм' : 'доли МЗР') : 'доли высоты лица';
    const f = v => v == null ? '-' : (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(mm ? 1 : 3);
    const tbl = h('table', { class: 'tb' }, h('tr', null, h('th', { text: 'Точка (после относительно до, ' + unit + ')' }), h('th', { class: 'n', text: 'По горизонтали' }), h('th', { class: 'n', text: 'По вертикали' })),
      rows.map(r => h('tr', null, h('td', { text: r.label }), h('td', { class: 'n', text: f(mm ? r.dx_mm : r.dx) }), h('td', { class: 'n', text: f(mm ? r.dy_mm : r.dy) }))));
    const warn = [];
    if (!poseOk) warn.push('Поза на одном или обоих снимках вне допуска ' + tol + ' градусов от цели (до: ' + (ca.e ? 'yaw ' + ca.e.yaw.toFixed(0) + ', pitch ' + ca.e.pitch.toFixed(0) + ', roll ' + ca.e.roll.toFixed(0) : 'нет данных') + '; после: ' + (cb.e ? 'yaw ' + cb.e.yaw.toFixed(0) + ', pitch ' + cb.e.pitch.toFixed(0) + ', roll ' + cb.e.roll.toFixed(0) : 'нет данных') + '). Результат ориентировочный и в протокол не сохраняется.');
    if (R.multi && (R.multi[0] || R.multi[1])) warn.push('На снимке найдено более одного лица: использовано самое крупное.');
    if (pose === 'frontal' && !mm) warn.push('Калибровки нет: значения даны в долях межзрачкового расстояния, не в миллиметрах.');
    if (mm && mm.approx) warn.push('Калибровка по радужке приблизительная: миллиметры ориентировочные. Для точности используйте калибровку по карте.');
    if (pose !== 'frontal') warn.push('Для ракурса, отличного от анфаса, миллиметры не показываются: расстояния искажены поворотом.');
    const msg = h('div', { class: 'stack' }, warn.map(w => h('div', { class: 'banner warn', style: 'border-radius:10px', text: w })));
    const saveBtn = h('button', { class: 'btn pri', disabled: !poseOk || null, onclick: async e => {
      e.target.disabled = true;
      const payload = { kind: 'compare', pose_ok: true, source: R.source, pose, before_id: before.id, after_id: after.id, before_ts: before.ts, after_ts: after.ts, norm: normName, scale: T.scale, rotation_deg: T.angle, tolerance: tol, rows: rows.map(r => { const o = { id: r.id, label: r.label, dx: r.dx, dy: r.dy }; if (mm) { o.dx_mm = r.dx_mm; o.dy_mm = r.dy_mm; } return o; }), ts: Math.floor(Date.now() / 1000) };
      if (mm) payload.calibration = mm;
      try { await Api.post('/rooms/' + Api.room + '/measurements', payload); F.hist = null; Toast.show('Сравнение сохранено в протокол'); } catch (er) { Toast.err(er); e.target.disabled = false; }
    } }, 'Сохранить в протокол');
    const dl = h('button', { class: 'btn sec', onclick: () => cv.toBlob(b => { const a = h('a', { href: URL.createObjectURL(b), download: 'сравнение.png' }); document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500); }, 'image/png') }, icon('download'), 'Скачать изображение');
    body.replaceChildren(
      Ui.segmented([['wipe', 'Шторка'], ['blend', 'Наложение']], st.mode, m => { st.mode = m; render(); }, 'Режим сравнения'),
      h('div', { class: 'cmp' }, cv), slider,
      h('p', { class: 'note', style: 'margin:0', text: 'До: ' + fmtDT(before.ts) + '. После: ' + fmtDT(after.ts) + '. Снимок «после» совмещён со снимком «до» по жёстким точкам (масштаб ' + T.scale.toFixed(3) + ', поворот ' + T.angle.toFixed(1) + ' градусов).' }),
      h('label', { class: 'row', style: 'min-height:44px' }, h('input', { type: 'checkbox', checked: true, style: 'width:22px;height:22px', onchange: e => { st.arrows = e.target.checked; render(); } }), ' Стрелки смещения ключевых точек'),
      msg, tbl, h('p', { class: 'note', style: 'margin:0;font-weight:600', text: SIGN_SYM }),
      h('div', { class: 'row wrap' }, saveBtn, dl));
    render();
  }
};

/* ===== Face Mesh: отчёт ===== */
FaceMesh.Report = {
  async collect() {
    const F = FaceMesh, a = App.proto.appointment || {};
    const hist = await F.loadHistory().catch(() => []);
    const mine = k => { const l = hist.filter(m => m.kind === k && m.appt_id === a.id); return l.length ? l[l.length - 1].data : null; };
    const sym = F.latest.symmetry || mine('symmetry'), mob = F.latest.mobility || mine('mobility');
    const cmpList = hist.filter(m => m.kind === 'compare' && m.appt_id === a.id).map(m => m.data);
    const rep = { kind: 'report', center: a.center || Cfg.center, patient: a.patient_name || '', doctor: a.doctor || '', appointment_id: a.id, date: fmtDT(Math.floor(Date.now() / 1000)), ts: Math.floor(Date.now() / 1000), symmetry: sym || null, mobility: mob || null, compare: cmpList, photo_ids: F.photos.map(p => ({ id: p.id, pose: p.pose, ts: p.ts })) };
    const cal = (sym && sym.calibration) || (cmpList.find(c => c.calibration) || {}).calibration || F.cal;
    if (cal) rep.calibration = cal;
    return { rep, hist };
  },
  async make(out) {
    const F = FaceMesh;
    out.replaceChildren(h('div', { class: 'sk', style: 'height:60px' }));
    const { rep, hist } = await this.collect();
    const trends = F.drawTrends(null, hist, true);
    F.lastReport = { rep, trends };
    this.show(out, F.lastReport);
  },
  node(rp) {
    const F = FaceMesh, rep = rp.rep;
    const kids = [h('h1', { text: rep.center }), h('h2', { text: 'Документирование лица: отчёт' }),
      h('table', null, h('tr', null, h('th', { text: 'Пациент' }), h('td', { text: rep.patient })), h('tr', null, h('th', { text: 'Дата' }), h('td', { text: rep.date })), h('tr', null, h('th', { text: 'Врач' }), h('td', { text: rep.doctor })),
        h('tr', null, h('th', { text: 'Калибровка размеров' }), h('td', { text: rep.calibration ? rep.calibration.label + '; межзрачковое расстояние ' + rep.calibration.ipd_mm.toFixed(1) + ' мм' : 'нет (значения в миллиметрах не приводятся)' })))];
    if (rep.symmetry) kids.push(h('h2', { text: 'Симметрия' }), F.symTable(rep.symmetry, true));
    if (rep.mobility) kids.push(h('h2', { text: 'Подвижность лица' }), F.mobTable(rep.mobility));
    if (rp.trends && rp.trends.length) { kids.push(h('h2', { text: 'Динамика между приёмами' })); for (const t of rp.trends) kids.push(h('img', { src: t.url, alt: t.title, style: 'max-height:60mm;display:block' })); }
    if (rep.compare && rep.compare.length) for (const c of rep.compare) {
      const mm = !!c.calibration, f = v => v == null ? '-' : (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(mm ? 1 : 3);
      kids.push(h('h2', { text: 'Сравнение до и после: ' + (POSE_NAMES[c.pose] || c.pose) }), h('p', { text: 'До: ' + fmtDT(c.before_ts) + '. После: ' + fmtDT(c.after_ts) + '. Единицы: ' + (c.norm === 'ipd' ? (mm ? 'мм' : 'доли МЗР') : 'доли высоты лица') + '.' }),
        h('table', { class: 'tb' }, h('tr', null, h('th', { text: 'Точка' }), h('th', { class: 'n', text: 'По горизонтали' }), h('th', { class: 'n', text: 'По вертикали' })), c.rows.map(r => h('tr', null, h('td', { text: r.label }), h('td', { class: 'n', text: f(mm ? r.dx_mm : r.dx) }), h('td', { class: 'n', text: f(mm ? r.dy_mm : r.dy) })))));
    }
    if (rep.photo_ids.length) { kids.push(h('h2', { text: 'Снимки приёма' }), h('div', { class: 'thumbs' }, rep.photo_ids.map(p => F.thumbEl(p)))); }
    kids.push(h('p', { class: 'sm', text: 'Документ содержит числовые наблюдения для документирования. Он не является диагнозом и не заменяет клинической оценки врача. ' + SIGN_SYM + '. ' + SIGN_MOB + '.' }),
      h('div', { class: 'sign', text: 'Подпись врача ______________________ / ' + rep.doctor + ' /' }));
    return h('div', null, kids);
  },
  show(out, rp) {
    const F = FaceMesh, rep = rp.rep;
    const none = !rep.symmetry && !rep.mobility && !(rep.compare && rep.compare.length) && !rep.photo_ids.length;
    out.replaceChildren(
      h('p', { class: 'note', style: 'margin:0', text: 'Отчёт сформирован ' + rep.date + '. Симметрия: ' + (rep.symmetry ? 'есть' : 'нет') + '; подвижность: ' + (rep.mobility ? 'есть' : 'нет') + '; сравнений: ' + (rep.compare || []).length + '; снимков: ' + rep.photo_ids.length + '; графиков: ' + (rp.trends || []).length + '.' }),
      ...(none ? [h('div', { class: 'banner warn', style: 'border-radius:10px', text: 'В этом приёме пока нет данных для отчёта.' })] : []),
      h('div', { class: 'row wrap' },
        h('button', { class: 'btn pri', onclick: async () => {
          const node = this.node(rp);
          const imgs = [...node.querySelectorAll('img')];
          await Promise.all(imgs.map(i => i.decode ? i.decode().catch(() => { }) : Promise.resolve()));
          Print.go(node);
        } }, 'Печать или PDF'),
        h('button', { class: 'btn sec', onclick: () => Ui.download('отчёт-лицо-' + (rep.appointment_id || 'приём') + '.json', JSON.stringify(rep, null, 2)) }, icon('download'), 'Скачать JSON'),
        h('button', { class: 'btn sec', onclick: async e => { e.target.disabled = true; try { await Api.post('/rooms/' + Api.room + '/measurements', rep); Toast.show('Отчёт сохранён в протокол'); } catch (er) { Toast.err(er); } e.target.disabled = false; } }, 'Сохранить в протокол')));
  }
};

App.init();
</script></body></html>'''


if __name__ == "__main__":
    main()
