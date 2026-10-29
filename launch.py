# -*- coding: utf-8 -*-
"""小贾AI群看板 —— 统一入口。

.cmd 里只写 ASCII，所有中文提示都放在这里，避免批处理编码乱码。

    python launch.py init     生成 config.json 并帮你打开
    python launch.py check    体检：Python / 依赖 / 配置 / 数据目录
    python launch.py board    启动看板服务并打开浏览器
    python launch.py update   跑一次完整更新（微信 + QQ）
    python launch.py demo     用内置示例数据生成一份看板，先看效果
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

BRAND = "小贾AI群看板"
LINE = "=" * 58


def say(msg: str = "") -> None:
    print(msg, flush=True)


def banner(title: str) -> None:
    say(LINE)
    say(f"  {BRAND} · {title}")
    say(LINE)


# --------------------------------------------------------------------- init
FIELD_GUIDE = [
    ("qq.uin", "你的 QQ 号。填了才能「静默启动 → 抓完即退」，不填就只能手动开 QQ"),
    ("qq.napcat_dir", "NapCat 解压目录。留空则用脚本同级的 napcat\\ 文件夹"),
    ("qq.ws_port", "OneBot 反向 WS 端口，默认 8765，一般不用改"),
    ("wechat.db_dir", "微信 db_storage 路径。留空会自动探测，探不到才需要手填"),
    ("days", "看板只显示最近多少天，默认 30"),
    ("board.port", "看板服务端口，默认 8766"),
]


def cmd_init() -> int:
    import settings

    banner("首次配置")
    existed = settings.CONFIG_FILE.exists()
    settings.ensure_config()
    say(("[i] 已存在配置，未覆盖：" if existed else "[OK] 已生成配置文件：")
        + str(settings.CONFIG_FILE))
    say()
    say("需要你填的几项：")
    for k, desc in FIELD_GUIDE:
        say(f"  {k:<18}{desc}")
    say()
    say("填完保存，再双击「体检.cmd」确认环境没问题。")
    try:
        os.startfile(str(settings.CONFIG_FILE))  # type: ignore[attr-defined]
    except Exception:
        say(f"（没能自动打开，请手动编辑 {settings.CONFIG_FILE}）")
    return 0


# -------------------------------------------------------------------- check
def cmd_check() -> int:
    import settings

    banner("环境体检")
    ok = True

    say(f"[Python] {sys.version.split()[0]}  ({sys.executable})")
    if sys.version_info < (3, 10):
        say("  ! 需要 Python 3.10 或更高版本")
        ok = False

    try:
        import websockets  # noqa: F401
        say(f"[依赖]   websockets {getattr(websockets, '__version__', '?')}")
    except ImportError:
        say("[依赖]   缺少 websockets —— 请运行：pip install -r requirements.txt")
        ok = False

    cfg_ok = settings.CONFIG_FILE.exists()
    if cfg_ok:
        say("[配置]   已找到 config.json")
    else:
        say("[配置]   还没有 config.json —— 用默认配置也能跑，")
        say("         想改参数就双击「首次配置.cmd」生成一份")

    days = settings.days()
    say(f"[窗口]   最近 {days} 天")

    if settings.get("qq", "enabled", True):
        nd = settings.napcat_dir()
        uin = str(settings.get("qq", "uin") or "").strip()
        say(f"[QQ]     NapCat {'√ ' + str(nd) if nd.exists() else '× 没找到 ' + str(nd)}")
        say(f"         QQ 号 {'√ ' + uin if uin else '× 未填（只能手动开 QQ）'}")
        if not nd.exists():
            ok = False
    else:
        say("[QQ]     已关闭")

    if settings.get("wechat", "enabled", True):
        db = settings.wechat_db_dir()
        if db:
            n = len(list((db / "message").glob("message_*.db"))) if (db / "message").exists() else 0
            say(f"[微信]   数据目录 √ {db}")
            say(f"         消息库 {n} 个")
        else:
            say("[微信]   × 没找到 db_storage，请在 config.json 里手填 wechat.db_dir")
            ok = False
    else:
        say("[微信]   已关闭")

    st = settings.data_dir() / "board_state.json"
    say(f"[状态]   {st}  {'已存在' if st.exists() else '暂无（首次运行后生成）'}")

    say()
    say("结论：" + ("可以开跑了 → 双击「更新一次.cmd」" if ok else "上面标 × 的还差一点，按提示补上"))
    return 0 if ok else 1


# -------------------------------------------------------------------- board
def cmd_board() -> int:
    import serve_board
    return serve_board.main()


# ------------------------------------------------------------------- update
def cmd_update() -> int:
    banner("完整更新")
    rc = 0

    import settings

    if settings.get("wechat", "enabled", True):
        say("\n--- 微信 ---")
        r = subprocess.run([sys.executable, "-E", "-X", "utf8", "update_wx.py"],
                           cwd=str(HERE))
        if r.returncode != 0:
            rc = r.returncode
            say(f"微信侧返回 {r.returncode}，继续跑 QQ 那一路")
    else:
        say("微信侧已关闭，跳过")

    if settings.get("qq", "enabled", True):
        say("\n--- QQ ---")
        r = subprocess.run([sys.executable, "-E", "-X", "utf8", "auto.py", "--start"],
                           cwd=str(HERE))
        if r.returncode != 0:
            rc = r.returncode
            say(f"QQ 侧返回 {r.returncode}")
    else:
        say("QQ 侧已关闭，跳过")

    board = settings.board_file()
    say()
    if board.exists():
        say(f"[完成] 看板：{board}")
        say("       双击「启动看板.cmd」打开带交互的版本（能记住已完成、能取消关注）")
    else:
        say("[完成] 但还没生成看板，看看上面的报错")
        rc = rc or 1
    return rc


# --------------------------------------------------------------------- demo
def cmd_demo() -> int:
    banner("示例看板")
    say("用 demo/ 里的假数据生成一份看板，不碰你的真实数据。\n")
    import report

    qq = HERE / "demo" / "群公告_示例.json"
    wx = HERE / "demo" / "微信群公告_示例.json"
    if not qq.exists():
        say(f"[错误] 找不到 {qq}")
        return 2

    from datetime import date
    sources = [(qq, "QQ")]
    if wx.exists():
        sources.append((wx, "微信"))
    out = HERE / "demo" / "看板预览.html"
    out.write_text(report.build(date.today(), 30, sources=sources), encoding="utf-8")
    say(f"[OK] {out}  {out.stat().st_size / 1024:.0f} KB")
    try:
        os.startfile(str(out))  # type: ignore[attr-defined]
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------- run
COMMANDS = {
    "init": cmd_init,
    "check": cmd_check,
    "board": cmd_board,
    "update": cmd_update,
    "demo": cmd_demo,
}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    cmd = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    if cmd in ("", "-h", "--help", "help"):
        say(f"{BRAND}\n")
        say("用法：python launch.py <命令>\n")
        say("  init    首次配置（生成 config.json）")
        say("  check   环境体检")
        say("  board   启动看板服务")
        say("  update  跑一次完整更新")
        say("  demo    用示例数据生成看板")
        return 0
    if cmd not in COMMANDS:
        say(f"未知命令：{cmd}（可用：{' / '.join(COMMANDS)}）")
        return 2
    return COMMANDS[cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
