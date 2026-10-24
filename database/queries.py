# -*- coding: utf-8 -*-
"""Все запросы M.A.R.S. к базе. Gateway вызывает их по имени и SQL не содержит.

QUERIES: имя -> (вид, SQL). Вид: list (все строки), one (одна строка или None), run (изменение, ответ lastrowid).
DYNAMIC: запросы, которые собираются из параметров (список полей, фильтр по врачу, список видов заданий).
Новый запрос добавляется сюда; изменение схемы - новой миграцией в schema.py.
"""

QUERIES = {
    # аудит
    "audit.add": ("run", "INSERT INTO audit(ts,actor,action,room,detail) VALUES(?,?,?,?,?)"),
    "audit.recent": ("list", "SELECT ts,actor,action,room,detail FROM audit ORDER BY id DESC LIMIT ?"),

    # врачи и сессии
    "doctors.add": ("run", "INSERT INTO doctors(login,name,salt,pw,is_admin,created) VALUES(?,?,?,?,?,?)"),
    "doctors.admin_password": ("one", "SELECT id, salt, pw FROM doctors WHERE login='admin'"),
    "doctors.by_login": ("one", "SELECT * FROM doctors WHERE login=?"),
    "doctors.id_by_login": ("one", "SELECT id FROM doctors WHERE login=?"),
    "doctors.name": ("one", "SELECT name FROM doctors WHERE id=?"),
    "doctors.set_admin_password": ("run", "UPDATE doctors SET salt=?, pw=?, is_admin=1 WHERE id=?"),
    "sessions.add": ("run", "INSERT INTO sessions(sid,doctor_id,expires) VALUES(?,?,?)"),
    "sessions.doctor": ("one", "SELECT d.* FROM sessions s JOIN doctors d ON d.id=s.doctor_id WHERE s.sid=? AND s.expires>?"),
    "sessions.delete": ("run", "DELETE FROM sessions WHERE sid=?"),
    "sessions.delete_for_doctor": ("run", "DELETE FROM sessions WHERE doctor_id=?"),
    "sessions.delete_expired": ("run", "DELETE FROM sessions WHERE expires<?"),

    # приёмы
    "appointments.add": ("run", "INSERT INTO appointments(code,patient_id,doctor_id,scheduled,status,created) VALUES(?,?,?,?, 'planned',?)"),
    "appointments.by_id": ("one", "SELECT * FROM appointments WHERE id=?"),
    "appointments.full_by_id": ("one", "SELECT a.*, p.full_name AS patient_name, p.email AS patient_email, d.name AS doctor_name FROM appointments a "
                                       "LEFT JOIN patients p ON p.id=a.patient_id LEFT JOIN doctors d ON d.id=a.doctor_id WHERE a.id=?"),
    "appointments.with_doctor_by_code": ("one", "SELECT a.*, d.name AS doctor_name FROM appointments a LEFT JOIN doctors d ON d.id=a.doctor_id WHERE a.code=?"),
    "appointments.id_by_code": ("one", "SELECT id FROM appointments WHERE code=?"),
    "appointments.status_by_code": ("one", "SELECT id,status FROM appointments WHERE code=?"),
    "appointments.protocol": ("one", "SELECT protocol FROM appointments WHERE id=?"),
    "appointments.set_protocol": ("run", "UPDATE appointments SET protocol=? WHERE id=?"),
    "appointments.set_doctor": ("run", "UPDATE appointments SET doctor_id=? WHERE id=?"),
    "appointments.set_patient": ("run", "UPDATE appointments SET patient_id=? WHERE id=?"),
    "appointments.set_waiting": ("run", "UPDATE appointments SET status='waiting' WHERE id=?"),
    "appointments.set_started": ("run", "UPDATE appointments SET status='in_progress', started=? WHERE id=?"),
    "appointments.set_completed": ("run", "UPDATE appointments SET status='completed', ended=? WHERE id=?"),

    # пациенты
    "patients.add": ("run", "INSERT INTO patients(full_name,created) VALUES(?,?)"),
    "patients.add_full": ("run", "INSERT INTO patients(full_name,birth,email,created) VALUES(?,?,?,?)"),
    "patients.by_id": ("one", "SELECT * FROM patients WHERE id=?"),
    "patients.exists": ("one", "SELECT id FROM patients WHERE id=?"),
    "patients.name": ("one", "SELECT full_name FROM patients WHERE id=?"),
    "patients.search": ("list", "SELECT id, full_name, birth FROM patients WHERE full_name LIKE ? ORDER BY full_name LIMIT 100"),
    "patients.set_email": ("run", "UPDATE patients SET email=? WHERE id=?"),
    # доступ врача к пациенту: только через свой приём (администратор видит всех)
    "patients.seen_by_doctor": ("one", "SELECT 1 AS x FROM appointments WHERE patient_id=? AND doctor_id=? LIMIT 1"),
    "patients.search_for_doctor": ("list", "SELECT DISTINCT p.id, p.full_name, p.birth FROM patients p JOIN appointments a ON a.patient_id=p.id "
                                           "WHERE a.doctor_id=? AND p.full_name LIKE ? ORDER BY p.full_name LIMIT 100"),

    # переписка, файлы, снимки, измерения, заметки, заключения
    "messages.add": ("run", "INSERT INTO messages(appt_id,ts,sender,kind,body) VALUES(?,?,?,?,?)"),
    "files.add": ("run", "INSERT INTO files(appt_id,ts,sender,name,mime,size) VALUES(?,?,?,?,?,?)"),
    "files.set_path": ("run", "UPDATE files SET path=? WHERE id=?"),
    "files.in_appointment": ("one", "SELECT * FROM files WHERE id=? AND appt_id=?"),
    "photos.add": ("run", "INSERT INTO photos(appt_id,patient_id,pose,ts,size) VALUES(?,?,?,?,?)"),
    "photos.set_path": ("run", "UPDATE photos SET path=? WHERE id=?"),
    "photos.by_id": ("one", "SELECT * FROM photos WHERE id=?"),
    "photos.of_patient": ("list", "SELECT id, appt_id, pose, ts, size FROM photos WHERE patient_id=? ORDER BY ts DESC LIMIT 500"),
    "measurements.add": ("run", "INSERT INTO measurements(appt_id,patient_id,kind,ts,body) VALUES(?,?,?,?,?)"),
    "measurements.of_patient": ("list", "SELECT id, appt_id, kind, ts, body FROM measurements WHERE patient_id=? ORDER BY ts LIMIT 2000"),
    "notes.get": ("one", "SELECT body FROM notes WHERE appt_id=?"),
    "notes.save": ("run", "INSERT INTO notes(appt_id,body,updated) VALUES(?,?,?) ON CONFLICT(appt_id) DO UPDATE SET body=excluded.body, updated=excluded.updated"),
    "conclusions.get": ("one", "SELECT body FROM conclusions WHERE appt_id=?"),
    "conclusions.save": ("run", "INSERT INTO conclusions(appt_id,body,updated) VALUES(?,?,?) ON CONFLICT(appt_id) DO UPDATE SET body=excluded.body, updated=excluded.updated"),

    # очередь заданий для вторичных сервисов
    "jobs.add": ("run", "INSERT INTO jobs(appt_id,kind,key,created,updated,input) VALUES(?,?,?,?,?,?)"),
    "jobs.add_once": ("run", "INSERT OR IGNORE INTO jobs(appt_id,kind,key,created,updated,input) VALUES(?,?,?,?,?,?)"),
    "jobs.by_key": ("one", "SELECT * FROM jobs WHERE appt_id=? AND key=?"),
    "jobs.in_appointment": ("one", "SELECT * FROM jobs WHERE id=? AND appt_id=?"),
    "jobs.first_input_like": ("one", "SELECT input FROM jobs WHERE appt_id=? AND kind='transcribe' AND key LIKE ? ORDER BY id LIMIT 1"),
    "jobs.latest_deidentify": ("one", "SELECT * FROM jobs WHERE appt_id=? AND kind='deidentify' ORDER BY id DESC LIMIT 1"),
    "jobs.any_transcribe": ("one", "SELECT 1 AS x FROM jobs WHERE appt_id=? AND kind='transcribe' LIMIT 1"),
    "jobs.transcribe_pending": ("one", "SELECT 1 AS x FROM jobs WHERE appt_id=? AND kind='transcribe' AND status='queued' LIMIT 1"),
    "jobs.transcribe_counts": ("list", "SELECT status, COUNT(*) AS n FROM jobs WHERE appt_id=? AND kind='transcribe' GROUP BY status"),
    "jobs.postpone": ("run", "UPDATE jobs SET next_try=? WHERE id=?"),
    "jobs.retry_later": ("run", "UPDATE jobs SET attempts=?, next_try=?, updated=?, error=? WHERE id=?"),
    "jobs.give_up": ("run", "UPDATE jobs SET status='error', attempts=?, updated=?, error=? WHERE id=?"),
    "jobs.fail": ("run", "UPDATE jobs SET status='error', updated=?, error=? WHERE id=?"),
    "jobs.finish": ("run", "UPDATE jobs SET status='done', updated=?, result=?, error=NULL WHERE id=?"),
    "jobs.old_transcribe_inputs": ("list", "SELECT input FROM jobs WHERE created<? AND kind='transcribe'"),
    "jobs.delete_old": ("run", "DELETE FROM jobs WHERE created<?"),

    # расшифровка речи и обезличивание
    "speech.add_segment": ("run", "INSERT INTO speech_segments(appt_id,role,chunk_t,seq,start,end,text,created) VALUES(?,?,?,?,?,?,?,?)"),
    "speech.segments": ("list", "SELECT role, chunk_t, start, end, text FROM speech_segments WHERE appt_id=? ORDER BY chunk_t, id"),
    "speech.any_segment": ("one", "SELECT 1 AS x FROM speech_segments WHERE appt_id=? LIMIT 1"),
    "speech.delete_old": ("run", "DELETE FROM speech_segments WHERE created<?"),
    "transcripts.get": ("one", "SELECT * FROM transcripts WHERE appt_id=?"),
    "transcripts.queue": ("run", "INSERT INTO transcripts(appt_id,status,created,updated) VALUES(?,?,?,?) "
                                 "ON CONFLICT(appt_id) DO UPDATE SET status='queued', confirmed=0, error=NULL, updated=excluded.updated"),
    "transcripts.done": ("run", "UPDATE transcripts SET status='done', deid=?, mapping=?, confirmed=0, model=?, stats=?, error=NULL, updated=? WHERE appt_id=?"),
    "transcripts.fail": ("run", "UPDATE transcripts SET status='error', error=?, updated=? WHERE appt_id=?"),
    "transcripts.edit": ("run", "UPDATE transcripts SET deid=?, mapping=?, confirmed=0, updated=? WHERE appt_id=?"),
    "transcripts.set_confirmed": ("run", "UPDATE transcripts SET confirmed=?, updated=? WHERE appt_id=?"),
    "transcripts.drop_old_originals": ("run", "UPDATE transcripts SET mapping=NULL WHERE created<? AND mapping IS NOT NULL"),
}

