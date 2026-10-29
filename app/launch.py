# -*- coding: utf-8 -*-
"""启动入口 —— 由根目录的「启动看板.cmd」调用，一般不用手动跑。

    python app/launch.py           启动看板（默认）
    python app/launch.py board     同上
    python app/launch.py update    命令行跑一次抓取（给任务计划用）
    python app/launch.py check     命令行体检

启动时自动做三件事：
  1. 没有 config.json 就从 config.example.json 生成一份
  2. 缺 Python 依赖就自动 pip install
  3. 拉起看板服务并打开浏览器
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

BRAND = "小贾AI群看板"
LINE = "=" * 56


def say(msg: str = "") -> None:
    print(msg, flush=True)


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


def cmd_board() -> int:
    import settings

    banner("启动")
    if settings.ensure_config(quiet=True):
        say(f"[配置] 已生成 {settings.CONFIG_FILE.name}（想改参数就编辑它）")

    ensure_deps()

    say(f"[数据] {settings.data_dir()}")
    say(f"[看板] {settings.board_file().name}")
    say()

    import serve_board
    return serve_board.main()


def cmd_update() -> int:
    import settings

    banner("抓取一次")
    rc = 0
    if settings.get("wechat", "enabled", True):
        say("\n--- 微信 ---")
        r = subprocess.run([sys.executable, "-E", "-X", "utf8",
                            str(APP_DIR / "update_wx.py")], cwd=str(APP_DIR))
        rc = r.returncode or rc
    if settings.get("qq", "enabled", True):
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
    say("结论：" + ("没问题，双击「启动看板.cmd」即可" if d["ok"]
                  else f"有 {d['fails']} 项待处理"))
    return 0 if d["ok"] else 1


COMMANDS = {"board": cmd_board, "update": cmd_update, "check": cmd_check}


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
        return 0
    if cmd not in COMMANDS:
        say(f"未知命令：{cmd}（可用：{' / '.join(COMMANDS)}）")
        return 2
    return COMMANDS[cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
