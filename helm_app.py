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
    import webview
    # Show the animated GlassPanel logo splash INSTANTLY, boot the server behind it, then swap to the app.
    splash = os.path.join(ROOT, "splash.html")
    start_url = splash if os.path.exists(splash) else URL
    window = webview.create_window(
        "GlassPanel", start_url,
        width=1360, height=860, min_size=(960, 640),
        background_color="#05070a",   # matches the splash bg so there's no white flash
    )

    def _boot():
        _ensure_server()
        t0 = time.time()
        while not _up() and time.time() - t0 < 15:
            time.sleep(0.15)
        time.sleep(1.9)   # minimum splash time so the logo animation actually plays before the swap
        try:
            if _up():
                window.load_url(URL)
            else:
                window.load_url("data:text/html,<body style='background:#05070a;color:#e8ecf1;"
                                "font:15px sans-serif;display:flex;align-items:center;justify-content:center;"
                                "height:100vh'>GlassPanel server didn't start &mdash; run "
                                "<code style='margin:0 6px'>python server.py</code></body>")
        except Exception:
            pass

    webview.start(_boot)   # runs _boot on a worker thread after the window opens; blocks until closed


if __name__ == "__main__":
    main()
