# ----------------------------------------------------------------------------
# База данных
# ----------------------------------------------------------------------------
# Gateway SQL не содержит: все запросы лежат в database/queries.py и вызываются по имени.
# С сервисом database (run_all.py) запросы идут к нему по HTTP; в одиночном режиме (python gateway/mars.py)
# тот же код хранилища работает внутри процесса.
try:
    from database.store import Store as _Store, decode as _db_decode, encode as _db_encode
except Exception:  # noqa: BLE001
    _Store = None


class DatabaseUnavailable(Exception):
    """Сервис database не ответил: запрос получает 503, сигнализация звонка продолжает работать."""


class LocalDB:
    def __init__(self, path):
        self.store = _Store(path)

    def call(self, name, args=()):
        return self.store.call(name, list(args))

    q = one = run = call

    def close(self):
        self.store.close()


class RemoteDB:
    def __init__(self):
        self.cli = _ServiceClient("database", threshold=3, probe_every=2.0)

    def call(self, name, args=()):
        try:
            st, data = self.cli.call("POST", "/v1/q", {"name": name, "args": _db_encode(list(args))}, timeout=5.0)
        except Exception as e:  # noqa: BLE001  (ServiceUnavailable: сбой, таймаут, выключатель цепи)
            raise DatabaseUnavailable(str(e))
        if st != 200 or not isinstance(data, dict):
            raise RuntimeError("database %s: %s" % (st, (data or {}).get("message")))
        return _db_decode(data.get("result"))

    q = one = run = call

    def close(self):
        pass


def open_db():
    if _svc_config is not None and _ServiceClient is not None and "database" in _svc_config.monitored_services():
        db = RemoteDB()
        t = time.time()
        while True:  # run_all запускает database вместе с gateway: ждём, пока он поднимет базу
            try:
                db.one("doctors.admin_password")
                return db
            except DatabaseUnavailable:
                if time.time() - t > 120:
                    raise
                time.sleep(0.5)
    if _Store is None:
        raise SystemExit("Нет папки database рядом с gateway: без неё база недоступна")
    return LocalDB(DATA_DIR / "mars.sqlite3")


def audit(actor, action, room="", detail=""):
    try:
        DB.run("audit.add",
               (now(), actor, action, room, clean_text(detail, 200)))
    except Exception:
        log.exception("audit")


def hash_pw(pw, salt):
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def create_doctor(login, name, password, is_admin=0):
    salt = secrets.token_bytes(16)
    return DB.run("doctors.add",
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
    row = DB.one("doctors.admin_password")
    if not row:
        create_doctor("admin", "Администратор", pw, 1)
    elif not hmac.compare_digest(bytes(row["pw"]), hash_pw(pw, bytes(row["salt"]))):
        salt = secrets.token_bytes(16)
        DB.run("doctors.set_admin_password", (salt, hash_pw(pw, salt), row["id"]))
        DB.run("sessions.delete_for_doctor", (row["id"],))
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
        self.doc_tokens = {}  # токен -> логин врача: у каждого врача своя ссылка, в аудите видно, кто именно
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
        if not DB.one("appointments.id_by_code", (code,)) and code not in ROOMS:
            return code
    raise ApiError(500, "internal", "Не удалось создать код")


def get_room(code, create=False):
    code = code.upper()
    with COND:
        r = ROOMS.get(code)
        if r:
            return r
    appt = DB.one("appointments.status_by_code", (code,))
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
    """(роль, логин врача или None) по токену комнаты."""
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, None
    tok = auth[7:].strip().encode("utf-8", "ignore")
    with COND:
        for t, login in room.doc_tokens.items():
            if hmac.compare_digest(tok, t.encode()):
                return "doctor", login
        if room.pat_token and hmac.compare_digest(tok, room.pat_token.encode()):
            return "patient", None
    return None, None


def cleanup_loop(stop_evt):
    while not stop_evt.wait(60):
        try:
            t = time.time()
            with COND:
                for code in [c for c, r in ROOMS.items() if t - r.last > ROOM_TTL]:
                    del ROOMS[code]
            DB.run("sessions.delete_expired", (now(),))
            jobs_cleanup()
        except DatabaseUnavailable:
            pass  # база недоступна: уборка подождёт до следующего круга
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
    return DB.one("appointments.by_id", (room.appt_id,))


PROTO_LOCK = threading.Lock()


def load_protocol_json(appt):
    try:
        return json.loads(appt.get("protocol") or "{}")
    except ValueError:
        return {}


def set_speech(appt, **kw):
    """Состояние расшифровки в протоколе приёма: consent (согласие пациента), active (идёт запись)."""
    with PROTO_LOCK:
        row = DB.one("appointments.protocol", (appt["id"],)) or {}
        proto = load_protocol_json(row)
        sp = dict(proto.get("speech") or {})
        sp.update(kw)
        sp["ts"] = now()
        proto["speech"] = sp
        DB.run("appointments.set_protocol", (json.dumps(proto, ensure_ascii=False), appt["id"]))
    return sp


def save_card(appt, card):
    vals = {k: clean_text(card.get(k), 2000) for k in CARD_FIELDS if k in card}
    pid = appt["patient_id"]
    if pid is None:
        name = vals.get("full_name") or "Без имени"
        pid = DB.run("patients.add", (name, now()))
        DB.run("appointments.set_patient", (pid, appt["id"]))
        # снимки и измерения, сделанные до карточки, иначе остались бы без пациента и без проверки доступа
        DB.run("photos.attach_patient", (pid, appt["id"]))
        DB.run("measurements.attach_patient", (pid, appt["id"]))
    if vals:
        if "full_name" in vals and not vals["full_name"]:
            vals.pop("full_name")
        if vals:
            DB.run("patients.update_card", (pid, vals))
    return pid


def patient_card(pid):
    if pid is None:
        return {k: "" for k in CARD_FIELDS}
    p = DB.one("patients.by_id", (pid,)) or {}
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


