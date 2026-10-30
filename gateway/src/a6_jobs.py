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


# Очередь заданий для вторичных сервисов: задания переживают перезапуск, повторы с нарастающей паузой.
# У каждого сервиса свой поток: зависший speech не задерживает расчёты vision.
VISION_KINDS = ("symmetry", "mobility", "compare", "calibrate")
JOB_SERVICES = {
    "vision": {"kinds": VISION_KINDS, "timeout": 10.0, "max_attempts": 20},
    # куски речи ждут, пока speech не вернётся (в пределах JOB_KEEP); кусок 10 с на процессоре идёт несколько секунд
    "speech": {"kinds": ("transcribe",), "timeout": 120.0, "max_attempts": None},
    # обезличивание всей расшифровки: окна текста по очереди через Ollama, на процессоре это минуты
    "assistant": {"kinds": ("deidentify",), "timeout": 1800.0, "max_attempts": None},
}
JOB_EVTS = {n: threading.Event() for n in JOB_SERVICES}
JOB_KEEP = 86400  # входные данные (точки лица, куски аудио) хранятся не дольше суток
TRANSCRIPT_DAYS = int(os.environ.get("TRANSCRIPT_DAYS", "30"))  # срок хранения исходной расшифровки
# Лимиты на один приём и свободное место: место пациента занимает тот, кто первым вошёл по коду,
# и без лимитов он мог бы заполнить диск файлами, снимками, измерениями и кусками речи.
APPT_FILES_MB = int(os.environ.get("APPT_FILES_MB", "200"))
APPT_PHOTOS = int(os.environ.get("APPT_PHOTOS", "300"))
APPT_MEASUREMENTS = int(os.environ.get("APPT_MEASUREMENTS", "500"))
APPT_QUEUED_AUDIO = int(os.environ.get("APPT_QUEUED_AUDIO", "720"))  # 2 часа речи по 10 с
MIN_FREE_MB = int(os.environ.get("MIN_FREE_MB", "500"))


def need_room_for(appt_id, kind, size=0):
    """413, если превышен лимит приёма; 507, если на диске меньше MIN_FREE_MB."""
    if shutil.disk_usage(str(DATA_DIR)).free - size < MIN_FREE_MB * 1024 * 1024:
        log.warning("мало места на диске: загрузка %s отклонена", kind)
        raise ApiError(507, "disk_full", "На сервере закончилось место. Сообщите администратору")
    if kind == "file":
        u = DB.one("files.appt_usage", (appt_id,))
        over = u["bytes"] + size > APPT_FILES_MB * 1024 * 1024
    elif kind == "photo":
        over = DB.one("photos.appt_count", (appt_id,))["n"] >= APPT_PHOTOS
    elif kind == "measurement":
        over = DB.one("measurements.appt_count", (appt_id,))["n"] >= APPT_MEASUREMENTS
    else:
        over = DB.one("jobs.queued_audio", (appt_id,))["n"] >= APPT_QUEUED_AUDIO
    if over:
        raise ApiError(413, "appointment_quota", "Достигнут лимит загрузок для этого приёма")


AUDIO_MIME = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/mpeg": ".mp3", "audio/wav": ".wav"}
MAX_AUDIO = 2 * 1024 * 1024


def service_enabled(name):
    return _ServiceClient is not None and _svc_config is not None and name in _svc_config.monitored_services()


def vision_enabled():
    return service_enabled("vision")


def job_service(kind):
    return next(n for n, c in JOB_SERVICES.items() if kind in c["kinds"])


def job_view(job):
    out = {"id": job["id"], "kind": job["kind"], "status": job["status"], "attempts": job["attempts"],
           "created": job["created"], "updated": job["updated"]}
    if job["status"] == "done" and job["result"] and job["kind"] not in ("transcribe", "deidentify"):
        out["result"] = json.loads(job["result"])
    if job["error"]:
        out["error"] = json.loads(job["error"])
    if job["status"] == "queued" and job["attempts"]:
        out["retry_at"] = job["next_try"]
    return out


def drop_audio(inp):
    rel = (inp or {}).get("audio")
    if rel:
        try:
            (DATA_DIR / rel).unlink()
        except OSError:
            pass


def job_payload(job, inp):
    """Тело запроса к сервису. Кусок речи читается с диска только при отправке,
    расшифровка для обезличивания собирается в момент отправки (все куски уже расшифрованы)."""
    if job["kind"] == "deidentify":
        return deid_payload(job["appt_id"])
    if job["kind"] != "transcribe":
        return inp
    path = DATA_DIR / inp["audio"]
    if not path.is_file():
        return None
    return {"audio_b64": base64.b64encode(path.read_bytes()).decode("ascii"), "mime": inp.get("mime", "")}


