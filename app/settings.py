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
    candidates: list[Path] = []
    wx_root = _wx_data_root()
    if wx_root:
        candidates = list(wx_root.glob("xwechat_files/*/db_storage"))
    if not candidates:
        for base in (Path.home() / "xwechat_files",
                     Path.home() / "Documents" / "xwechat_files"):
            if base.exists():
                candidates += list(base.glob("*/db_storage"))
    best, best_mtime = None, -1.0
    for d in candidates:
        msg = d / "message"
        if not msg.exists():
            continue
        for f in msg.glob("message_*.db"):
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if m > best_mtime:
                best, best_mtime = d, m
    return best


if __name__ == "__main__":
    ensure_config()
    print(json.dumps(load(), ensure_ascii=False, indent=2))
    print("data_dir:", data_dir())
    print("work_dir:", work_dir())
    print("board   :", board_file())
