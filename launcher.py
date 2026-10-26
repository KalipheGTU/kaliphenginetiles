import logging, socket, sys, threading, time, traceback

def fatal(msg):
    try: logging.error(msg)
    except Exception: pass
    if sys.platform == "win32":
        try:
            import ctypes; ctypes.windll.user32.MessageBoxW(0, msg[-1200:], "KalipheTiles — erreur", 0x10)
        except Exception: pass

try:
    from app.main import app, DATA
    logging.basicConfig(filename=str(DATA / "kaliphetiles.log"), level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    sys.excepthook = lambda *a: logging.error("".join(traceback.format_exception(*a)))
    import uvicorn, webview

    def free_port():
        s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

    port = free_port()
    threading.Thread(target=lambda: uvicorn.run(app, host="127.0.0.1", port=port, log_config=None), daemon=True).start()
    for _ in range(100):
        try: socket.create_connection(("127.0.0.1", port), 0.1).close(); break
        except OSError: time.sleep(0.1)
    logging.info("démarrage sur le port %s", port)
    webview.create_window("KalipheTiles", f"http://127.0.0.1:{port}", width=1400, height=850)
    webview.start()
except Exception:
    fatal(traceback.format_exc())
