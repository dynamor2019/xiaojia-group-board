# -*- coding: utf-8 -*-
"""看板的本地小服务（只用标准库，无第三方依赖）。

为什么需要它
------------
看板是个静态 HTML，浏览器不能直接写磁盘文件。而「取消关注某个群」必须落到
data/board_state.json，爬虫下次才能跳过它 —— 所以需要一个极小的本地服务做中转。
顺带把「已完成」标记、以及「刷新 / 体检」这些操作也搬到网页上，省得用户去点一堆 .cmd。

接口
----
    GET  /                看板
    GET  /?demo=1         看板（示例数据，不碰真实数据）
    GET  /api/state       已完成 / 已退订
    POST /api/state       保存
    GET  /api/doctor      环境体检
    POST /api/install     安装 Python 依赖
    POST /api/refresh     开始抓取（微信 + QQ）
    GET  /api/refresh     抓取进度
    GET  /api/health      存活探针
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import board_state  # noqa: E402
import settings  # noqa: E402

DEFAULT_PORT = int(settings.get("board", "port", 8766))


# ------------------------------------------------------------------ 渲染
def render_board(demo: bool = False) -> bytes:
    """每次请求都现生成 —— 取消关注 / 刷新完成后立刻生效，不用重启服务。"""
    import report
    from digest import pick_src

    if demo:
        d = settings.demo_dir()
        sources = []
        qq, wx = d / "群公告_示例.json", d / "微信群公告_示例.json"
        if qq.exists():
            sources.append((qq, "QQ"))
        if wx.exists():
            sources.append((wx, "微信"))
        return report.build(date.today(), 30, sources=sources,
                            served=True, demo=True).encode("utf-8")

    sources = [(pick_src(), "QQ")]
    wx = report.pick_wx_src()
    if wx:
        sources.append((wx, "微信"))
    return report.build(date.today(), settings.days(),
                        sources=sources, served=True).encode("utf-8")


# ------------------------------------------------------------------ 抓取任务
class RefreshJob:
    """一次「刷新」的后台任务：先微信、再 QQ，边跑边把日志吐给前端。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.running = False
        self.rc: int | None = None
        self.started = 0.0
        self.ended = 0.0
        self._lock = threading.Lock()

    def add(self, text: str) -> None:
        with self._lock:
            for ln in str(text).splitlines():
                if ln.strip():
                    self.lines.append(ln.rstrip())
            del self.lines[:-400]        # 只留最后 400 行

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "rc": self.rc,
                "lines": list(self.lines),
                "elapsed": round((self.ended or time.time()) - self.started, 1)
                if self.started else 0,
            }

    def start(self) -> bool:
        if self.running:
            return False
        with self._lock:
            self.lines = []
        self.rc = None
        self.running = True
        self.started = time.time()
        self.ended = 0.0
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self) -> None:
        steps: list[tuple[str, list[str]]] = []
        if settings.get("wechat", "enabled", True):
            steps.append(("微信", [sys.executable, "-E", "-X", "utf8",
                                   str(APP_DIR / "update_wx.py")]))
        if settings.get("qq", "enabled", True):
            steps.append(("QQ", [sys.executable, "-E", "-X", "utf8",
                                 str(APP_DIR / "auto.py"), "--start"]))
        try:
            if not steps:
                self.add("QQ 和微信都被关掉了，没东西可抓。改 config.json 里的 enabled 再试。")
                self.rc = 1
                return
            rc = 0
            for name, cmd in steps:
                self.add(f"── {name} 侧开始 ──")
                try:
                    proc = subprocess.Popen(cmd, cwd=str(APP_DIR),
                                            stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT,
                                            env=_child_env())
                    assert proc.stdout is not None
                    for raw in iter(proc.stdout.readline, b""):
                        self.add(raw.decode("utf-8", "replace"))
                    proc.wait()
                    if proc.returncode:
                        rc = proc.returncode
                        self.add(f"── {name} 侧返回 {proc.returncode}（继续跑另一侧）──")
                    else:
                        self.add(f"── {name} 侧完成 ──")
                except Exception as e:                  # noqa: BLE001
                    rc = 1
                    self.add(f"── {name} 侧出错：{e} ──")
            self.rc = rc
            self.add("全部跑完，正在刷新看板…")
        finally:
            self.running = False
            self.ended = time.time()


def _child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


JOB = RefreshJob()


