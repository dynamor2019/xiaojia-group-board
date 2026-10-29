# -*- coding: utf-8 -*-
"""看板状态：已完成标记 + 取消关注的群。

存成一个 JSON 文件，让「浏览器（经本地服务）」和「爬虫」共用同一份真相：
  * 看板点「取消关注」→ 服务写文件
  * 爬虫开跑前读文件 → 直接跳过这些群，不扫描

结构：
{
  "done":  ["<消息稳定id>", ...],
  "muted": [{"src": "微信", "gid": "123@chatroom", "name": "某群"}, ...]
}
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import settings

BASE = Path(__file__).resolve().parent
STATE_FILE = settings.data_dir() / "board_state.json"

_LOCK = threading.Lock()
_DEFAULT = {"done": [], "muted": []}


def muted_key(src: str, gid: str) -> str:
    return f"{src}|{gid}"


def load() -> dict:
    with _LOCK:
        if not STATE_FILE.exists():
            return {k: list(v) for k, v in _DEFAULT.items()}
        try:
            d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {k: list(v) for k, v in _DEFAULT.items()}
        out = {}
        out["done"] = [str(x) for x in (d.get("done") or [])]
        muted = []
        for m in d.get("muted") or []:
            if isinstance(m, dict) and m.get("gid"):
                muted.append({
                    "src": str(m.get("src") or ""),
                    "gid": str(m.get("gid")),
                    "name": str(m.get("name") or m.get("gid")),
                })
            elif isinstance(m, str) and "|" in m:      # 兼容纯 "src|gid" 写法
                s, g = m.split("|", 1)
                muted.append({"src": s, "gid": g, "name": g})
        out["muted"] = muted
        return out


def save(state: dict) -> dict:
    data = {
        "done": [str(x) for x in (state.get("done") or [])],
        "muted": [
            {"src": str(m.get("src") or ""), "gid": str(m.get("gid")),
             "name": str(m.get("name") or m.get("gid"))}
            for m in (state.get("muted") or []) if m.get("gid")
        ],
    }
    with _LOCK:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    return data


def muted_set(state: dict | None = None) -> set[str]:
    """返回 {"src|gid"} 集合，供爬虫快速判断。"""
    st = state if state is not None else load()
    return {muted_key(m["src"], m["gid"]) for m in st.get("muted", [])}


def is_muted(src: str, gid: str, state: dict | None = None) -> bool:
    return muted_key(src, str(gid)) in muted_set(state)


def muted_names(state: dict | None = None) -> set[str]:
    """按群名匹配的兜底集合（QQ 侧偶尔拿不到 gid 时用）。"""
    st = state if state is not None else load()
    return {m["name"] for m in st.get("muted", []) if m.get("name")}
