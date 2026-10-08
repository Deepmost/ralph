#!/usr/bin/env python3
"""
Ralph Dashboard - 实时监控面板
启动本地 HTTP 服务，提供 dashboard.html 和 /api/state 接口。
数据源：state.json（由 ralph.py 维护）。
"""

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
STATE_FILE = SCRIPT_DIR / "state.json"
HTML_FILE = SCRIPT_DIR / "dashboard.html"

_EMPTY_STATE = json.dumps({
    "runtime": {
        "iteration": 0, "max_iterations": 0,
        "phase": "idle", "current_task": None, "elapsed": 0,
    },
    "tasks": [],
    "logs": "",
}, ensure_ascii=False).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?")[0]

        if path == "/api/state":
            try:
                body = STATE_FILE.read_bytes()
            except Exception:
                body = _EMPTY_STATE
            self._respond(200, "application/json; charset=utf-8", body,
                          extra={"Access-Control-Allow-Origin": "*"})

        elif path in ("/", "/index.html"):
            try:
                self._respond(200, "text/html; charset=utf-8", HTML_FILE.read_bytes())
            except Exception as e:
                self._respond(500, "text/plain", str(e).encode())

        else:
            self.send_response(404)
            self.end_headers()

    # ── helpers ──

    def _respond(self, code: int, content_type: str, body: bytes,
                 extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass  # 静默访问日志


def start(port: int = 17331, max_iterations: int = 50,
          open_browser: bool = True) -> None:
    """启动 dashboard HTTP 服务（daemon 线程）"""
    server = HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://localhost:{port}"
    print(f"  Dashboard: {url}")

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()


if __name__ == "__main__":
    print("Dashboard 独立启动模式（调试用）")
    start(open_browser=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nDashboard 已停止")