# ------------------------------------------------------------------ 体检
def doctor() -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, level: str = "warn") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail,
                       "level": "ok" if ok else level})

    v = sys.version_info
    add("Python", v >= (3, 10), f"{sys.version.split()[0]}　{sys.executable}", "fail")

    try:
        import websockets
        add("websockets", True, getattr(websockets, "__version__", "已安装"))
    except ImportError:
        add("websockets", False, "未安装 —— 点「安装依赖」即可", "fail")

    if settings.CONFIG_FILE.exists():
        add("config.json", True, str(settings.CONFIG_FILE))
    else:
        add("config.json", False, "还没有（用默认配置也能跑，想改参数就建一个）")

    if settings.get("qq", "enabled", True):
        nd = settings.napcat_dir()
        ok = nd.exists() and (nd / "NapCatWinBootMain.exe").exists()
        add("NapCat", ok, str(nd) if ok else f"没找到 {nd}　（只影响 QQ）")
        uin = str(settings.get("qq", "uin") or "").strip()
        add("QQ 号", bool(uin), uin or "未填 —— 不填就只能手动开 QQ 再抓")
        exe = settings.qq_exe()
        add("QQ.exe", Path(exe).exists(), exe)
    else:
        add("QQ 侧", True, "已关闭（config.json: qq.enabled = false）")

    if settings.get("wechat", "enabled", True):
        db = settings.wechat_db_dir()
        if db:
            msg = db / "message"
            n = len(list(msg.glob("message_*.db"))) if msg.exists() else 0
            add("微信数据", n > 0, f"{db}　（{n} 个消息库）", "fail")
        else:
            add("微信数据", False, "没找到 db_storage —— 在 config.json 里手填 wechat.db_dir", "fail")
        proc = (settings.get("wechat", "process") or "Weixin.exe").strip()
        running = proc.encode() in subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {proc}", "/FO", "CSV", "/NH"],
            capture_output=True).stdout
        add("微信进程", running,
            f"{proc} {'正在运行' if running else '未运行（首次取密钥需要它开着）'}")
    else:
        add("微信侧", True, "已关闭（config.json: wechat.enabled = false）")

    keys = settings.work_dir() / "all_keys.json"
    add("数据库密钥", keys.exists(), "已有" if keys.exists() else "还没有（首次刷新会自动提取）")

    st = settings.data_dir() / "board_state.json"
    add("看板状态", True, f"{st}　{'已存在' if st.exists() else '首次运行会生成'}")

    fails = [c for c in checks if c["level"] == "fail" and not c["ok"]]
    return {"checks": checks, "ok": not fails, "fails": len(fails)}


# ------------------------------------------------------------------ 依赖安装
def install_deps() -> dict:
    req = APP_DIR / "requirements.txt"
    if not req.exists():
        return {"ok": False, "output": f"找不到 {req}"}
    try:
        p = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)],
                           capture_output=True, timeout=300, cwd=str(APP_DIR))
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
        return {"ok": p.returncode == 0, "output": out[-4000:]}
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "output": str(e)}


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "XiaoJiaBoard/1.0"

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

    def log_message(self, fmt, *args):
        pass

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html", "/board"):
            try:
                self._send(200, render_board("demo=1" in query), "text/html; charset=utf-8")
            except Exception as e:                      # noqa: BLE001
                self._send(500, f"生成看板失败：{e}".encode("utf-8"),
                           "text/plain; charset=utf-8")
        elif path == "/api/state":
            self._json(200, board_state.load())
        elif path == "/api/doctor":
            self._json(200, doctor())
        elif path == "/api/refresh":
            self._json(200, JOB.snapshot())
        elif path == "/api/health":
            st = board_state.load()
            self._json(200, {"ok": True, "done": len(st["done"]),
                             "muted": len(st["muted"]), "refreshing": JOB.running})
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:                               # noqa: BLE001
            body = {}

        if path == "/api/state":
            if not isinstance(body, dict):
                self._json(400, {"ok": False, "error": "body 必须是对象"})
                return
            saved = board_state.save(body)
            self._json(200, {"ok": True, "done": len(saved["done"]),
                             "muted": len(saved["muted"])})
        elif path == "/api/refresh":
            started = JOB.start()
            self._json(200, {"ok": started, "already": not started})
        elif path == "/api/install":
            self._json(200, install_deps())
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


def pick_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")

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

    print(f"小贾AI群看板已启动：{url}")
    print("右上角有「刷新 / 体检」按钮；关掉这个窗口即停止。")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:                               # noqa: BLE001
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
