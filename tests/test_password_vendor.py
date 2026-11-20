# -*- coding: utf-8 -*-
"""Безопасность: смена пароля и проверка файлов MediaPipe по SHA-256.

1. Файлы MediaPipe (без сети): подменённый файл не сохраняется при скачивании, лежащий на диске с чужим
   хэшем считается отсутствующим и удаляется, сервер не отдаёт в браузер ни его, ни посторонние файлы.
2. Смена пароля через POST /auth/password: старый пароль обязателен, новый не короче 8 символов
   и не равен старому, остальные сессии врача закрываются, запись в аудите без пароля.
3. Пароль администратора, сменённый в интерфейсе, не откатывается при перезапуске.

    python tests/test_password_vendor.py
Файлы MediaPipe берутся из mars_data/vendor (если их нет, часть 1 пропускается).
"""
import http.cookiejar
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR_SRC = ROOT / "mars_data" / "vendor"
PORT = 18960
FAILS = []


def check(c, m):
    print(("OK    " if c else "FAIL  ") + m)
    if not c:
        FAILS.append(m)


def load_gateway(data_dir):
    os.environ["DATA_DIR"] = str(data_dir)
    spec = importlib.util.spec_from_file_location("mars_gateway_test", ROOT / "gateway" / "mars.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def part_vendor():
    print("--- файлы MediaPipe ---")
    if not (VENDOR_SRC / "vision_bundle.mjs").is_file():
        print("SKIP  нет mars_data/vendor: запустите M.A.R.S. один раз, чтобы скачать файлы")
        return
    tmp = Path(tempfile.mkdtemp(prefix="mars_pv_"))
    shutil.copytree(VENDOR_SRC, tmp / "vendor")
    gw = load_gateway(tmp)
    check(set(gw.VENDOR_SHA256) == {rel for rel, _ in gw.VENDOR_FILES}, "у каждого файла MediaPipe записан SHA-256")
    check(gw.vendor_missing() == [], "настоящие файлы проходят проверку хэша")

    bundle = tmp / "vendor" / "vision_bundle.mjs"
    good = bundle.read_bytes()
    bundle.write_bytes(good.replace(b"function", b"functiom", 1))
    missing = [rel for rel, _ in gw.vendor_missing()]
    check(missing == ["vision_bundle.mjs"], "подменённый файл на диске считается отсутствующим")
    check(not gw.vendor_file_ok("vision_bundle.mjs"), "подменённый файл не проходит проверку перед отдачей в браузер")

    real_urlopen = gw.urllib.request.urlopen
    gw.urllib.request.urlopen = lambda req, timeout=None: FakeResp(b"alert('evil');" + good[:100])
    try:
        ok = gw.ensure_vendor()
    finally:
        gw.urllib.request.urlopen = real_urlopen
    check(ok is False, "скачанный файл с чужим хэшем: Face Mesh отключён")
    check(not bundle.exists(), "файл с чужим хэшем не сохранён и прежний подменённый удалён")
    check(not (tmp / "vendor" / "vision_bundle.mjs.part").exists(), "временный файл не остался")

    gw.urllib.request.urlopen = lambda req, timeout=None: FakeResp(good)
    try:
        ok = gw.ensure_vendor()
    finally:
        gw.urllib.request.urlopen = real_urlopen
    check(ok is True and bundle.read_bytes() == good, "файл с правильным хэшем сохраняется")
    (tmp / "vendor" / "notes.txt").write_text("посторонний файл")
    check(not gw.vendor_file_ok("notes.txt"), "файлы не из списка MediaPipe сервер не отдаёт")
    shutil.rmtree(tmp, ignore_errors=True)


class Client:
    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.op.open(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            raw = e.read().decode() or "null"
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, None

    def login(self, login, pw):
        return self.call("POST", "/auth/login", {"login": login, "password": pw})[0]

    def me(self):
        return (self.call("GET", "/auth/me")[1] or {}).get("doctor")


def start(data_dir, admin_password=None):
    env = dict(os.environ, DATA_DIR=str(data_dir), PORT=str(PORT))
    env.pop("MARS_SERVICES", None)
    env.pop("ADMIN_PASSWORD", None)
    if admin_password:
        env["ADMIN_PASSWORD"] = admin_password
    p = subprocess.Popen([sys.executable, str(ROOT / "gateway" / "mars.py")], cwd=str(ROOT), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    c = Client()
    for _ in range(60):
        try:
            c.call("GET", "/health")
            return p
        except Exception:  # noqa: BLE001  сервер ещё не слушает
            time.sleep(0.25)
    p.terminate()
    raise SystemExit("gateway не запустился")


def stop(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


def part_password():
    print("--- смена пароля ---")
    tmp = Path(tempfile.mkdtemp(prefix="mars_pw_"))
    if VENDOR_SRC.is_dir():
        shutil.copytree(VENDOR_SRC, tmp / "vendor")  # чтобы сервер не скачивал MediaPipe
    p = start(tmp, admin_password="test-pass-1234")
    try:
        admin = Client()
        admin.login("admin", "test-pass-1234")
        admin.call("POST", "/doctors", {"login": "doc_pw", "name": "Врач Пароль", "password": "first-pass-1"})
        anon = Client()
        s, _ = anon.call("POST", "/auth/password", {"old_password": "first-pass-1", "new_password": "second-pass-2"})
        check(s == 401, "без входа сменить пароль нельзя (401)")

        doc, other = Client(), Client()
        doc.login("doc_pw", "first-pass-1")
        other.login("doc_pw", "first-pass-1")
        s, b = doc.call("POST", "/auth/password", {"old_password": "wrong-pass", "new_password": "second-pass-2"})
        check(s == 403 and (b or {}).get("error") == "bad_credentials", "неверный старый пароль: 403")
        s, _ = doc.call("POST", "/auth/password", {"old_password": "first-pass-1", "new_password": "short"})
        check(s == 400, "новый пароль короче 8 символов: 400")
        s, _ = doc.call("POST", "/auth/password", {"old_password": "first-pass-1", "new_password": "first-pass-1"})
        check(s == 400, "новый пароль совпадает со старым: 400")
        s, _ = doc.call("POST", "/auth/password", {"old_password": "first-pass-1", "new_password": "second-pass-2"})
        check(s == 200, "пароль сменён")
        check((doc.me() or {}).get("login") == "doc_pw", "текущий вход сохраняется")
        check(other.me() is None, "другие сессии этого врача закрыты")
        check(Client().login("doc_pw", "first-pass-1") == 401, "старый пароль больше не подходит")
        check(Client().login("doc_pw", "second-pass-2") == 200, "новый пароль подходит")
        s, b = admin.call("GET", "/audit?limit=50")
        items = (b or {}).get("items") or []
        rows = [it for it in items if it.get("action") == "password_changed"]
        check(len(rows) == 1 and "doctor:doc_pw" in rows[0].get("actor", ""), "смена пароля записана в журнал аудита")
        check(not any("pass-" in json.dumps(it, ensure_ascii=False) for it in items), "паролей в журнале аудита нет")

        s, _ = admin.call("POST", "/auth/password", {"old_password": "test-pass-1234", "new_password": "admin-new-pass-9"})
        check(s == 200, "администратор сменил свой пароль")
        check(not (tmp / "admin_password.txt").exists(), "файл со старым паролем администратора удалён")
        codes = [doc.call("POST", "/auth/password", {"old_password": "guess-%d" % i, "new_password": "x" * 10})[0] for i in range(10)]
        check(429 in codes, "перебор текущего пароля ограничен (429)")
    finally:
        stop(p)

    p = start(tmp)  # перезапуск без ADMIN_PASSWORD: пароль из интерфейса остаётся
    try:
        check(Client().login("admin", "admin-new-pass-9") == 200, "после перезапуска действует пароль, заданный в интерфейсе")
        check(Client().login("admin", "test-pass-1234") == 401, "старый пароль администратора не вернулся")
    finally:
        stop(p)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    part_vendor()
    part_password()
    print("\nИтог: %s" % ("всё прошло" if not FAILS else "ошибок %d" % len(FAILS)))
    sys.exit(1 if FAILS else 0)
