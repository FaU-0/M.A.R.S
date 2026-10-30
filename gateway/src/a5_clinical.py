class ClinicalMixin:
    """Протокол, выгрузки, заключение, анализ лица, расшифровка речи, измерения. Часть Handler."""

    # --- протокол ---
    def meta(self, appt):
        d = DB.one("doctors.name", (appt["doctor_id"],)) if appt["doctor_id"] else None
        p = DB.one("patients.name", (appt["patient_id"],)) if appt["patient_id"] else None
        return {"id": appt["id"], "code": appt["code"], "status": appt["status"], "scheduled": appt["scheduled"],
                "doctor": (d or {}).get("name", ""), "patient_name": (p or {}).get("full_name", ""),
                "patient_id": appt["patient_id"], "center": CENTER_NAME}

    # выгрузки, которые браузер сохраняет файлом: врач — данные приёма, пациент — только своё заключение
    EXPORT_KINDS = {"face_report_json": "doctor", "face_report_fhir": "doctor", "repeatability_csv": "doctor",
                    "compare_image": "doctor", "consultation_json": "doctor", "conclusion_json": "any"}

    def h_export(self, code):
        """Записывает в журнал аудита выгрузку данных приёма. Браузер сохраняет файл только после ответа 200:
        выгрузка без записи в журнале невозможна (152-ФЗ: учёт передачи персональных данных)."""
        room, role = self.need_room(code)
        b = self.read_json(4096)
        kind = b.get("kind")
        if kind not in self.EXPORT_KINDS:
            raise ApiError(400, "bad_request", "Неизвестный вид выгрузки")
        if self.EXPORT_KINDS[kind] == "doctor" and role != "doctor":
            raise ApiError(403, "forbidden", "Только врач")
        audit(self.who, "export_" + kind, room.code)
        self.send_json(200, {"ok": True})

    def h_render_conclusion(self, code):
        """Печатная форма заключения. Строит сервис reports; если он недоступен, отвечаем 503,
        и браузер печатает сам (запасной путь)."""
        room, role = self.need_room(code)
        b = self.read_json(1024 * 1024)
        audit(self.who, "conclusion_printed", room.code)
        payload = {"center": clean_text(b.get("center"), 200), "doctor": clean_text(b.get("doctor"), 200),
                   "patient": clean_text(b.get("patient"), 200), "date": clean_text(b.get("date"), 40),
                   "conclusion": clean_conclusion(b.get("conclusion") or {}),
                   "lang": "en" if b.get("lang") == "en" else "ru"}
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
        DB.run("jobs.add_once",
               (appt["id"], kind, key, t, t, json.dumps(b["input"], ensure_ascii=False)))
        job = DB.one("jobs.by_key", (appt["id"], key))
        JOB_EVTS["vision"].set()
        self.send_json(202, job_view(job))

    # --- расшифровка речи (сервис speech, через очередь заданий) ---
    def h_audio(self, code):
        """Кусок записи своего микрофона (около 10 с). Принимается только при согласии пациента
        и включённой расшифровке. Кусок сохраняется на диск сразу, даже если speech недоступен."""
        room, role = self.need_room(code)
        if not room.bucket(role, "main").take(1):
            raise ApiError(429, "rate_limited", "Слишком много кусков записи")
        appt = appt_by_room(room)
        sp = load_protocol_json(appt).get("speech") or {}
        if room.ended or appt["status"] == "completed":
            raise ApiError(409, "appointment_ended", "Приём завершён: запись речи не принимается")
        if not (sp.get("consent") and sp.get("active")):
            raise ApiError(403, "no_speech_consent", "Нет согласия пациента на расшифровку речи")
        mime = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if mime not in AUDIO_MIME:
            raise ApiError(415, "bad_audio", "Неподдерживаемый формат аудио")
        key = clean_text(self.headers.get("X-Chunk-Key"), 64)
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", key or ""):
            raise ApiError(400, "bad_key", "Нужен ключ куска (8-64 символа)")
        try:
            seq = max(0, int(self.headers.get("X-Seq") or 0))
            dur = max(0, min(60000, int(self.headers.get("X-Duration-Ms") or 0)))
        except ValueError:
            raise ApiError(400, "bad_request", "Некорректный номер или длительность куска")
        raw = self.read_raw(MAX_AUDIO)
        if len(raw) < 100:
            raise ApiError(400, "bad_audio", "Пустой кусок записи")
        need_room_for(appt["id"], "audio", len(raw))
        key = role[0] + "-" + key  # ключи врача и пациента не пересекаются
        dup = DB.one("jobs.by_key", (appt["id"], key))
        if dup:
            return self.send_json(202, job_view(dup))
        # Время начала куска по часам сервера (часы двух устройств могут расходиться). Сеанс записи
        # привязывается к часам сервера по первому куску, дальше идёт смещение от браузера:
        # так куски, пришедшие пачкой после обрыва связи, встают на свои места.
        try:
            off = int(self.headers.get("X-Offset-Ms")) if self.headers.get("X-Offset-Ms") else None
        except ValueError:
            off = None
        now_ms = int(time.time() * 1000)
        anchor = None
        if off is not None and off >= 0:
            sess = key.rsplit("-", 1)[0]
            first = DB.one("jobs.first_input_like",
                           (appt["id"], sess + "-%"))
            anchor = (json.loads(first["input"]).get("anchor") if first else None) or now_ms - dur - off
            t_ms = anchor + off
        else:
            t_ms = now_ms - dur
        rel = "audio/%d_%s_%s%s" % (appt["id"], role, secrets.token_hex(6), AUDIO_MIME[mime])
        (DATA_DIR / rel).write_bytes(raw)
        inp = {"audio": rel, "mime": mime, "role": role, "seq": seq, "t": t_ms, "dur": dur, "anchor": anchor}
        t = now()
        DB.run("jobs.add_once",
               (appt["id"], "transcribe", key, t, t, json.dumps(inp, ensure_ascii=False)))
        job = DB.one("jobs.by_key", (appt["id"], key))
        if json.loads(job["input"]).get("audio") != rel:
            drop_audio(inp)  # одновременный повтор: этот файл не нужен
        JOB_EVTS["speech"].set()
        self.send_json(202, job_view(job))

    def h_transcript(self, code, what):
        """Только врач. status: сколько кусков расшифровано (без текста).
        Без суффикса: обезличенная расшифровка (метки вместо личных данных, без оригиналов).
        original: исходная расшифровка; каждый просмотр пишется в журнал аудита."""
        room, role = self.need_room(code)
        if role != "doctor":
            raise ApiError(403, "forbidden", "Только врач")
        appt = appt_by_room(room)
        rows = DB.q("jobs.transcribe_counts", (appt["id"],))
        cnt = {r["status"]: r["n"] for r in rows}
        st = {"chunks": sum(cnt.values()), "done": cnt.get("done", 0), "queued": cnt.get("queued", 0), "error": cnt.get("error", 0),
              "cancelled": cnt.get("cancelled", 0),
              "speech": (load_protocol_json(appt).get("speech") or {}), "service_up": None}
        if service_enabled("speech"):
            st["service_up"] = (services_status()["services"].get("speech") or {}).get("up")
        if what == "status":
            return self.send_json(200, st)
        if what == "original":
            segs = DB.q("speech.segments", (appt["id"],))
            audit(self.who, "transcript_original_viewed", room.code, "segments=%d" % len(segs))
            return self.send_json(200, {"status": st, "segments": merge_speakers(segs)})
        view = transcript_view(appt["id"])
        if view.get("segments"):  # вкладка опрашивает, пока идёт обезличивание; пишем только показ текста
            audit(self.who, "transcript_viewed", room.code, "segments=%d" % len(view["segments"]))
        self.send_json(200, {"status": st, "deid": view})

    def h_transcript_redo(self, code):
        room, role = self.need_room(code)
        self.read_json(1024, allow_empty=True)  # тело надо прочитать: иначе оно склеится со следующим запросом в том же соединении
        if role != "doctor":
            raise ApiError(403, "forbidden", "Только врач")
        appt = appt_by_room(room)
        if not DB.one("speech.any_segment", (appt["id"],)) and \
                not DB.one("jobs.transcribe_pending", (appt["id"],)):
            raise ApiError(400, "no_transcript", "Расшифровки этого приёма нет")
        if not service_enabled("assistant"):
            return self.send_json(503, {"error": "service_unavailable", "service": "assistant", "degraded": True,
                                        "message": "Сервис обезличивания не подключён"}, {"Retry-After": "30"})
        enqueue_deid(appt["id"])
        audit(self.who, "transcript_deidentify_requested", room.code)
        self.send_json(202, {"deid": transcript_view(appt["id"])})

    def h_transcript_edit(self, code):
        """Правки врача: undo (вернуть оригинал под меткой), add (скрыть фрагмент), confirm / unconfirm (для отчёта).
        Любая правка текста снимает подтверждение."""
        room, role = self.need_room(code)
        if role != "doctor":
            raise ApiError(403, "forbidden", "Только врач")
        appt = appt_by_room(room)
        b = self.read_json(65536)
        op = clean_text(b.get("op"), 20)
        with PROTO_LOCK:
            row = DB.one("transcripts.get", (appt["id"],))
            if not row or row["status"] != "done" or not row["deid"]:
                raise ApiError(409, "not_ready", "Обезличенной расшифровки ещё нет")
            if op in ("confirm", "unconfirm"):
                DB.run("transcripts.set_confirmed", (1 if op == "confirm" else 0, now(), appt["id"]))
                audit(self.who, "transcript_" + op + "ed", room.code)
                return self.send_json(200, {"deid": transcript_view(appt["id"])})
            if not row["mapping"]:
                raise ApiError(410, "original_expired", "Срок хранения исходника истёк: правки недоступны")
            mp = json.loads(row["mapping"])
            if op == "undo":
                lab = clean_text(b.get("label"), 60)
                n = 0
                for g in mp["segments"]:
                    np_ = []
                    for x in g["parts"]:
                        if isinstance(x, dict) and x["l"] == lab:
                            np_.append(x["o"])
                            n += 1
                        else:
                            np_.append(x)
                    g["parts"] = np_
                mp["replacements"] = [r for r in mp["replacements"] if r.get("label") != lab]
                if not n:
                    raise ApiError(404, "no_label", "Такой замены нет")
                detail = "%s x%d" % (lab, n)
            elif op == "add":
                frag = clean_text(b.get("text"), 200).strip()
                typ = clean_text(b.get("type"), 20) or "OTHER"
                if len(frag) < 2:
                    raise ApiError(400, "bad_text", "Выделите фрагмент текста")
                kinds = {"NAME": "ИМЯ", "ORG": "ОРГАНИЗАЦИЯ", "CITY": "МЕСТО", "SCHOOL": "УЧЕБНОЕ_ЗАВЕДЕНИЕ", "OTHER": "СКРЫТО"}
                base = kinds.get(typ, "СКРЫТО")
                used = {r.get("label") for r in mp["replacements"]}
                k = 1
                while "[%s_%d]" % (base, k) in used:
                    k += 1
                lab = "[%s_%d]" % (base, k)
                rx = re.compile(re.escape(frag), re.I)
                n = 0
                for g in mp["segments"]:
                    np_ = []
                    for x in g["parts"]:
                        if not isinstance(x, str):
                            np_.append(x)
                            continue
                        pos = 0
                        for m in rx.finditer(x):
                            np_ += [x[pos:m.start()], {"l": lab, "o": m.group()}]
                            pos = m.end()
                            n += 1
                        np_.append(x[pos:])
                    g["parts"] = [y for y in np_ if y != ""]
                if not n:
                    raise ApiError(404, "not_found", "Фрагмент не найден в обезличенном тексте")
                mp["replacements"].append({"label": lab, "type": typ, "source": "doctor", "originals": [frag]})
                detail = "%s x%d" % (lab, n)
            else:
                raise ApiError(400, "bad_op", "Неизвестная правка")
            deid = [{"role": g["role"], "t": g["t"], "text": parts_text(g["parts"])} for g in mp["segments"]]
            DB.run("transcripts.edit",
                   (json.dumps(deid, ensure_ascii=False), json.dumps(mp, ensure_ascii=False), now(), appt["id"]))
        audit(self.who, "transcript_replacement_" + ("undone" if op == "undo" else "added"), room.code, detail)
        self.send_json(200, {"deid": transcript_view(appt["id"])})

    def h_job_get(self, code, jid):
        room, role = self.need_room(code)
        job = DB.one("jobs.in_appointment", (jid, appt_by_room(room)["id"]))
        if not job:
            raise ApiError(404, "not_found", "Задание не найдено")
        self.send_json(200, job_view(job))

    def h_protocol_get(self, code):
        room, role = self.need_room(code)
        appt = appt_by_room(room)
        proto = load_protocol_json(appt)
        face = proto.get("face") or {}
        if role == "patient":
            return self.send_json(200, {"appointment": self.meta(appt), "status": appt["status"], "face": face,
                                        "speech": proto.get("speech") or {}})
        note = DB.one("notes.get", (appt["id"],))
        concl = DB.one("conclusions.get", (appt["id"],))
        audit(self.who, "protocol_viewed", room.code)  # карточка, заметки, заключение
        self.send_json(200, {"appointment": self.meta(appt), "status": appt["status"], "location": proto.get("location") or {},
                             "face": face, "speech": proto.get("speech") or {}, "card": patient_card(appt["patient_id"]),
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
        with PROTO_LOCK:  # состояние расшифровки меняет пациент: берём свежее, не затираем
            fresh = load_protocol_json(appt_by_room(room))
            if "speech" in fresh:
                proto["speech"] = fresh["speech"]
            DB.run("appointments.set_protocol", (json.dumps(proto, ensure_ascii=False), appt["id"]))
        if "notes" in b:
            DB.run("notes.save",
                   (appt["id"], clean_text(b["notes"], 20000), now()))
        if concl is not None:
            DB.run("conclusions.save",
                   (appt["id"], json.dumps(concl, ensure_ascii=False), now()))
            audit(self.who, "conclusion_saved", room.code)
        self.send_json(200, {"status": "saved"})

    def verified_measurement(self, appt, kind, b):
        """Если метрики посчитал vision (job_id задания этого приёма), сохраняются числа из задания, а не из браузера:
        verified=true. Иначе это расчёт на устройстве, verified=false."""
        jid = b.get("job_id")
        job = DB.one("jobs.in_appointment", (int(jid), appt["id"])) if isinstance(jid, int) else None
        if not job or job["status"] != "done" or job["kind"] != kind or not job["result"]:
            return dict(b, verified=False)
        res = json.loads(job["result"])
        if res.get("pose_ok") is not True:
            raise ApiError(400, "pose_out_of_tolerance", "Поза вне допуска: значения не сохраняются")
        out = dict(b)
        out.update(res)
        return dict(out, verified=True, job_id=job["id"], submitted_by=b["submitted_by"])

    def h_meas_post(self, code):
        room, role = self.need_room(code)
        if not room.bucket(role, "main").take(2):
            raise ApiError(429, "rate_limited", "Слишком много измерений")
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
        need_room_for(appt["id"], "measurement")
        b = dict(b, submitted_by=self.who)  # кто прислал: врач или устройство пациента
        if kind in ("symmetry", "mobility", "compare"):
            b = self.verified_measurement(appt, kind, b)
        if kind == "report" and isinstance(b.get("transcript"), dict):
            # текст расшифровки в отчёт не копируем: он живёт в transcripts со своим сроком хранения и правками врача
            b = dict(b, transcript={"included": True, "segments": len(b["transcript"].get("segments") or [])})
        DB.run("measurements.add",
               (appt["id"], appt["patient_id"], kind, now(), json.dumps(b, ensure_ascii=False)))
        audit(self.who, "measurements_saved", room.code, kind)
        self.send_json(200, {"status": "stored", "kind": kind})


class Handler(RoomsMixin, ClinicalMixin, HttpBase):
    """Обработчик HTTP gateway: методы собраны из HttpBase и примесей."""


