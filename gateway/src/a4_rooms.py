class RoomsMixin:
    """Комнаты, события звонка, файлы и снимки. Часть Handler."""

    # --- комнаты ---
    def h_rooms_create(self):
        doc = self.doctor()
        ip_limit(self.client_address[0], "rooms", 30, 0.2)
        b = self.read_json(65536, allow_empty=True)
        aid = b.get("appointment_id")
        if aid:
            appt = DB.one("appointments.by_id", (int(aid),)) if str(aid).isdigit() else None
            if not appt:
                raise ApiError(404, "appointment_not_found", "Приём не найден")
            if appt["status"] == "completed":
                raise ApiError(410, "room_closed", "Консультация завершена")
            if not doc["is_admin"] and appt["doctor_id"] != doc["id"]:
                # неназначенный приём раздаёт администратор: иначе любой врач стал бы «своим» и получил историю пациента
                raise ApiError(403, "forbidden", "Приём назначен другому врачу" if appt["doctor_id"] else "Приём не назначен: обратитесь к администратору")
        else:
            aid, code, _ = self.create_appt(doc, None, "", "", now())
            appt = DB.one("appointments.by_id", (aid,))
            audit(self.actor(doc), "appointment_created", code)
        room = get_room(appt["code"], create=True)
        with COND:
            if room.closed:
                raise ApiError(410, "room_closed", "Консультация завершена")
            token = next((t for t, login in room.doc_tokens.items() if login == doc["login"]), None)
            if not token:
                token = "doc_" + secrets.token_urlsafe(24)
                room.doc_tokens[token] = doc["login"]
            room.last = time.time()
        if not appt["doctor_id"] and doc["id"]:
            DB.run("appointments.set_doctor", (doc["id"], appt["id"]))
        self.send_json(201, {"room": room.code, "token": token, "appointment_id": appt["id"]})

    def h_info(self, code):
        ip_limit(self.client_address[0], "info", 30, 0.5)
        code = code.upper()
        appt = DB.one("appointments.with_doctor_by_code", (code,))
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

    def need_room(self, code, sub_ok_closed=False, fast=False):
        room = get_room(code)
        with COND:
            closed = room.closed
        if closed:
            raise ApiError(410, "room_closed", "Консультация прервана")
        return room, self.room_access(room, fast)

    def doctor_login(self, fast=False):
        """Логин врача по сессии. Сессия, проверенная по базе меньше SESSION_RECHECK назад, в базу не ходит:
        сигнализация звонка не зависит от database. Если база недоступна, действует последняя удачная
        проверка не старше SESSION_GRACE. Выход врача убирает запись сразу."""
        sid = self.cookie("mars_sid")
        key = sid_hash(sid) if sid else None
        hit = SESSION_SEEN.get(key) if key else None
        if hit and time.time() - hit[1] < (SESSION_GRACE if fast else SESSION_RECHECK):
            return hit[0]
        try:
            doc = self.doctor(required=False)
        except DatabaseUnavailable:
            if hit and time.time() - hit[1] < SESSION_GRACE:
                return hit[0]
            raise
        if not doc:
            return None
        if key:
            if len(SESSION_SEEN) > 5000:
                SESSION_SEEN.clear()
            SESSION_SEEN[key] = (doc["login"], time.time())
        return doc["login"]

    def room_access(self, room, fast=False):
        """Роль в комнате. Ссылка врача действует только вместе с его сессией: после выхода врача
        или отключения его учётной записи она перестаёт работать. self.who - кто действует, для аудита."""
        role, login = room_role(room, self.headers)
        if not role:
            raise ApiError(401, "unauthorized", "Нет доступа к комнате")
        if role == "doctor":
            if self.doctor_login(fast) != login:
                raise ApiError(401, "unauthorized", "Войдите заново: ссылка врача действует только с его сессией")
            self.who = "doctor:%s" % login
        else:
            self.who = "patient"
        return role

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
        role = self.room_access(room, fast=True)  # опрос событий звонка: без запроса к базе
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
        room, role = self.need_room(code, fast=True)  # события звонка: сессия врача без запроса к базе
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
        # сигнализация звонка и события лица в базу не ходят: идущий звонок переживает отказ database
        appt = None if typ in ("offer", "answer", "ice", "presence", "face", "measure") else appt_by_room(room)
        who = "doctor" if role == "doctor" else "patient"
        if typ in ("offer", "answer", "ice"):
            if not isinstance(data, dict):
                raise ApiError(400, "bad_request", "Некорректные данные сигналинга")
            to, out = other, data
        elif typ == "chat":
            text = clean_text((data or {}).get("text") if isinstance(data, dict) else "", 4000)
            if not text:
                raise ApiError(400, "empty", "Пустое сообщение")
            mid = DB.run("messages.add",
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
            if typ == "presence" and role == "patient" and data.get("state") == "waiting":
                try:
                    appt = appt_by_room(room)
                    if appt["status"] in ("planned", "waiting"):
                        DB.run("appointments.set_waiting", (appt["id"],))
                except DatabaseUnavailable:
                    pass  # отметка «пациент ждёт» не важнее сигнализации
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
        DB.run("appointments.set_completed", (now(), appt["id"]))
        finish_speech(appt)
        audit(self.who, "emergency_end", room.code)
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
            DB.run("appointments.set_started", (now(), appt["id"]))
            audit(self.who, "consultation_start", room.code)
            return other, data
        if act == "end":
            if role != "doctor":
                raise ApiError(403, "forbidden", "Только врач")
            DB.run("appointments.set_completed", (now(), appt["id"]))
            finish_speech(appt)
            room.ended = True
            audit(self.who, "consultation_end", room.code)
            return "all", data
        if act in ("speech_request", "speech_stop", "speech_consent"):
            return self.speech_control(room, role, appt, act, data)
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
            audit(self.who, "patient_seat_reset", room.code)
            return "doctor", {"action": "reset_patient"}
        return other, data

    def speech_control(self, room, role, appt, act, data):
        """Запрос врача -> согласие пациента -> запись у обеих сторон. Пациент может отозвать в любой момент.
        После завершения приёма запись не включается; при отзыве согласия недорасшифрованные куски удаляются."""
        starting = act == "speech_request" or (act == "speech_consent" and data.get("granted") is True)
        if starting and (room.ended or appt["status"] == "completed"):
            raise ApiError(409, "appointment_ended", "Приём завершён: расшифровку включить нельзя")
        if act == "speech_request":
            if role != "doctor":
                raise ApiError(403, "forbidden", "Только врач")
            return "patient", {"action": act}
        if act == "speech_stop":
            if role != "doctor":
                raise ApiError(403, "forbidden", "Только врач")
            sp = set_speech(appt, active=False)
            audit(self.who, "speech_stopped", room.code)
            return "all", {"action": act, "speech": sp}
        if role != "patient":
            raise ApiError(403, "forbidden", "Только пациент")
        g = data.get("granted") is True
        sp = set_speech(appt, consent=g, active=g)
        dropped = 0 if g else cancel_queued_speech(appt["id"], "consent_revoked")
        audit("patient", "consent_speech", room.code, "granted" if g else "revoked, dropped=%d" % dropped)
        return "all", {"action": act, "granted": g, "speech": sp}

    def build_document(self, room, appt):
        row = DB.one("conclusions.get", (appt["id"],))
        if not row:
            raise ApiError(400, "no_conclusion", "Сначала сохраните заключение")
        concl = json.loads(row["body"])
        doc = DB.one("doctors.name", (appt["doctor_id"],)) if appt["doctor_id"] else None
        pname = (DB.one("patients.name", (appt["patient_id"],)) or {}).get("full_name", "") if appt["patient_id"] else ""
        payload = {"center": CENTER_NAME, "doctor": (doc or {}).get("name", ""), "patient": pname,
                   "date": datetime.now().strftime("%Y-%m-%d"), "conclusion": concl}
        mid = DB.run("messages.add",
                     (appt["id"], now(), "doctor", "document", json.dumps(payload, ensure_ascii=False)))
        audit(self.who, "conclusion_sent", room.code)
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
        need_room_for(appt["id"], "file", len(raw))
        fid = DB.run("files.add",
                     (appt["id"], now(), who, name, FILE_TYPES[ext], len(raw)))
        rel = "files/%d.dat" % fid
        (DATA_DIR / rel).write_bytes(raw)
        DB.run("files.set_path", (rel, fid))
        meta = {"id": fid, "name": name, "mime": FILE_TYPES[ext], "size": len(raw)}
        DB.run("messages.add",
               (appt["id"], now(), who, "file", json.dumps(meta, ensure_ascii=False)))
        audit(self.who, "file_upload", room.code, "id=%d size=%d" % (fid, len(raw)))
        return meta

    def h_file_get(self, code, fid):
        room, role = self.need_room(code)
        row = DB.one("files.in_appointment", (fid, room.appt_id))
        if not row or not (DATA_DIR / row["path"]).is_file():
            raise ApiError(404, "not_found", "Файл не найден")
        audit(self.who, "file_downloaded", room.code, "id=%d" % fid)
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
        need_room_for(appt["id"], "photo", len(raw))
        ts = now()
        pid = DB.run("photos.add",
                     (appt["id"], appt["patient_id"], pose, ts, len(raw)))
        rel = "photos/%d.jpg" % pid
        (DATA_DIR / rel).write_bytes(raw)
        DB.run("photos.set_path", (rel, pid))
        audit(self.who, "photo_saved", room.code, "id=%d pose=%s" % (pid, pose))
        with COND:
            if not room.closed:
                add_event(room, "face", "server", "doctor", {"kind": "photo", "id": pid, "pose": pose, "ts": ts})
        self.send_json(200, {"timestamp": ts, "frame_size": len(raw), "id": pid, "pose": pose, "status": "stored"})