def job_done(job, inp, data):
    """Итог задания. Расшифровка ложится в speech_segments, аудио удаляется сразу;
    обезличенный текст и соответствие замен ложатся в transcripts раздельно."""
    if job["kind"] == "deidentify":
        return deid_done(job["appt_id"], data)
    if job["kind"] != "transcribe":
        return json.dumps(data, ensure_ascii=False)
    t = now()
    for sg in data.get("segments") or []:
        text = clean_text(sg.get("text"), 4000)
        if text:
            DB.run("speech.add_segment",
                   (job["appt_id"], inp["role"], inp["t"], inp.get("seq", 0), float(sg.get("start") or 0), float(sg.get("end") or 0), text, t))
    drop_audio(inp)
    return json.dumps({"segments": len(data.get("segments") or []), "duration": data.get("duration"),
                       "elapsed": data.get("elapsed"), "model": data.get("model")}, ensure_ascii=False)


def deid_payload(appt_id):
    appt = DB.one("appointments.by_id", (appt_id,))
    segs = DB.q("speech.segments", (appt_id,))
    card = patient_card(appt["patient_id"]) if appt and appt["patient_id"] else {}
    loc = load_protocol_json(appt or {}).get("location") or {}
    doc = DB.one("doctors.name", (appt["doctor_id"],)) if appt and appt["doctor_id"] else None
    known = {"patient": {"full_name": card.get("full_name", ""), "birth": card.get("birth", ""), "email": card.get("email", ""),
                         "phone": loc.get("phone", ""), "address": loc.get("address", "")},
             "doctor": {"name": (doc or {}).get("name", "")}}
    return {"segments": merge_speakers(segs), "known": known}


def parts_text(parts):
    return "".join(p if isinstance(p, str) else p["l"] for p in parts)


def deid_done(appt_id, data):
    segs = data.get("segments") or []
    mapping = {"segments": [{"role": g.get("role"), "t": g.get("t"), "parts": g.get("parts") or [g.get("text", "")]} for g in segs],
               "replacements": data.get("replacements") or []}
    deid = [{"role": g["role"], "t": g["t"], "text": parts_text(g["parts"])} for g in mapping["segments"]]
    st = data.get("stats") or {}
    DB.run("transcripts.done",
           (json.dumps(deid, ensure_ascii=False), json.dumps(mapping, ensure_ascii=False), clean_text(st.get("model"), 60),
            json.dumps(st, ensure_ascii=False), now(), appt_id))
    return json.dumps(st, ensure_ascii=False)


def transcript_view(appt_id):
    """Обезличенная расшифровка для врача: текст с метками и список замен без оригиналов."""
    row = DB.one("transcripts.get", (appt_id,))
    if not row:
        return {"status": "none"}
    out = {"status": row["status"], "confirmed": bool(row["confirmed"]), "model": row["model"], "updated": row["updated"],
           "segments": json.loads(row["deid"]) if row["deid"] else [], "editable": bool(row["mapping"])}
    if row["mapping"]:
        out["replacements"] = [{"label": r.get("label"), "type": r.get("type"), "source": r.get("source"), "forms": len(r.get("originals") or [])}
                               for r in json.loads(row["mapping"]).get("replacements") or []]
    if row["error"]:
        out["error"] = json.loads(row["error"])
    if row["status"] == "queued":
        job = DB.one("jobs.latest_deidentify", (appt_id,))
        if job and job["error"]:
            out["waiting"] = json.loads(job["error"]).get("service")  # ждёт, пока сервис оживёт
    return out


def cancel_queued_speech(appt_id, reason):
    """Куски речи, ещё не расшифрованные, удаляются вместе с аудио. Возвращает их число."""
    n, t = 0, now()
    err = json.dumps({"error": reason, "service": "speech", "message": "Отменено: пациент отозвал согласие"}, ensure_ascii=False)
    for job in DB.q("jobs.queued_transcribe", (appt_id,)):
        drop_audio(json.loads(job["input"]))
        DB.run("jobs.cancel", (t, err, job["id"]))
        n += 1
    return n


def finish_speech(appt):
    """Конец приёма (обычный или экстренный): запись выключается, расшифровка уходит на обезличивание."""
    set_speech(appt, active=False)
    if service_enabled("assistant") and DB.one("jobs.any_transcribe", (appt["id"],)):
        enqueue_deid(appt["id"])


def enqueue_deid(appt_id):
    """Обезличивание после «Завершить приём» или по кнопке «Обезличить повторно».
    Прежний обезличенный текст виден, пока не готов новый; подтверждение снимается."""
    t = now()
    DB.run("transcripts.queue", (appt_id, "queued", t, t))
    DB.run("jobs.add",
           (appt_id, "deidentify", "deid-%d-%s" % (t, secrets.token_hex(4)), t, t, "{}"))
    JOB_EVTS["assistant"].set()


