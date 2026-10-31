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
    POST /api/days        改「近 N 天」（存进 config.json，前端随后整页重取）
    POST /api/refresh     开始抓取（微信 + QQ）
    GET  /api/refresh     抓取进度
    POST /api/napcat      下载 NapCat
    GET  /api/napcat      下载进度
    POST /api/update/apply  开始更新到 GitHub 最新版
    GET  /api/update/apply  更新进度
    GET  /api/update      有没有新版本（默认走 24 小时缓存，?force=1 强制联网）
    POST /api/update/ignore 这次不提醒
    POST /api/restart     重启自己（更新完换上新代码）
    POST /api/quit        停掉这个服务
    GET  /api/health      存活探针
"""
from __future__ import annotations

import json
import os
import re
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
import update_check  # noqa: E402

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
        if settings.get("qq", "enabled", False):
            steps.append(("QQ", [sys.executable, "-E", "-X", "utf8",
                                 str(APP_DIR / "auto.py"), "--start"]))
        try:
            if not steps:
                self.add("QQ 和微信都关着，没东西可抓 —— 点看板右上角「数据源」，"
                         "把要用的那一路打开（QQ 那边开之前先看一眼风险说明）。")
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


# ------------------------------------------------------------------ 更新任务
class UpdateJob:
    """更新到 GitHub 最新版。git pull 或整包覆盖都在后台跑，日志照样吐给前端。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.running = False
        self.result: dict | None = None
        self._lock = threading.Lock()

    def add(self, text: str) -> None:
        with self._lock:
            for ln in str(text).splitlines():
                if ln.strip():
                    self.lines.append(ln.rstrip())
            del self.lines[:-300]

    def snapshot(self) -> dict:
        with self._lock:
            return {"running": self.running, "lines": list(self.lines),
                    "result": self.result}

    def start(self) -> bool:
        if self.running:
            return False
        with self._lock:
            self.lines = []
        self.result = None
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self) -> None:
        try:
            self.result = update_check.apply(progress=self.add)
        except Exception as e:                          # noqa: BLE001
            self.result = {"ok": False, "error": str(e)[:200]}
            self.add("出错：" + str(e)[:200])
        finally:
            self.running = False


UPD = UpdateJob()

_RESTARTING = False


def _restart_server() -> None:
    """把自己关掉，再用同一个端口起一个新的 —— .py 已经换新，不重启还是老代码在跑。"""
    global _RESTARTING
    _RESTARTING = True
    time.sleep(0.4)                      # 先把这次的 HTTP 响应吐干净
    port = None
    try:
        if _HTTPD:
            port = _HTTPD.server_address[1]
            _HTTPD.shutdown()
            _HTTPD.server_close()
    except Exception:                                   # noqa: BLE001
        pass
    time.sleep(1.0)                      # 等端口真的释放，新进程才能抢到同一个号
    if not port:
        return
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [sys.executable, "-X", "utf8", str(APP_DIR / "serve_board.py"),
             str(port), "--no-open"],
            cwd=str(settings.root()), creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        pass


def _heartbeat() -> None:
    """每天问一次 GitHub。失败不吭声，界面上会显示上次的错误，别打扰用户。"""
    time.sleep(8)                        # 别和启动抢网络
    while True:
        try:
            if update_check.due():
                update_check.check()
        except Exception:                               # noqa: BLE001
            pass
        time.sleep(1800)                 # 每半小时看一次"到点没"，真正联网一天一次

# serve_forever 用的实例，main() 里赋值；/api/quit 要靠它停服务
_HTTPD: "ThreadingHTTPServer | None" = None


def _shutdown_server() -> None:
    """停掉 serve_forever。放独立线程里跑，免得在请求线程里等自己收尾。"""
    time.sleep(0.25)
    if _HTTPD is not None:
        _HTTPD.shutdown()


