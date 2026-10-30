# -*- coding: utf-8 -*-
"""统一配置 —— 所有人只需要改根目录的 config.json，不用动代码。

目录约定
--------
    <项目根>/            启动看板.cmd、README、config.json、看板 html
    <项目根>/app/        全部代码（这个文件所在）
    <项目根>/data/       抓取结果、状态、日志
    <项目根>/assets/     logo、截图
    <项目根>/demo/       示例数据

优先级：config.json > 自动探测 > 内置默认值。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent          # .../app
ROOT = APP_DIR.parent                              # 项目根
CONFIG_FILE = ROOT / "config.json"
EXAMPLE_FILE = APP_DIR / "config.example.json"

DEFAULTS: dict = {
    "days": 30,                      # 只看最近多少天
    "qq": {
        "enabled": True,
        "uin": "",                   # 你的 QQ 号，快速登录用
        "napcat_dir": "",            # NapCat 解压目录；留空则用根目录下的 napcat/
        "exe": "",                   # QQ.exe 路径；留空自动找常见位置
        "ws_port": 8765,             # OneBot 反向 WS 端口（本机）
        "pages": 15,                 # 每个群最多翻几页
    },
    "wechat": {
        "enabled": True,
        "db_dir": "",                # 留空自动探测 xwechat_files/<账号>/db_storage
        "process": "Weixin.exe",
        "exit_after_crawl": False,   # 微信不需要退出，保持在线即可
    },
    "board": {
        "port": 8766,
        "open_browser": True,
    },
    "update": {
        "enabled": True,             # 每天问一次 GitHub 有没有新版本
        "hours": 24,                 # 心跳间隔（小时）
        "repo": "dynamor2019/xiaojia-group-board",
    },
    "paths": {
        "data_dir": "",              # 抓取结果放哪；留空=data/
        "work_dir": "",              # 解密中间产物放哪；留空=app/vendor/wcdb-key-tool/
    },
}

_cache: dict | None = None


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_config(quiet: bool = False) -> bool:
    """没有 config.json 就生成一份。返回 True 表示这次新建了。"""
    if CONFIG_FILE.exists():
        return False
    if EXAMPLE_FILE.exists():
        CONFIG_FILE.write_text(EXAMPLE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        if not quiet:
            print(f"[提示] 已生成 {CONFIG_FILE.name}，请先填写 QQ 号等信息再运行。")
    else:
        CONFIG_FILE.write_text(
            json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def load(reload: bool = False, quiet: bool = True) -> dict:
    global _cache
    if _cache is not None and not reload:
        return _cache
    ensure_config(quiet=quiet)
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[警告] {CONFIG_FILE.name} 解析失败（{e}），使用默认配置")
        raw = {}
    _cache = _merge(DEFAULTS, raw)
    return _cache


def get(section_name: str, key: str, default=None):
    return (load().get(section_name) or {}).get(key, default)


def section(name: str) -> dict:
    return load().get(name) or {}


def save(patch: dict) -> dict:
    """把 patch 深合并进 config.json 再落盘（不写默认值，不碰别的键）。

    看板上的「数据源」面板用它改 qq/wechat 的 enabled、路径、账号。
    写之前先读盘上的原文，避免把我们不该管的键也一并写下去。
    """
    try:
        cur = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:                                   # noqa: BLE001
        cur = {}
    merged = _merge(cur, patch or {})
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)
    return load(reload=True)


def days() -> int:
    return int(load().get("days") or 30)


# ------------------------------------------------------------------ 路径
def root() -> Path:
    return ROOT


def base_dir() -> Path:
    """兼容旧调用：代码目录。"""
    return APP_DIR


def data_dir() -> Path:
    """抓取结果、状态、日志。"""
    p = (get("paths", "data_dir") or "").strip()
    d = Path(p).expanduser() if p else ROOT / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def work_dir() -> Path:
    """解密中间产物（密钥、明文库）。"""
    p = (get("paths", "work_dir") or "").strip()
    return Path(p).expanduser() if p else APP_DIR / "vendor" / "wcdb-key-tool"


def assets_dir() -> Path:
    return ROOT / "assets"


def demo_dir() -> Path:
    return ROOT / "demo"


def board_file() -> Path:
    """看板 HTML 落在根目录，方便直接双击打开。"""
    return ROOT / "小贾AI群看板.html"


def napcat_dir() -> Path:
    p = (get("qq", "napcat_dir") or "").strip()
    if p:
        return Path(p).expanduser()
    return ROOT / "napcat"


def qq_exe() -> str:
    p = (get("qq", "exe") or "").strip()
    if p and Path(p).exists():
        return p
    for cand in (
        r"C:\Program Files\Tencent\QQNT\QQ.exe",
        r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe",
        str(Path.home() / "AppData/Local/Programs/Tencent/QQNT/QQ.exe"),
    ):
        if Path(cand).exists():
            return cand
    return r"C:\Program Files\Tencent\QQNT\QQ.exe"


# ------------------------------------------------------------ 微信目录探测
def wechat_db_dir() -> Path | None:
    """config 优先，否则自动探测 xwechat_files/<账号>/db_storage。"""
    p = (get("wechat", "db_dir") or "").strip()
    if p:
        d = Path(p).expanduser()
        return d if d.exists() else None
    return detect_db_dir()


def _wx_data_root() -> Path | None:
    """微信把数据根目录记在 %APPDATA%\\Tencent\\xwechat\\config\\*.ini（内容就是一行路径）。"""
    cfg = Path.home() / "AppData" / "Roaming" / "Tencent" / "xwechat" / "config"
    if not cfg.exists():
        return None
    for ini in cfg.glob("*.ini"):
        try:
            txt = ini.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if txt and Path(txt).exists():
            return Path(txt)
    return None


def detect_db_dir() -> Path | None:
    """挑最近还在写入的那个账号的 db_storage（多账号/换号时不会认错）。"""
    cands = db_candidates()
    return Path(cands[0]["path"]) if cands else None


def db_candidates() -> list[dict]:
    """所有能找到的微信数据目录 —— 多账号时让用户自己选，别替他猜。

    优先级：微信自己记录的数据根（`%APPDATA%\\Tencent\\xwechat\\config\\*.ini`）
    高于任何猜测路径。这一步不能省 —— 猜路径里经常躺着换盘前的旧副本，
    按 mtime 比大小会正好选中那份过期的，抓出来一片空。
    """
    roots: list[tuple[Path, bool]] = []
    wx_root = _wx_data_root()
    if wx_root:
        roots.append((wx_root, True))
    for base in (Path.home() / "xwechat_files",
                 Path.home() / "Documents" / "xwechat_files"):
        if base.exists():
            roots.append((base, False))

    out: list[dict] = []
    seen: set[str] = set()
    for root, official in roots:
        found_here = 0
        # ini 里记的是"数据根"（如 H:\wx），下面还有一层 xwechat_files；
        # 但个别版本记的就是 xwechat_files 本身，两种都试
        pats = ["xwechat_files/*/db_storage", "*/db_storage"] if official \
            else ["*/db_storage"]
        dirs: list[Path] = []
        for pat in pats:
            dirs += list(root.glob(pat))
        for d in dirs:
            msg = d / "message"
            if not msg.exists():
                continue
            try:
                key = str(d.resolve()).lower()
            except OSError:
                key = str(d).lower()
            if key in seen:
                continue
            seen.add(key)
            dbs = list(msg.glob("message_*.db"))
            mtime = max((f.stat().st_mtime for f in dbs), default=0.0)
            out.append({
                "path": str(d),
                "account": d.parent.name,          # 一般是 wxid_xxx
                "db_count": len(dbs),
                "mtime": mtime,
                "official": official,
            })
            found_here += 1
        if official and found_here:
            break          # 官方位置有结果，就别拿猜测路径来搅局

    # 同一个号可能在两个位置各有一份（换过盘、备份过），那不算"多个账号"。
    # 按号去重，官方位置优先、其次取最近用过的 —— 否则用户会被问一句莫名其妙的话。
    best: dict[str, dict] = {}
    for c in out:
        k = (c["account"] or "").lower()
        cur = best.get(k)
        if cur is None or (c["official"], c["mtime"]) > (cur["official"], cur["mtime"]):
            best[k] = c
    out = list(best.values())
    out.sort(key=lambda x: (not x["official"], -x["mtime"]))
    return out


# ------------------------------------------------------------ QQ 位置 / 账号
QQ_EXE_HINTS = (
    r"C:\Program Files\Tencent\QQNT\QQ.exe",
    r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe",
    r"D:\Program Files\Tencent\QQNT\QQ.exe",
    r"D:\Tencent\QQNT\QQ.exe",
)


def _qq_from_registry() -> list[str]:
    """注册表里的 QQ 安装位置 —— 装在非常规盘符时只靠猜是不行的。"""
    found: list[str] = []
    try:
        import winreg
    except ImportError:
        return found
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
    )
    for hive, path in keys:
        try:
            with winreg.OpenKey(hive, path) as k:
                for name in ("InstallLocation", "DisplayIcon", "UninstallString"):
                    try:
                        v, _ = winreg.QueryValueEx(k, name)
                    except OSError:
                        continue
                    v = str(v).strip().strip('"')
                    if not v:
                        continue
                    p = Path(v)
                    found.append(str(p if p.suffix.lower() == ".exe" else p / "QQ.exe"))
        except OSError:
            continue
    return found


def qq_exes() -> list[str]:
    """所有可能的 QQ.exe（去重、只留真实存在的），排在前面的是当前配置。"""
    cands: list[str] = []
    p = (get("qq", "exe") or "").strip()
    if p:
        cands.append(p)
    cands += _qq_from_registry()
    cands += list(QQ_EXE_HINTS)
    cands.append(str(Path.home() / "AppData/Local/Programs/Tencent/QQNT/QQ.exe"))

    out: list[str] = []
    seen: set[str] = set()
    for c in cands:
        p = Path(c)
        if p.name.lower() != "qq.exe":      # 注册表里 InstallLocation/DisplayIcon 会甩出 Uninstall.exe
            continue
        try:
            if not p.exists():
                continue
            k = str(p.resolve()).lower()
        except OSError:
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(str(p))
    return out


def qq_uins() -> list[str]:
    """这台机器上用 NapCat 登录过的 QQ 号（每个号一个 napcat_<uin>.json）。"""
    d = napcat_dir() / "config"
    out: list[str] = []
    for f in d.glob("napcat_*.json") if d.exists() else []:
        stem = f.stem
        if "_" not in stem:
            continue
        uin = stem.split("_", 1)[1].strip()
        if uin.isdigit():
            out.append(uin)
    return sorted(set(out))


if __name__ == "__main__":
    ensure_config()
    print(json.dumps(load(), ensure_ascii=False, indent=2))
    print("data_dir:", data_dir())
    print("work_dir:", work_dir())
    print("board   :", board_file())
