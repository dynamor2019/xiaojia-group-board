# -*- coding: utf-8 -*-
"""看板的本地小服务（只用标准库，无第三方依赖）。

为什么需要它
------------
看板是个静态 HTML，浏览器不能直接写磁盘文件。而「取消关注某个群」必须写到
board_state.json 里，爬虫下次才能跳过它 —— 所以需要一个极小的本地服务做中转。
顺带把「已完成」的标记也搬到服务端，比浏览器存储可靠得多（预览面板换端口也不丢）。

用法
----
    python serve_board.py          # 默认 http://127.0.0.1:8766
    启动看板.cmd                    # 启动并自动开浏览器
"""
from __future__ import annotations

import json
import socket
import sys
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import board_state  # noqa: E402
import settings  # noqa: E402

DEFAULT_PORT = int(settings.get("board", "port", 8766))
DAYS = settings.days()


def render_board() -> bytes:
    """每次请求都现生成，这样取消关注后立刻生效，不用重跑爬虫。"""
    import report
    from digest import pick_src

    sources = [(pick_src(), "QQ")]
    wx = report.pick_wx_src()
    if wx:
        sources.append((wx, "微信"))
    html_text = report.build(date.today(), DAYS, sources=sources, served=True)
    return html_text.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "GroupBoard/1.0"

    # ---- 工具 ----
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, fmt, *args):  # 静音，别刷屏
        pass

    # ---- 路由 ----
    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/board"):
            try:
                self._send(200, render_board(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, f"生成看板失败：{e}".encode("utf-8"),
                           "text/plain; charset=utf-8")
        elif path == "/api/state":
            self._json(200, board_state.load())
        elif path == "/api/health":
            st = board_state.load()
            self._json(200, {"ok": True, "done": len(st["done"]), "muted": len(st["muted"])})
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path != "/api/state":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body 必须是对象")
            saved = board_state.save(body)
            self._json(200, {"ok": True, "done": len(saved["done"]),
                             "muted": len(saved["muted"])})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})


def pick_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    port = DEFAULT_PORT
    open_browser = bool(settings.get("board", "open_browser", True))
    for a in sys.argv[1:]:
        if a.isdigit():
            port = int(a)
        elif a in ("--no-open", "-n"):
            open_browser = False

    port = pick_port(port)
    url = f"http://127.0.0.1:{port}/"
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    print(f"看板服务已启动：{url}")
    print("按 Ctrl+C 停止。取消关注的群会写进 board_state.json，爬虫下次自动跳过。")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
