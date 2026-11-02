"""Desktop launcher: runs the calculator in its own window (no browser, no manual server start)."""
import socket, sys, threading, webbrowser
from http.server import ThreadingHTTPServer
import app


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def main():
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), app.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    try:
        import webview
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.create_window("Material Usage Calculator", url, width=1500, height=950, min_size=(1000, 650))
        webview.start()
    except Exception as e:                       # no WebView2 -> fall back to the default browser
        print("Window unavailable (%s); opening browser at %s" % (e, url))
        webbrowser.open(url)
        try:
            while True: threading.Event().wait(3600)
        except KeyboardInterrupt:
            pass
    srv.shutdown()


if __name__ == "__main__":
    main()