def merge_speakers(segs):
    """Реплики обеих сторон по времени. У одного говорящего порядок всегда кусок за куском:
    Whisper иногда ставит последнему сегменту время за краем куска, и он не должен перепрыгнуть следующий кусок."""
    lanes = {}
    for g in segs:  # уже по chunk_t, id: внутри куска сегменты в порядке расшифровки
        lane = lanes.setdefault(g["role"], [])
        t = g["chunk_t"] + int(g["start"] * 1000)
        if lane:
            t = max(t, lane[-1]["t"])
        lane.append({"role": g["role"], "t": t, "dur": int((g["end"] - g["start"]) * 1000), "text": g["text"]})
    return list(heapq.merge(*lanes.values(), key=lambda x: x["t"]))


def run_job(job, svc):
    cfg = JOB_SERVICES[svc]
    cli = _SVC_CLIENTS.get(svc) or _SVC_CLIENTS.setdefault(svc, _ServiceClient(svc, probe_every=5.0))
    t = now()
    inp = json.loads(job["input"])
    if job["kind"] == "deidentify" and DB.one("jobs.transcribe_pending", (job["appt_id"],)):
        DB.run("jobs.postpone", (t + 5, job["id"]))  # ждём, пока расшифруются все куски
        return True
    payload = job_payload(job, inp)
    if payload is None:
        err = json.dumps({"error": "input_gone", "service": svc, "message": "Входные данные задания не найдены"}, ensure_ascii=False)
        DB.run("jobs.fail", (t, err, job["id"]))
        return True
    path = "/v1/" + job["kind"]
    try:
        st, data = cli.call("POST", path, payload, timeout=cfg["timeout"], request_id="job%d" % job["id"])
    except Exception as e:  # noqa: BLE001  (ServiceUnavailable и сбои сети: повторим позже)
        n = job["attempts"] + 1
        err = json.dumps({"error": "service_unavailable", "service": svc, "message": "Сервис временно недоступен"}, ensure_ascii=False)
        if cfg["max_attempts"] and n >= cfg["max_attempts"]:
            DB.run("jobs.give_up", (n, t, err, job["id"]))
        else:
            DB.run("jobs.retry_later",
                   (n, t + min(60, 2 ** min(n, 6)), t, err, job["id"]))
        log.info("задание %d: %s недоступен (%s), попытка %d", job["id"], svc, e, n)
        return False
    if st == 200 and isinstance(data, dict):
        DB.run("jobs.finish", (t, job_done(job, inp, data), job["id"]))
    else:
        err = data if isinstance(data, dict) else {"error": "bad_response", "service": svc, "message": "HTTP %d" % st}
        DB.run("jobs.fail", (t, json.dumps(err, ensure_ascii=False), job["id"]))
        if job["kind"] == "transcribe":
            drop_audio(inp)  # кусок не читается: хранить его незачем
        if job["kind"] == "deidentify":
            DB.run("transcripts.fail", (json.dumps(err, ensure_ascii=False), t, job["appt_id"]))
    return True


def jobs_loop(stop_evt, svc):
    kinds = JOB_SERVICES[svc]["kinds"]
    while not stop_evt.is_set():
        JOB_EVTS[svc].wait(1.0)
        JOB_EVTS[svc].clear()
        if not service_enabled(svc):
            continue
        try:
            for job in DB.q("jobs.due", (now(), list(kinds))):
                if stop_evt.is_set() or not run_job(job, svc):
                    break  # сервис недоступен: остальные ждут своей очереди
        except DatabaseUnavailable:
            pass  # база недоступна: очередь подождёт
        except Exception:
            log.exception("очередь заданий %s", svc)


def jobs_cleanup():
    """Невыполненное за JOB_KEEP задание не удаляется молча: помечается ошибкой, входные данные (аудио, точки лица)
    стираются, а врач видит пробел. Обезличивание, которое так и не выполнилось, переводит расшифровку в «ошибку»,
    чтобы врач нажал «Обезличить повторно», а не ждал вечно. Завершённые задания удаляются через JOB_KEEP."""
    old, t = now() - JOB_KEEP, now()
    for job in DB.q("jobs.old_queued", (old,)):
        if job["kind"] == "transcribe":
            drop_audio(json.loads(job["input"]))
        svc = job_service(job["kind"])
        err = json.dumps({"error": "expired", "service": svc,
                          "message": "Не выполнено за сутки: сервис %s был недоступен" % svc}, ensure_ascii=False)
        DB.run("jobs.expire", (t, err, job["id"]))
        if job["kind"] == "deidentify":
            DB.run("transcripts.fail", (err, t, job["appt_id"]))
    DB.run("jobs.delete_finished_old", (old,))
    DB.run("speech.delete_old", (now() - TRANSCRIPT_DAYS * 86400,))
    # исходник живёт в соответствии замен: по сроку хранения оно удаляется, обезличенный текст остаётся
    DB.run("transcripts.drop_old_originals", (now() - TRANSCRIPT_DAYS * 86400,))