# ------------------------------------------------------------------ 装 NapCat
class NapCatJob:
    """下载 / 升级 NapCat 的后台任务，进度一行行吐给前端。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.running = False
        self.ok: bool | None = None
        self._lock = threading.Lock()

    def add(self, text: str) -> None:
        with self._lock:
            for ln in str(text).splitlines():
                if ln.strip():
                    self.lines.append(ln.rstrip())
            del self.lines[:-200]

    def snapshot(self) -> dict:
        with self._lock:
            return {"running": self.running, "ok": self.ok, "lines": list(self.lines)}

    def start(self) -> bool:
        if self.running:
            return False
        with self._lock:
            self.lines = []
        self.ok = None
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self) -> None:
        try:
            import napcat_setup
            napcat_setup.download(settings.napcat_dir(), on_progress=self.add)
            self.ok = True
        except Exception as e:                          # noqa: BLE001
            self.ok = False
            self.add(f"失败：{e}")
        finally:
            self.running = False


NAPJOB = NapCatJob()


# ------------------------------------------------------------------ 数据源
def sources_info() -> dict:
    """看板「数据源」面板要的一切：开关、当前值、所有候选。

    多个账号时**默认预选一个**（配置里的 > 自动探测到的第一个），
    同时在面板顶上说明"预选的是谁、不对可以换"。
    之前那版故意留空逼用户选，结果每次刷新都像丢了一次选择 —— 反而更难用。
    """
    import datetime as _dt

    qq_uin = str(settings.get("qq", "uin") or "").strip()
    qq_exe = str(settings.get("qq", "exe") or "").strip()
    wx_dir = str(settings.get("wechat", "db_dir") or "").strip()
    cur_db = settings.wechat_db_dir()

    exes = settings.qq_exes()
    uins = settings.qq_uins()
    cands = settings.db_candidates()
    for c in cands:
        c["mtime_txt"] = (
            _dt.datetime.fromtimestamp(c["mtime"]).strftime("%m-%d %H:%M")
            if c["mtime"] else "—"
        )

    hints: list[str] = []
    if not settings.get("qq", "enabled", False):
        hints.append("QQ 读取现在是关着的（默认就这样）。要开就先看完下面那段风险说明，再拨开关")
    if len(cands) > 1 and not wx_dir:
        who = ""
        try:
            who = Path(str(cur_db)).parent.name if cur_db else ""
        except Exception:                               # noqa: BLE001
            who = ""
        hints.append(f"这台机器上有 {len(cands)} 个微信账号，"
                     f"已按最近使用预选 {who or '第一个'} —— 不对就在下面换")
    if len(uins) > 1 and not qq_uin:
        hints.append(f"这台机器登录过 {len(uins)} 个 QQ 号，已预选最近用的 {uins[0]} —— 不对就在下面换")
    if qq_uin and uins and qq_uin not in uins:
        hints.append(f"QQ 号 {qq_uin} 这台机器没登录过，要重新扫码")
    if not exes:
        hints.append("没找到 QQ.exe —— 在下面填一下 QQ 装在哪")
    if not cands:
        hints.append("没找到微信数据目录 —— 先正常登录一次微信，再从下面手动选文件夹")

    return {
        "qq": {
            "enabled": bool(settings.get("qq", "enabled", False)),
            "uin": qq_uin, "uin_list": uins,
            "default": qq_uin or (uins[0] if uins else ""),
            "exe": qq_exe, "exe_list": exes,
            "napcat": str(settings.napcat_dir()),
        },
        "wechat": {
            "enabled": bool(settings.get("wechat", "enabled", True)),
            "db_dir": wx_dir, "current": str(cur_db or ""),
            "default": wx_dir or str(cur_db or ""),
            "candidates": cands,
        },
        "hints": hints,
    }


def _clean_path(p: str) -> str:
    """统一成 Windows 反斜杠，顺手把 `H://wx//x` 这种重复分隔符压掉。

    路径可能是手粘的、也可能经过一层 shell，正斜杠和双斜杠都见过 ——
    config.json 里存成 `H://wx//...` 虽然还能用（Path 会归一），但显示出来像坏了。
    """
    if not p:
        return ""
    try:
        return str(Path(p))
    except (OSError, ValueError):
        return p


def save_sources(body: dict) -> dict:
    """保存用户的选择。只动 qq/wechat 里的几个键，别的配置原样保留。"""
    patch: dict = {}
    qq = body.get("qq") or {}
    wx = body.get("wechat") or {}

    if "enabled" in qq:
        patch.setdefault("qq", {})["enabled"] = bool(qq["enabled"])
    if "uin" in qq:
        uin = str(qq.get("uin") or "").strip()
        if uin and not uin.isdigit():
            return {"ok": False, "error": "QQ 号只能是数字"}
        patch.setdefault("qq", {})["uin"] = uin
    if "exe" in qq:
        exe = str(qq.get("exe") or "").strip()
        if exe and not Path(exe).exists():
            return {"ok": False, "error": f"找不到 {exe}"}
        patch.setdefault("qq", {})["exe"] = _clean_path(exe)

    if "enabled" in wx:
        patch.setdefault("wechat", {})["enabled"] = bool(wx["enabled"])
    if "db_dir" in wx:
        d = str(wx.get("db_dir") or "").strip()
        if d and not (Path(d) / "message").exists():
            return {"ok": False, "error": f"{d} 下面没有 message 目录，不像是微信数据目录"}
        patch.setdefault("wechat", {})["db_dir"] = _clean_path(d)

    if not patch:
        return {"ok": False, "error": "没有要保存的内容"}
    try:
        settings.save(patch)
    except OSError as e:
        return {"ok": False, "error": f"写 config.json 失败：{e}"}
    return {"ok": True, "info": sources_info()}


# ------------------------------------------------------------------ 体检
def _ver_pair(text: str) -> tuple[str, int]:
    """'9.9.36-53644' → ('9.9.36-53644', 53644)。构建号用来比新旧。"""
    t = (text or "").strip()
    m = re.search(r"-(\d+)$", t)
    return t, (int(m.group(1)) if m else 0)


def _qq_version() -> tuple[str, int]:
    """本机 QQ 实际版本。NT 版写在 versions/config.json 的 curVersion 里。"""
    roots = [Path(settings.qq_exe()).parent, Path(r"C:\Program Files\Tencent\QQNT"),
             Path(r"C:\Program Files (x86)\Tencent\QQNT")]
    for base in roots:
        cfg = base / "versions" / "config.json"
        if not cfg.exists():
            continue
        try:
            d = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        v = str(d.get("curVersion") or d.get("baseVersion") or "")
        if v:
            return _ver_pair(v)
    return "", 0


def doctor() -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, level: str = "warn",
            fix: str | None = None) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail,
                       "level": "ok" if ok else level, "fix": None if ok else fix})

    v = sys.version_info
    add("Python", v >= (3, 10), f"{sys.version.split()[0]}　{sys.executable}", "fail")

    try:
        import websockets
        add("websockets", True, getattr(websockets, "__version__", "已安装"))
    except ImportError:
        add("websockets", False, "未安装 —— 点下面的链接自动装", "fail", fix="deps")

    if settings.CONFIG_FILE.exists():
        add("config.json", True, str(settings.CONFIG_FILE))
    else:
        add("config.json", False, "还没有（用默认配置也能跑，想改参数就建一个）")

    if settings.get("qq", "enabled", False):
        nd = settings.napcat_dir()
        ok = nd.exists() and (nd / "NapCatWinBootMain.exe").exists()
        if ok:
            add("NapCat", True, str(nd))
        else:
            add("NapCat", False,
                f"没找到 {nd}（只影响 QQ；微信不受影响）", fix="napcat")
        uin = str(settings.get("qq", "uin") or "").strip()
        add("QQ 号", bool(uin), uin or "未填 —— 不填就只能手动开 QQ 再抓")
        exe = settings.qq_exe()
        add("QQ.exe", Path(exe).exists(), exe)

        # QQ 版本对不上是「加载失败」最常见的原因，但又不该拦着不让用，所以报 warn
        qv, qb = _qq_version()
        if not qv:
            add("QQ 版本", True, "没读到（QQ 不是 NT 版，或装在非常规路径）")
        elif qb and qb < 40768:
            add("QQ 版本", False,
                f"{qv} 太旧 —— NapCat 要求 QQNT ≥ 9.9.27（构建 40768 以上），"
                f"请先升级 QQ", "warn")
        else:
            add("QQ 版本", True,
                f"{qv}　（QQ 刚自动升级过、又突然抓不到，先更新 NapCat 再试）")
    else:
        add("QQ 侧", True,
            "默认关闭 —— 想用就去「数据源」打开（开之前先看那段风险说明和出问题怎么办）")

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
        add("微信侧", True, "已关闭 —— 看板右上角「数据源」里可以打开")

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
        elif path == "/api/sources":
            self._json(200, sources_info())
        elif path == "/api/refresh":
            self._json(200, JOB.snapshot())
        elif path == "/api/napcat":
            self._json(200, NAPJOB.snapshot())
        elif path == "/api/update":
            self._json(200, update_check.check(force="force=1" in query))
        elif path == "/api/update/apply":
            self._json(200, UPD.snapshot())
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
        elif path == "/api/napcat":
            started = NAPJOB.start()
            self._json(200, {"ok": started, "already": not started})
        elif path == "/api/install":
            self._json(200, install_deps())
        elif path == "/api/sources":
            self._json(200, save_sources(body if isinstance(body, dict) else {}))
        elif path == "/api/days":
            n = body.get("days") if isinstance(body, dict) else None
            try:
                n = int(n)
            except (TypeError, ValueError):
                self._json(400, {"ok": False, "error": "days 要给一个数字"})
                return
            if not 1 <= n <= 365:
                self._json(400, {"ok": False, "error": "天数要在 1~365 之间"})
                return
            settings.save({"days": n})
            self._json(200, {"ok": True, "days": n})
        elif path == "/api/update/apply":
            started = UPD.start()
            self._json(200, {"ok": started, "already": not started})
        elif path == "/api/update/ignore":
            self._json(200, update_check.ignore())
        elif path == "/api/restart":
            self._json(200, {"ok": True})
            # 必须非守护线程：shutdown() 一返回，main() 就走到结尾，
            # 守护线程会被直接掐死，"起新进程"那半句根本来不及执行。
            threading.Thread(target=_restart_server, daemon=False).start()
        elif path == "/api/quit":
            self._json(200, {"ok": True})
            threading.Thread(target=_shutdown_server, daemon=True).start()
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
    global _HTTPD
    _HTTPD = httpd

    # 实际用了哪个端口写下来：启动器靠它判断"是否已经在跑"，不必盲扫
    pf = settings.data_dir() / "board.port"
    try:
        pf.write_text(str(port), encoding="utf-8")
    except OSError:
        pass

    try:
        print(f"小贾AI群看板已启动：{url}", flush=True)
        print("服务在后台跑；要停就点页面最下方的「停止看板服务」。", flush=True)
    except Exception:                                   # noqa: BLE001  没有控制台就算了
        pass
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:                               # noqa: BLE001
            pass

    try:                                    # 每天一次 GitHub 心跳，后台跑，不挡界面
        threading.Thread(target=_heartbeat, daemon=True, name="heartbeat").start()
    except Exception:                                   # noqa: BLE001
        pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        if not _RESTARTING:                 # 重启时新进程已经接手，别把它写的端口文件删了
            httpd.server_close()
            try:
                pf.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
