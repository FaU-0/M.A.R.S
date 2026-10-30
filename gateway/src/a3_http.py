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


SESSION_SEEN = {}  # хэш сессии -> (логин, когда проверена по базе)
SESSION_RECHECK = 60
SESSION_GRACE = 600


class HttpBase(BaseHTTPRequestHandler):
    """Вывод, ввод, вход врача, маршрутизация, врачи и приёмы. Комнаты и клинические разделы — в RoomsMixin и ClinicalMixin."""
    protocol_version = "HTTP/1.1"
    who = "unknown"  # кто действует в комнате (doctor:<логин> или patient), выставляет room_access
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
        try:  # тело запроса не прочитано: соединение закрывается, иначе остаток тела склеится со следующим запросом
            if not getattr(self, "_body_read", True) and int(self.headers.get("Content-Length") or 0) > 0:
                self.close_connection = True
        except ValueError:
            self.close_connection = True
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
            row = DB.one("sessions.doctor", (sid_hash(sid), now()))
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
        except DatabaseUnavailable:
            try:
                if not self._body_read and int(self.headers.get("Content-Length") or 0) > 0:
                    self.close_connection = True
            except ValueError:
                self.close_connection = True
            self.send_json(503, {"error": "service_unavailable", "service": "database", "degraded": True,
                                 "message": "База данных временно недоступна"}, {"Retry-After": "5"})
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
        m = re.fullmatch(r"/rooms/([A-Za-z0-9]{4,12})/(events|join|frames|protocol|measurements|ice-config|info|render-conclusion|export)", path)
        if m:
            code, sub = m.group(1), m.group(2)
            key = (method, sub)
            table = {
                ("POST", "join"): self.h_join, ("POST", "events"): self.h_events_post,
                ("GET", "events"): lambda c: self.h_events_get(c, qs),
                ("POST", "frames"): self.h_frames, ("GET", "protocol"): self.h_protocol_get,
                ("POST", "protocol"): self.h_protocol_post, ("POST", "measurements"): self.h_meas_post,
                ("GET", "ice-config"): self.h_ice, ("GET", "info"): self.h_info,
                ("POST", "render-conclusion"): self.h_render_conclusion, ("POST", "export"): self.h_export,
            }
            if key in table:
                return table[key](code)
        m = re.fullmatch(r"/rooms/([A-Za-z0-9]{4,12})/analyze", path)
        if method == "POST" and m:
            return self.h_analyze(m.group(1))
        m = re.fullmatch(r"/rooms/([A-Za-z0-9]{4,12})/audio", path)
        if method == "POST" and m:
            return self.h_audio(m.group(1))
        m = re.fullmatch(r"/rooms/([A-Za-z0-9]{4,12})/transcript(?:/(status|original|deidentify|edit))?", path)
        if m:
            sub = (method, m.group(2) or "")
            if sub in (("GET", ""), ("GET", "status"), ("GET", "original")):
                return self.h_transcript(m.group(1), m.group(2) or "")
            if sub == ("POST", "deidentify"):
                return self.h_transcript_redo(m.group(1))
            if sub == ("POST", "edit"):
                return self.h_transcript_edit(m.group(1))
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
        row = DB.one("doctors.by_login", (login,))
        salt = row["salt"] if row else b"\x00" * 16
        calc = hash_pw(pw, salt)
        if not row or not hmac.compare_digest(calc, row["pw"]):
            raise ApiError(401, "bad_credentials", "Неверный логин или пароль")
        sid = secrets.token_urlsafe(32)
        DB.run("sessions.add", (sid_hash(sid), row["id"], now() + SESSION_TTL))
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
            SESSION_SEEN.pop(sid_hash(sid), None)
            DB.run("sessions.delete", (sid_hash(sid),))
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
        if DB.one("doctors.id_by_login", (login,)):
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
        self.send_json(200, {"items": DB.q("audit.recent", (lim,))})

    # --- приёмы ---
    def create_appt(self, doc, patient_id, name, email, when, birth=""):
        if patient_id:
            if not DB.one("patients.exists", (patient_id,)):
                raise ApiError(404, "patient_not_found", "Пациент не найден")
            self.need_patient(doc, patient_id)  # иначе записью на приём можно получить доступ к чужой истории
            if email:
                DB.run("patients.set_email", (email, patient_id))
        elif name:
            patient_id = DB.run("patients.add_full",
                                (name, birth, email, now()))
        code = new_code()
        did = doc["id"] if doc["id"] else None
        aid = DB.run("appointments.add",
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
        row = DB.one("appointments.full_by_id", (aid,))
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
        rows = DB.q("appointments.between", (t_from, t_to, None if doc["is_admin"] else doc["id"]))
        self.send_json(200, {"appointments": [self.appt_dict(r) for r in rows]})

    def h_patients(self, qs):
        doc = self.doctor()
        q = "%" + clean_text((qs.get("q") or [""])[0], 100).replace("%", "") + "%"
        rows = DB.q("patients.search", (q,)) if doc["is_admin"] else DB.q("patients.search_for_doctor", (doc["id"], q))
        audit(self.actor(doc), "patient_search", "", "found=%d" % len(rows))  # сам запрос (фамилию) не пишем
        self.send_json(200, {"patients": rows})

    def need_appointment(self, doc, aid):
        """Данные приёма без пациента (снимки до создания карточки): только врач этого приёма или администратор."""
        appt = DB.one("appointments.by_id", (aid,))
        if doc["is_admin"] or (appt and appt["doctor_id"] and appt["doctor_id"] == doc["id"]):
            return
        audit(self.actor(doc), "patient_access_denied", "", "appointment=%s" % aid)
        raise ApiError(404, "not_found", "Приём не найден")

    def need_patient(self, doc, pid):
        """Врач видит пациента только через свой приём; администратор видит всех.
        Чужой пациент неотличим от несуществующего (404), попытка пишется в журнал аудита."""
        if doc["is_admin"] or DB.one("patients.seen_by_doctor", (pid, doc["id"])):
            return
        audit(self.actor(doc), "patient_access_denied", "", "patient=%s" % pid)
        raise ApiError(404, "patient_not_found", "Пациент не найден")

    def h_patient_sub(self, pid, sub):
        doc = self.doctor()
        self.need_patient(doc, pid)
        if sub == "photos":
            rows = DB.q("photos.of_patient", (pid,))
            audit(self.actor(doc), "patient_photos_viewed", "", "patient=%d" % pid)
            return self.send_json(200, {"photos": rows})
        audit(self.actor(doc), "patient_measurements_viewed", "", "patient=%d" % pid)
        rows = DB.q("measurements.of_patient", (pid,))
        for r in rows:
            try:
                r["data"] = json.loads(r.pop("body"))
            except ValueError:
                r["data"] = {}
        self.send_json(200, {"measurements": rows})

    def h_photo(self, pid):
        doc = self.doctor()
        row = DB.one("photos.by_id", (pid,))
        if not row:
            raise ApiError(404, "not_found", "Снимок не найден")
        try:
            if row["patient_id"] is not None:
                self.need_patient(doc, row["patient_id"])
            else:
                self.need_appointment(doc, row["appt_id"])
        except ApiError:
            raise ApiError(404, "not_found", "Снимок не найден")
        p = DATA_DIR / row["path"]
        if not p.is_file():
            raise ApiError(404, "not_found", "Файл снимка отсутствует")
        audit(self.actor(doc), "photo_viewed", "", "id=%d" % pid)
        self.reply(200, p.read_bytes(), "image/jpeg")

