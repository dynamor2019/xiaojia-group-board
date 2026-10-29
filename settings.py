# -*- coding: utf-8 -*-
"""统一配置入口 —— 所有人都只需要改 config.json，不用动代码。

首次运行时如果找不到 config.json，会从 config.example.json 复制一份出来并提示修改。

优先级：config.json 里的值 > 自动探测 > 内置默认值。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"
EXAMPLE_FILE = BASE / "config.example.json"

DEFAULTS: dict = {
    "days": 30,                      # 只看最近多少天
    "qq": {
        "enabled": True,
        "uin": "",                   # 你的 QQ 号，quick 登录用
        "napcat_dir": "",            # NapCat 解压目录；留空则用上一级的 napcat/
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
        "data_dir": "",              # 抓取结果放哪；留空=脚本目录
        "work_dir": "",              # 解密中间产物放哪；留空=vendor/wcdb-key-tool
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


def get(section: str, key: str, default=None):
    return (load().get(section) or {}).get(key, default)


def days() -> int:
    return int(load().get("days") or 30)


def base_dir() -> Path:
    return BASE


def napcat_dir() -> Path:
    """NapCat 目录：config 里优先，否则找同级的 napcat/。"""
    p = (get("qq", "napcat_dir") or "").strip()
    if p:
        return Path(p).expanduser()
    return BASE / "napcat"


def data_dir() -> Path:
    """抓取结果（群公告_*.json / 看板 html）存放目录。"""
    p = (get("paths", "data_dir") or "").strip()
    return Path(p).expanduser() if p else BASE


def board_file() -> Path:
    """看板 HTML 的落盘路径（与 data_dir 保持一致）。"""
    return data_dir() / "小贾AI群看板.html"


def work_dir() -> Path:
    """解密中间产物（密钥、明文库）存放目录。"""
    p = (get("paths", "work_dir") or "").strip()
    return Path(p).expanduser() if p else BASE / "vendor" / "wcdb-key-tool"


def wechat_db_dir() -> Path | None:
    """微信数据目录：config 优先，否则自动探测 xwechat_files/<账号>/db_storage。"""
    p = (get("wechat", "db_dir") or "").strip()
    if p:
        d = Path(p).expanduser()
        return d if d.exists() else None
    return detect_db_dir()


def _wx_data_root() -> Path | None:
    """微信把数据根目录记在 %APPDATA%\\Tencent\\xwechat\\config\\*.ini 里（内容就是一行路径）。"""
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
    root = _wx_data_root()
    if root:
        candidates = list(root.glob("xwechat_files/*/db_storage"))
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


def section(name: str) -> dict:
    return load().get(name) or {}


if __name__ == "__main__":
    ensure_config()
    print(json.dumps(load(), ensure_ascii=False, indent=2))
