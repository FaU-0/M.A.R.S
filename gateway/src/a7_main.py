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
    os.umask(0o077)  # файлы пациентов: только владелец (0600), в том числе в одиночном режиме
    for sub in ("", "files", "photos", "vendor", "audio"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    try:
        DB = open_db()
    except DatabaseUnavailable:
        print("Сервис database не отвечает: gateway не запущен")
        return
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
    for svc in JOB_SERVICES:
        threading.Thread(target=jobs_loop, args=(stop_evt, svc), daemon=True).start()
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


