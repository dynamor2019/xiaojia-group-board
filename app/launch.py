# -*- coding: utf-8 -*-
"""启动入口 —— 由根目录的「启动看板.cmd」调用，一般不用手动跑。

    python app/launch.py           启动看板（默认）
    python app/launch.py board     同上
    python app/launch.py update    命令行跑一次抓取（给任务计划用）
    python app/launch.py check     命令行体检
    python app/launch.py napcat    自动下载 / 升级 NapCat（只影响 QQ）

启动时自动做三件事：
  1. 没有 config.json 就从 config.example.json 生成一份
  2. 缺 Python 依赖就自动 pip install
  3. 拉起看板服务并打开浏览器
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

BRAND = "小贾AI群看板"
LINE = "=" * 56

# 让子进程脱离当前控制台：窗口关掉也不影响它活着
if os.name == "nt":
    _DETACH = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
else:                                                   # 非 Windows 只是方便调试
    _DETACH = 0


def say(msg: str = "") -> None:
    # 没有控制台时（任务计划 / pythonw / 被别的进程拉起）sys.stdout 是 None，
    # 直接 print 会抛 AttributeError 把整个启动器打死。
    try:
        print(msg, flush=True)
    except Exception:                                   # noqa: BLE001
        pass


def banner(title: str) -> None:
    say(LINE)
    say(f"  {BRAND} · {title}")
    say(LINE)


def ensure_deps() -> bool:
    """websockets 只在抓 QQ 时用得上，缺了就装；装不上也不拦着开看板。"""
    try:
        import websockets  # noqa: F401
        return True
    except ImportError:
        pass
    req = APP_DIR / "requirements.txt"
    if not req.exists():
        return False
    say("[1/3] 首次运行，安装依赖（只有一个 websockets）…")
    try:
        p = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)],
                           capture_output=True, timeout=300)
        out = (p.stdout + p.stderr).decode("utf-8", "replace").strip()
        if p.returncode == 0:
            say("      依赖装好了")
            return True
        say("      自动安装失败，把下面几行发给懂的人看：")
        for ln in out.splitlines()[-6:]:
            say("      " + ln)
        say("      微信侧不受影响；QQ 侧需要先跑：")
        say(f"      {sys.executable} -m pip install -r app/requirements.txt")
        return False
    except Exception as e:                              # noqa: BLE001
        say(f"      安装出错：{e}")
        return False


# ------------------------------------------------------------------ 看板进程
# 探测本机必须绕开系统代理：开了 Clash / 公司代理的机器上，
# urllib 会把 127.0.0.1 也丢给代理，空闲端口只能等到超时（实测扫一轮 12 秒）。
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _listening(port: int, timeout: float = 0.15) -> bool:
    """有没有人在监听。先问 socket 再发 HTTP：某些机器上连空闲端口要等到超时，
    只对"确实有人"的端口发请求，才能把探测压在一瞬间。"""
    try:
        with socket.socket() as s:
            s.settimeout(timeout)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def _health(port: int, timeout: float = 1.5) -> bool:
    """这个端口上是不是已经跑着我们的看板。"""
    try:
        with _LOCAL.open(f"http://127.0.0.1:{port}/api/health",
                         timeout=timeout) as r:
            return r.status == 200 and b'"ok"' in r.read()
    except Exception:                                   # noqa: BLE001
        return False


def _port_file():
    import settings
    return settings.data_dir() / "board.port"


def running_port(port: int) -> int | None:
    """先信上次写下的端口，再试配置里的，最后扫一小段兜底。"""
    cands: list[int] = []
    pf = _port_file()
    if pf.exists():
        try:
            cands.append(int(pf.read_text(encoding="utf-8").strip()))
        except (ValueError, OSError):
            pass
    for p in [port, port + 1, port + 2, port + 3]:
        if p not in cands:
            cands.append(p)
    for p in cands:
        if _listening(p) and _health(p):
            return p
    return None


def open_board(port: int) -> None:
    try:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}/")
    except Exception:                                   # noqa: BLE001
        pass


def _tail(path: Path, n: int = 12) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [x for x in lines if x.strip()][-n:]


def cmd_board() -> int:
    import settings

    banner("启动")
    if settings.ensure_config(quiet=True):
        say(f"[配置] 已生成 {settings.CONFIG_FILE.name}（想改参数就编辑它）")

    ensure_deps()

    port = int(settings.get("board", "port", 8766))
    want_browser = bool(settings.get("board", "open_browser", True))

    # 已经有一份在后台跑着：直接开浏览器，不再起第二个
    alive = running_port(port)
    if alive:
        say(f"[已在运行] http://127.0.0.1:{alive}/")
        say("      窗口马上关掉，看板不受影响。")
        if want_browser:
            open_board(alive)
        return 0

    logdir = settings.data_dir() / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    log = logdir / "看板.log"
    if log.exists() and log.stat().st_size > 512 * 1024:
        try:
            log.unlink()                                # 超过 512 KB 就重开一份
        except OSError:
            pass
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")

    say("[后台启动] 窗口会自动关掉，服务继续跑 —— 关窗口不影响看板。")
    fh = open(log, "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(APP_DIR / "serve_board.py")],
            cwd=str(settings.ROOT),
            stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
            creationflags=_DETACH, close_fds=True)
    finally:
        fh.close()                                     # 子进程自己握着句柄

    deadline = time.time() + 25
    while time.time() < deadline:
        found = running_port(port)
        if found:
            say(f"[就绪] 看板在 http://127.0.0.1:{found}/")
            say("      这个窗口可以关；要停服务，点页面最下方的「停止看板服务」。")
            return 0
        if proc.poll() is not None:                    # 子进程自己退了 = 起不来
            break
        time.sleep(0.3)

    say("[失败] 服务没起来，日志最后几行：")
    for ln in _tail(log):
        say("      " + ln)
    say(f"      完整日志：{log}")
    return 1


def cmd_update() -> int:
    import settings

    banner("抓取一次")
    rc = 0
    if settings.get("wechat", "enabled", True):
        say("\n--- 微信 ---")
        r = subprocess.run([sys.executable, "-E", "-X", "utf8",
                            str(APP_DIR / "update_wx.py")], cwd=str(APP_DIR))
        rc = r.returncode or rc
    if settings.get("qq", "enabled", False):
        say("\n--- QQ ---")
        r = subprocess.run([sys.executable, "-E", "-X", "utf8",
                            str(APP_DIR / "auto.py"), "--start"], cwd=str(APP_DIR))
        rc = r.returncode or rc
    say()
    if settings.board_file().exists():
        say(f"[完成] 看板：{settings.board_file()}")
    return rc


def cmd_check() -> int:
    banner("环境体检")
    import serve_board

    d = serve_board.doctor()
    for c in d["checks"]:
        mark = "✓" if c["ok"] else ("✕" if c["level"] == "fail" else "!")
        say(f"  {mark} {c['name']}　{c['detail']}")
    say()
    if any(c.get("fix") == "napcat" for c in d["checks"]):
        say("  → NapCat 没装，跑这个自动下（约 34 MB）：")
        say("    python app/launch.py napcat")
        say()
    say("结论：" + ("没问题，双击「启动看板.cmd」即可" if d["ok"]
                  else f"有 {d['fails']} 项待处理"))
    return 0 if d["ok"] else 1


def cmd_napcat() -> int:
    banner("装 NapCat")
    import napcat_setup
    import settings

    nd = settings.napcat_dir()
    if napcat_setup.is_installed(nd):
        say(f"已经装好了：{nd}")
        say("再跑一次这个命令可以升级，config 里已有的登录状态会保留。")
        return 0

    say(f"目标位置：{nd}")
    say("只影响 QQ，微信侧不需要它。开始下载…")
    say()
    try:
        r = napcat_setup.download(nd, on_progress=lambda m: say("  " + m))
    except Exception as e:                              # noqa: BLE001
        say()
        say(f"[失败] {e}")
        say("手动方案：去 github.com/NapNeko/NapCatQQ/releases")
        say("下 NapCat.Shell.zip，把解压出来的文件全部放进：")
        say(f"  {nd}")
        return 1

    say()
    say(f"[完成] NapCat {r['tag']}，解出 {r['files']} 个文件")
    say("下一步：打开看板，点右上角「刷新数据」，QQ 会被自动拉起来。")
    return 0


COMMANDS = {"board": cmd_board, "update": cmd_update,
            "check": cmd_check, "napcat": cmd_napcat}


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")

    if sys.version_info < (3, 10):
        say(f"需要 Python 3.10 或更高版本，当前是 {sys.version.split()[0]}")
        return 2

    cmd = (sys.argv[1] if len(sys.argv) > 1 else "board").strip().lower()
    if cmd in ("-h", "--help", "help"):
        say(f"{BRAND}\n")
        say("  python app/launch.py           启动看板（默认）")
        say("  python app/launch.py update    抓一次")
        say("  python app/launch.py check     体检")
        say("  python app/launch.py napcat    下载 / 升级 NapCat")
        return 0
    if cmd not in COMMANDS:
        say(f"未知命令：{cmd}（可用：{' / '.join(COMMANDS)}）")
        return 2
    return COMMANDS[cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
