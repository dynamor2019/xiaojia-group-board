# -*- coding: utf-8 -*-
"""一键流水线：抓近 N 天 @所有人 → 生成待办清单和看板。

吸取的教训（2026-10-29 事故）：
  NapCat 会拦住 QQ 不让它退出。一旦 QQ 窗口被关掉（点 X），主窗口销毁、进程却被留住，
  就变成「有进程 / 无窗口 / 无托盘」的死局 —— 再点 QQ 图标只会提示「已登录」然后退出。
  所以：默认「跑完就让 QQ 干净退出」，绝不留僵尸。

行为矩阵：
  NapCat 在线              → 直接抓，不碰 QQ
  QQ 在跑但 NapCat 不在线  → 跳过，不碰（可能是你正常的 QQ，也可能是失控实例）
  QQ 没在跑 + --start      → 用 -q 快速登录拉起 → 抓 → 退出
  QQ 没在跑 + 无 --start   → 跳过

用法：
    python auto.py --start          # 拉起 QQ 抓一次，抓完退出（自动化默认）
    python auto.py                  # 只在 NapCat 已在线时抓
    python auto.py --start --keep   # 抓完不退出（想常驻时用）
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import os
import subprocess
import sys
import time
from pathlib import Path

import settings

BASE = Path(__file__).resolve().parent
NAPCAT = settings.napcat_dir()
LOGDIR = settings.data_dir() / "logs"
PY = sys.executable
QQ_EXE = settings.qq_exe()

UIN = str(settings.get("qq", "uin") or "").strip()
WS_HOST, WS_PORT = "127.0.0.1", int(settings.get("qq", "ws_port", 8765))

_logf = None

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# -E 会屏蔽 PYTHONIOENCODING，所以子进程要靠命令行 -X utf8 强制 UTF-8
CHILD_ENV = os.environ.copy()
CHILD_ENV["PYTHONUTF8"] = "1"
CHILD_ENV["PYTHONIOENCODING"] = "utf-8"


def log(msg: str) -> None:
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    if _logf:
        _logf.write(line + "\n")
        _logf.flush()


def open_log() -> Path:
    LOGDIR.mkdir(exist_ok=True)
    p = LOGDIR / f"run_{dt.datetime.now():%Y-%m-%d_%H%M}.log"
    global _logf
    _logf = open(p, "w", encoding="utf-8")
    return p


def _dec(b: bytes | None) -> str:
    b = b or b""
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


# ------------------------------------------------------------------ QQ 状态
def qq_pids() -> list[int]:
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/NH", "/FO", "CSV"],
                           capture_output=True, timeout=20)
        out = _dec(r.stdout)
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        cells = [c.strip('"') for c in line.split('","')]
        if len(cells) > 1 and cells[0].lower() == "qq.exe":
            try:
                pids.append(int(cells[1]))
            except ValueError:
                pass
    return pids


def qq_has_window() -> bool:
    """有没有「可见」的主窗口。进程在但没窗口 = 僵尸态。"""
    pids = set(qq_pids())
    if not pids:
        return False
    try:
        u = ctypes.windll.user32
    except Exception:
        return True  # 判断不了就当正常，避免误判
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and u.IsWindowVisible(hwnd):
            if u.GetWindowTextLengthW(hwnd) > 0:
                found.append(hwnd)
        return True

    u.EnumWindows(cb, 0)
    return bool(found)


# ------------------------------------------------------------------ NapCat
def napcat_alive(timeout: float = 8.0) -> bool:
    """起个临时 WS 服务，看 NapCat 会不会连上来。"""
    try:
        from websockets.asyncio.server import serve
    except ImportError:
        from websockets.server import serve

    state = {"ok": False}

    async def handler(ws):
        state["ok"] = True
        try:
            await ws.wait_closed()
        except Exception:
            pass

    async def probe():
        try:
            server = await serve(handler, WS_HOST, WS_PORT, max_size=None, ping_interval=20)
        except OSError:
            return False  # 端口被占：另一个抓取进程在跑
        try:
            deadline = time.time() + timeout
            while time.time() < deadline and not state["ok"]:
                await asyncio.sleep(0.25)
            return state["ok"]
        finally:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass

    try:
        return asyncio.run(probe())
    except Exception:
        return False


def start_qq(uin: str = UIN):
    """拉起 QQ，返回启动器 PID（退出时按这棵树收，不会误伤别的 QQ）。"""
    launcher = NAPCAT / "NapCatWinBootMain.exe"
    inject = NAPCAT / "NapCatWinBootHook.dll"
    if not launcher.exists() or not inject.exists() or not Path(QQ_EXE).exists():
        log(f"  ! 启动所需文件缺失：{launcher} / {inject} / {QQ_EXE}")
        return None

    env = os.environ.copy()
    env.update({
        "NAPCAT_PATCH_PACKAGE": str(NAPCAT / "qqnt.json"),
        "NAPCAT_LOAD_PATH": str(NAPCAT / "loadNapCat.js"),
        "NAPCAT_INJECT_PATH": str(inject),
        "NAPCAT_LAUNCHER_PATH": str(launcher),
        "NAPCAT_MAIN_PATH": str(NAPCAT / "napcat.mjs"),
    })
    cmd = [str(launcher), QQ_EXE, str(inject)]
    if uin:
        cmd += ["-q", uin]
    try:
        proc = subprocess.Popen(cmd, cwd=str(NAPCAT), env=env,
                                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except Exception as e:
        log(f"  ! 启动失败: {e}")
        return None
    log(f"  已拉起 QQ（快速登录 {uin or '无'}），启动器 PID={proc.pid}")
    return proc.pid


def exit_qq(launcher_pid: int | None) -> bool:
    """关掉自己拉起来的 QQ。

    NapCat 的 bot_exit 只会重启插件、不会退 QQ；QQ 又是 -q 无声登录（没窗口），
    所以没有可发 WM_CLOSE 的目标。只能按启动器这一棵树强制收，且只收这一棵。
    """
    if not launcher_pid:
        return True
    log(f"  结束本次拉起的 QQ（启动器 PID={launcher_pid} 整棵树）...")
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(launcher_pid)],
                       capture_output=True, timeout=30)
    except Exception as e:
        log(f"  ! taskkill 失败: {e}")
    for _ in range(15):
        time.sleep(1)
        if not qq_pids():
            log("  QQ 已退出，没留僵尸")
            return True
    # 启动器没了但 QQ 子进程还在：兜底按镜像名收（此时机器上只可能有我们拉起的这份）
    if not launcher_alive(launcher_pid):
        try:
            subprocess.run(["taskkill", "/F", "/T", "/IM", "QQ.exe"],
                           capture_output=True, timeout=30)
        except Exception:
            pass
        time.sleep(2)
        if not qq_pids():
            log("  QQ 已退出（兜底清理）")
            return True
    log("  ! QQ 仍未退出，请手动关掉")
    return False


def launcher_alive(pid: int) -> bool:
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, timeout=15)
        return "NapCatWinBootMain" in _dec(r.stdout) or str(pid) in _dec(r.stdout)
    except Exception:
        return False


# ------------------------------------------------------------------ 子任务
def run(cmd: list[str], timeout: int, title: str) -> bool:
    log(f"$ {title}")
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=str(BASE), capture_output=True, timeout=timeout, env=CHILD_ENV)
    except subprocess.TimeoutExpired:
        log(f"  ! 超时（>{timeout}s）")
        return False
    out, err = _dec(p.stdout), _dec(p.stderr).strip()
    for ln in [x for x in out.strip().splitlines() if x.strip()][-14:]:
        log("  " + ln)
    if err:
        for ln in err.splitlines()[-8:]:
            log("  ! " + ln)
    ok = p.returncode == 0
    log(f"  {'完成' if ok else '失败'} rc={p.returncode} 用时 {time.time() - t0:.0f}s")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--pages", type=int, default=15)
    ap.add_argument("--start", action="store_true", help="QQ 没在跑时主动拉起")
    ap.add_argument("--keep", action="store_true", help="抓完不退出 QQ（默认跑完即退）")
    ap.add_argument("--open", action="store_true", help="跑完打开看板")
    ap.add_argument("--delay", type=int, default=0)
    a = ap.parse_args()

    lp = open_log()
    log("===== 群公告自动流水线开始 =====")
    log(f"日志：{lp}")
    if a.delay > 0:
        log(f"等待 {a.delay}s ...")
        time.sleep(a.delay)

    pids = qq_pids()
    has_win = qq_has_window()
    alive = napcat_alive(6 if pids else 2)
    log(f"QQ 进程 {len(pids)} 个 ｜ 主窗口 {'有' if has_win else '无'} ｜ NapCat {'在线' if alive else '离线'}")

    launched = False
    launcher_pid = None
    if alive:
        log("[1/4] NapCat 在线，直接抓（不动你的 QQ）")
    elif pids:
        if not has_win:
            log("[1/4] QQ 在跑但没有任何可见窗口 = 失控状态（僵尸），本次跳过、不碰它。")
            log("      处理：手动关掉 QQ 再重开；或看板群里的说明。")
        else:
            log("[1/4] QQ 在跑但不是 NapCat 模式，跳过（不干扰你正在用的 QQ）")
            log("      想让 QQ 也刷新：退出 QQ，再点一次刷新。")
        return 0            # 主动跳过不算失败
    elif a.start:
        if not UIN:
            log("[1/4] 还没配置 QQ 号，无法快速登录。")
            log("      请在 config.json 里填写  qq.uin = \"你的QQ号\"  后重试。")
            return 6
        log("[1/4] QQ 没在跑，拉起")
        launcher_pid = start_qq()
        if not launcher_pid:
            return 2
        launched = True
        time.sleep(8)
    else:
        log("[1/4] QQ 没在跑，且未指定 --start，跳过")
        return 0            # 没让启动就是跳过，同样不算失败

    ok = run([PY, "-E", "-X", "utf8", "crawl_ws.py",
              "--days", str(a.days), "--pages", str(a.pages)],
             timeout=900, title=f"抓取近 {a.days} 天 @所有人")
    if not ok:
        log("抓取失败 —— 不重新生成看板，避免旧数据冒充新数据")
        if launched and not a.keep:
            exit_qq(launcher_pid)
        return 3

    run([PY, "-E", "-X", "utf8", "digest.py"], timeout=120, title="生成待办提醒")
    run([PY, "-E", "-X", "utf8", "report.py"], timeout=120, title="生成看板")

    board = settings.board_file()

    if board.exists():
        log(f"看板：{board}")
        if a.open:
            try:
                os.startfile(str(board))
            except Exception as e:
                log(f"  ! 打开失败: {e}")

    if launched and not a.keep:
        exit_qq(launcher_pid)
    elif not launched:
        log("QQ 是你自己的实例，保持不动")

    log("===== 全部完成 =====")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:
        log(f"!! 未捕获异常: {type(e).__name__}: {e}")
        rc = 1
    if _logf:
        _logf.close()
    sys.exit(rc)
