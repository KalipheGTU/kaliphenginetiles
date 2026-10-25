import socket, threading, time
import uvicorn, webview
from app.main import app

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

port = free_port()
threading.Thread(target=lambda: uvicorn.run(app, host="127.0.0.1", port=port, log_config=None), daemon=True).start()
for _ in range(100):
    try: socket.create_connection(("127.0.0.1", port), 0.1).close(); break
    except OSError: time.sleep(0.1)
webview.create_window("KalipheTiles", f"http://127.0.0.1:{port}", width=1400, height=850)
webview.start()
