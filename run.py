"""Starts ClipForge and opens it in your browser."""
import threading
import webbrowser

import uvicorn

PORT = 8765

if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print(f"\n  ClipForge is running at {url}\n  Keep this window open while you use it. Close it to stop ClipForge.\n")
    threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=PORT, log_level="warning")
