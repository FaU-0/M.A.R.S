# 5. Данные

## 5.1 Модель данных

База — SQLite в сервисе database (`DATA_DIR/mars.sqlite3`). Схема описана в `database/schema.py`, все запросы — в `database/queries.py`.

```mermaid
erDiagram
    doctors ||--o{ sessions : "входит"
    doctors ||--o{ appointments : "ведёт"
    patients ||--o{ appointments : "записан"
    appointments ||--o{ messages : "чат, события"
    appointments ||--o| notes : "заметки врача"
    appointments ||--o| conclusions : "заключение"
    appointments ||--o{ files : "файлы"
    appointments ||--o{ photos : "снимки лица"
    appointments ||--o{ measurements : "метрики лица"
    appointments ||--o{ jobs : "задания"
    appointments ||--o{ speech_segments : "текст речи"
    appointments ||--o| transcripts : "обезличенная расшифровка"
    patients ||--o{ photos : "история"
    patients ||--o{ measurements : "история"

    doctors {
        int id PK
        text login UK
        text name
        blob salt
        blob pw "scrypt"
        int is_admin
    }
    sessions {
        text sid PK "SHA-256 от cookie"
        int doctor_id
        int expires
    }
    patients {
        int id PK
        text full_name
        text birth
        text email
        text allergies_chronic_meds_complaints
    }
    appointments {
        int id PK
        text code UK "код комнаты"
        int patient_id
        int doctor_id
        int scheduled
        text status "planned waiting in_progress completed"
        text protocol "JSON: карточка приёма"
        int started
        int ended
    }
    measurements {
        int id PK
        int appt_id
        int patient_id
        text kind "symmetry mobility compare calibration"
        text body "JSON"
        int ts
    }
    photos {
        int id PK
        int appt_id
        int patient_id
        text pose
        text path
    }
    jobs {
        int id PK
        int appt_id
        text kind "symmetry transcribe deidentify ..."
        text key "UNIQUE с appt_id"
        text status "queued done error cancelled"
        int attempts
        int next_try
        text input
        text result
    }
    speech_segments {
        int id PK
        int appt_id
        text role "doctor patient"
        int chunk_t
        real start
        real end
        text text
    }
    transcripts {
        int appt_id PK
        text status
        text deid "обезличенный текст"
        text mapping "соответствие замен"
        int confirmed
    }
    audit {
        int id PK
        int ts
        text actor
        text action
        text room
        text detail
    }
```

Таблицы `messages`, `notes`, `conclusions`, `files` устроены просто: привязка к приёму, время, отправитель, содержимое. Файлы, снимки и аудио хранятся на диске в `DATA_DIR/files`, `photos`, `audio`, а в базе лежит путь.

## 5.2 Миграции

Схема меняется только **новым шагом в конце списка** `MIGRATIONS` в `database/schema.py`. Номер применённого шага хранится в таблице `schema_version`. База, созданная прежней версией, обновляется при первом запуске без потери данных.

| Шаг | Что делает |
|---|---|
| 1 | Создаёт все таблицы и индексы |
| 2 | Привязывает к пациенту снимки и измерения, сделанные до создания карточки (раньше они выпадали из истории пациента) |

## 5.3 Где лежат данные

| Путь | Что | Доступ |
|---|---|---|
| `DATA_DIR/mars.sqlite3` | База | 0600 |
| `DATA_DIR/files/`, `photos/`, `audio/` | Файлы приёма, снимки лица, куски речи до расшифровки | 0600, папка 0700 |
| `DATA_DIR/backups/` | Резервные копии базы, хранятся 14 последних | 0600 |
| `DATA_DIR/vendor/` | Файлы MediaPipe, проверенные по SHA-256 | — |
| `DATA_DIR/models/` (или `MODELS_DIR`) | Модель Whisper | — |
| `DATA_DIR/admin_password.txt` | Пароль администратора; удаляется после первой смены пароля | 0600 |

## 5.4 Сроки хранения

```mermaid
flowchart LR
    A["Кусок аудио 10 с"] -- "сразу после расшифровки" --> X1["удалён"]
    A -- "не расшифрован за 24 ч" --> X2["удалён, задание = ошибка"]
    B["Исходный текст речи"] -- "30 дней (TRANSCRIPT_DAYS)" --> X3["удалён"]
    C["Обезличенный текст"] --> K["хранится с приёмом"]
    D["Входные данные задания<br/>(точки лица, аудио)"] -- "24 ч (JOB_KEEP)" --> X4["стёрты"]
    E["Выполненное задание"] -- "24 ч" --> X5["удалено"]
    F["Видео звонка"] --> X6["не хранится никогда<br/>(идёт между браузерами)"]
```

## 5.5 Лимиты на один приём

Лимиты не дают заполнить диск через один приём. Место пациента занимает тот, кто первым вошёл по коду.

| Что | Лимит | Ответ при превышении |
|---|---|---|
| Файлы | 200 МБ (`APPT_FILES_MB`) | 413 |
| Снимки | 300 (`APPT_PHOTOS`) | 413 |
| Измерения | 500 (`APPT_MEASUREMENTS`) | 413 |
| Куски речи в очереди | 720, это 2 часа (`APPT_QUEUED_AUDIO`) | 413 |
| Свободное место на диске | не меньше 500 МБ (`MIN_FREE_MB`) | 507 |
| JSON / файл / снимок | 25 / 20 / 5 МБ | 413 |
