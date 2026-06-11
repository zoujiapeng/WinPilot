"""WinPilot entry point: start the server and open the browser."""
from __future__ import annotations

import threading
import webbrowser

import uvicorn

from winpilot.config import CONFIG


def main() -> None:
    host = CONFIG.get("server", "host", default="127.0.0.1")
    port = int(CONFIG.get("server", "port", default=8765))
    url = f"http://{host}:{port}/"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"WinPilot UI: {url}")
    uvicorn.run("winpilot.server:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