CARD_FIELDS = ("full_name", "birth", "sex", "blood", "allergies", "chronic", "meds", "complaints", "email")


def _patient_update(conn, pid, fields):
    """Поля карточки пациента: только из белого списка."""
    vals = {k: v for k, v in (fields or {}).items() if k in CARD_FIELDS}
    if not vals:
        return None
    sets = ",".join("%s=?" % k for k in vals)
    return conn.execute("UPDATE patients SET %s WHERE id=?" % sets, tuple(vals.values()) + (pid,)).lastrowid


def _appointments_between(conn, t_from, t_to, doctor_id=None):
    """Приёмы за период; doctor_id: только свои и неназначенные (для не-администратора)."""
    sql = ("SELECT a.*, p.full_name AS patient_name, p.email AS patient_email, d.name AS doctor_name FROM appointments a "
           "LEFT JOIN patients p ON p.id=a.patient_id LEFT JOIN doctors d ON d.id=a.doctor_id "
           "WHERE a.scheduled BETWEEN ? AND ?")
    args = [t_from, t_to]
    if doctor_id is not None:
        sql += " AND (a.doctor_id=? OR a.doctor_id IS NULL)"
        args.append(doctor_id)
    sql += " ORDER BY a.scheduled LIMIT 500"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _jobs_due(conn, now, kinds):
    """Задания в очереди, которым пора, для одного сервиса (его видов заданий)."""
    kinds = [str(k) for k in kinds or []][:20]
    if not kinds:
        return []
    sql = "SELECT * FROM jobs WHERE status='queued' AND next_try<=? AND kind IN (%s) ORDER BY id LIMIT 10" % ",".join("?" * len(kinds))
    return [dict(r) for r in conn.execute(sql, [now] + kinds).fetchall()]


DYNAMIC = {
    "patients.update_card": ("run", _patient_update),
    "appointments.between": ("list", _appointments_between),
    "jobs.due": ("list", _jobs_due),
}
