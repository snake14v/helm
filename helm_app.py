# helm_app.py — HELM as a native desktop window (not a browser tab).
# Wraps the EXISTING server.py + index.html in a pywebview window (Win11's built-in WebView2 engine,
# so no Chromium is bundled and there's no tab/address bar). Pure addition: edits nothing else. If the
# server is already running (MISSION-CONTROL.bat / start-server-hidden.vbs), it just attaches a window
# to it — one server, N clients. Run:  python helm_app.py   (or build a .exe with PyInstaller later).
import os, socket, subprocess, sys, time

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8799
URL = f"http://127.0.0.1:{PORT}"


def _up(timeout=0.3):
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout):
            return True
    except OSError:
        return False


def _ensure_server():
    """Start server.py hidden only if nothing is already serving :8799."""
    if _up():
        return "already running"
    py = sys.executable
    # pythonw.exe if present = no console flash; else CREATE_NO_WINDOW hides it.
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else py
    subprocess.Popen([exe, os.path.join(ROOT, "server.py")], cwd=ROOT,
                     creationflags=0x08000000)  # CREATE_NO_WINDOW
    for _ in range(80):          # wait up to ~8s for the port
        if _up():
            return "started"
        time.sleep(0.1)
    return "timeout"


def main():
    status = _ensure_server()
    if not _up():
        print(f"HELM server did not come up ({status}) — run 'python server.py' and retry.")
        sys.exit(1)
    import webview
    webview.create_window(
        "GlassPanel", URL,
        width=1360, height=860, min_size=(960, 640),
        background_color="#0a0c10",   # match the app's dark bg so there's no white flash
    )
    webview.start()   # blocks until the window is closed


if __name__ == "__main__":
    main()
