# -*- coding: utf-8 -*-
"""
update_wx.py — 微信侧一键更新：取密钥 → 解密 → 抓 @所有人 → 重出看板

与 QQ 那套「启动→抓→杀掉」完全不同：**微信全程不用退出**。
原因：微信的库在磁盘上，密钥拿到一次就可以离线解密；
      QQ 必须靠 NapCat 注入新进程才抓得到，所以只能抓完就撤。

实际负担很小（实测）：取密钥 ~12s（只在密钥失效时才跑）、解密 ~7s、抓取 ~2s。

安全边界
--------
* 只对 vendor/wcdb-key-tool 做只读内存扫描（PROCESS_VM_READ），不注入、不写内存
* 不改微信安装目录、不碰登录态、不发消息、不联网
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import settings

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor" / "wcdb-key-tool"
TOOL = VENDOR / "wcdb_key_tool_windows.py"

WORK = settings.work_dir()
KEYS = WORK / "all_keys.json"
PLAIN = WORK / "decrypted"
DATA = settings.data_dir()

WX_PROCESS = (settings.get("wechat", "process") or "Weixin.exe").strip()


def log(msg: str) -> None:
    print(msg, flush=True)


def run_tool(args: list[str], timeout: int = 300) -> tuple[int, str]:
    cmd = [sys.executable, "-X", "utf8", str(TOOL), *args]
    r = subprocess.run(cmd, cwd=str(VENDOR), capture_output=True, timeout=timeout)
    out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
    return r.returncode, out


def wechat_running() -> bool:
    r = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {WX_PROCESS}", "/FO", "CSV", "/NH"],
        capture_output=True,
    )
    return WX_PROCESS.encode("utf-8", "ignore") in r.stdout


def decrypt_ok() -> bool:
    """用产物是否齐全判断上次解密是否可用。"""
    probe = PLAIN / "contact" / "contact.db"
    msgs = list(PLAIN.glob("message/message_*.db"))
    return probe.exists() and bool(msgs)


def step_extract(db_dir: Path, force: bool = False) -> bool:
    if KEYS.exists() and not force:
        log("[1/4] 已有密钥文件，直接沿用")
        return True
    if not wechat_running():
        log("[1/4] 微信未运行 —— 但有旧密钥时仍可离线解密")
        return KEYS.exists()
    log("[1/4] 从微信进程内存提取密钥（只读扫描，不注入）…")
    KEYS.parent.mkdir(parents=True, exist_ok=True)
    code, out = run_tool(["extract", "--db-dir", str(db_dir), "--output", str(KEYS)])
    tail = [ln for ln in out.strip().splitlines() if ln.strip()][-3:]
    for ln in tail:
        log("      " + ln.strip())
    if code != 0 or not KEYS.exists():
        log("[1/4] 提取失败")
        return False
    log("[1/4] 密钥就绪")
    return True


def step_decrypt(db_dir: Path, force: bool = False) -> int:
    """每次都重解（只 7 秒），否则定时任务刷不到新消息。返回成功解开的库数。"""
    log("[2/4] 解密数据库…")
    code, out = run_tool(
        ["decrypt", "--db-dir", str(db_dir), "--keys", str(KEYS), "--output", str(PLAIN)]
    )
    ok_n = 0
    for ln in out.strip().splitlines():
        s = ln.strip()
        if "结果:" in s:
            log("      " + s)
            try:
                ok_n = int(s.split("结果:")[1].split("成功")[0].strip())
            except (IndexError, ValueError):
                pass
        elif "解密数据量" in s:
            log("      " + s)
    if code != 0 and not ok_n:
        log("[2/4] 解密失败")
    return ok_n


def step_crawl(days: int) -> dict | None:
    log(f"[3/4] 抓取近 {days} 天群 @所有人 …")
    import wx_crawl

    data, stats = wx_crawl.scan(days)
    wx_crawl.write_outputs(data, days)
    bare = stats.get("bare", 0)
    log(f"      {stats.get('groups_scanned', 0)} 个群 / {stats.get('hits', 0)} 条"
        f"（其中 {bare} 条只喊了 @所有人、上下文也找不到正文；"
        f"跳过 {stats.get('skipped_muted', 0)} 个已取消关注的群）")
    return data


def step_report(days: int) -> None:
    log("[4/4] 重出看板（QQ + 微信合并）…")
    import datetime as _dt

    import report as rp

    srcs = [(rp.pick_src(), "QQ")]
    wx = rp.pick_wx_src()
    if wx:
        srcs.append((wx, "微信"))
    t0 = time.time()
    txt = rp.build(_dt.date.today(), days, sources=srcs)
    out = settings.board_file()

    out.write_text(txt, encoding="utf-8")
    log(f"      {out.name} {out.stat().st_size / 1024:.0f} KB，用时 {time.time()-t0:.1f}s")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="只看最近多少天（默认取 config.json）")
    ap.add_argument("--force", action="store_true", help="强制重新取密钥并重解")
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--crawl-only", action="store_true", help="只重抓并出看板（库已解密时用）")
    a = ap.parse_args()

    days = a.days or settings.days()
    DB_DIR = settings.wechat_db_dir()

    if not settings.get("wechat", "enabled", True):
        log("[跳过] config.json 里 wechat.enabled = false")
        return 0
    if not TOOL.exists():
        log(f"[错误] 缺少 {TOOL}")
        return 2
    if not DB_DIR:
        log("[错误] 没找到微信数据目录。")
        log("      请在 config.json 里把  wechat.db_dir  填成你的 db_storage 路径，")
        log("      一般长这样：D:\\\\xwechat_files\\\\wxid_xxx\\\\db_storage")
        return 2
    log(f"[环境] {DB_DIR}")

    t0 = time.time()

    if a.crawl_only:
        if not decrypt_ok():
            log("[错误] 还没有解密产物，去掉 --crawl-only 重跑")
            return 2
    else:
        if not step_extract(DB_DIR, force=a.force):
            return 3
        n = step_decrypt(DB_DIR)
        if n == 0 and not a.force:
            log("[2/4] 一个库都没解开，密钥可能已失效（换账号/重建库），重新提取…")
            if not step_extract(DB_DIR, force=True):
                return 3
            n = step_decrypt(DB_DIR)
        if n == 0:
            return 3

    if not step_crawl(days):
        return 4
    if not a.no_report:
        step_report(days)

    log(f"[完成] 全程 {time.time()-t0:.1f}s，微信一直在线，未重启、未退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
