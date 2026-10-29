# -*- coding: utf-8 -*-
"""Схема базы M.A.R.S. и её миграции.

MIGRATIONS - список шагов по порядку; номер шага = его место в списке, начиная с 1.
Применённый номер хранится в таблице schema_version. Изменение схемы - только новым шагом в конце списка,
старые шаги не редактируются: по ним уже обновлены существующие базы.
"""

# 1. Схема на момент выноса SQL из gateway. CREATE ... IF NOT EXISTS: база, созданная прежним gateway,
#    проходит этот шаг без изменений и просто получает номер версии.
V1 = """
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
CREATE TABLE IF NOT EXISTS speech_segments(id INTEGER PRIMARY KEY, appt_id INTEGER NOT NULL, role TEXT NOT NULL,
  chunk_t INTEGER NOT NULL, seq INTEGER NOT NULL DEFAULT 0, start REAL NOT NULL, end REAL NOT NULL, text TEXT NOT NULL,
  created INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_speech_appt ON speech_segments(appt_id, chunk_t);
CREATE TABLE IF NOT EXISTS transcripts(appt_id INTEGER PRIMARY KEY, status TEXT NOT NULL, deid TEXT, mapping TEXT,
  confirmed INTEGER NOT NULL DEFAULT 0, model TEXT DEFAULT '', stats TEXT, error TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_photos_patient ON photos(patient_id, pose, ts);
CREATE INDEX IF NOT EXISTS ix_meas_patient ON measurements(patient_id, ts);
"""

# 2. Снимки и измерения, сохранённые до создания карточки пациента, оставались без patient_id:
#    их не было в истории пациента, а проверка доступа к таким снимкам пропускалась.
V2 = """
UPDATE photos SET patient_id=(SELECT a.patient_id FROM appointments a WHERE a.id=photos.appt_id)
  WHERE patient_id IS NULL;
UPDATE measurements SET patient_id=(SELECT a.patient_id FROM appointments a WHERE a.id=measurements.appt_id)
  WHERE patient_id IS NULL;
CREATE INDEX IF NOT EXISTS ix_photos_appt ON photos(appt_id)
"""

MIGRATIONS = [V1, V2]
