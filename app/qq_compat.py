# -*- coding: utf-8 -*-
"""QQ 版本 × NapCat 兼容性预检。

为什么需要这个模块（2026-11-11 的事故）：
    QQ 在 09:46 通过热更新从 9.9.22 升到 9.9.37-54395。NapCat 4.18.41 的
    偏移表只到 9.9.36-53489 —— 没有这个 build。于是注入静默失败：
    QQ 正常起来了（用户看到的是普通 QQ），但 NapCat 从来没加载，
    ws://127.0.0.1:8765 一直没人连。脚本只能干等 120 秒，然后报一句
    「等了 120 秒 NapCat 仍未连接」—— 这句话等于没说，用户还以为是
    「需要重新安装 QQ」。

    NapCat 的工作方式决定了这件事没法在本地绕过去：它要往 QQ 的进程里
    按**精确到 build 号的偏移**打补丁（napcat.mjs 里那张
    `"9.9.36-53489-x64": {send:"C3ED0F", recv:"24A6235"}` 表）。
    build 号差一个数字，偏移就全错 —— 这不是"装坏了"，是上游还没跟上。

所以：抓之前先比对。不匹配就两秒钟内说清楚，别让用户等两分钟还看不懂。

为什么不写死版本号：直接读本地 napcat.mjs 里的那张表。用户升级 NapCat
之后表会自动变长，预检跟着放行，不需要改这个文件。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import settings

# napcat.mjs 里的偏移表长这样：
#   "9.9.36-53489-x64": { send: "C3ED0F", recv: "24A6235" },
# 只认 9.9.x 的 Windows x64 条目（安卓/linux/arm64 对这里没意义）
_ROW = re.compile(r'"(9\.9\.\d+-\d+)-(x64|arm64)"\s*:\s*\{')


def qq_dir() -> Path:
    """QQ 安装根目录（settings.qq_exe() 是 ...\\QQNT\\QQ.exe）。"""
    return Path(settings.qq_exe()).parent


def qq_version() -> str:
    """当前 QQ 的完整版本，形如 `9.9.37-54395`。

    真源是 `QQNT/versions/config.json` 里的 curVersion ——
    resources/app/package.json 也能读到版本，但那是"壳"的版本，
    热更新之后它跟实际跑起来的 build 不一定同步。
    """
    cfg = qq_dir() / "versions" / "config.json"
    try:
        d = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return ""
    v = str(d.get("curVersion") or d.get("baseVersion") or "").strip()
    if v:
        return v
    # 兜底：目录名就是版本号（versions/9.9.37-54395/）
    try:
        subs = [p.name for p in (qq_dir() / "versions").iterdir() if p.is_dir()]
    except Exception:
        return ""
    subs = [s for s in subs if re.fullmatch(r"\d+\.\d+\.\d+-\d+", s)]
    return sorted(subs)[-1] if subs else ""


def table(text: str) -> set[str]:
    """从 napcat.mjs 的源码文本里抽出偏移表认的版本（不带 -x64）。

    单独拎出来是为了让「还没装上的新版本」也能查：自愈模块下载完 zip，
    先读包里的 napcat.mjs 判断它到底认不认这个 QQ，再决定要不要覆盖安装。
    """
    return {m.group(1) for m in _ROW.finditer(text)}


def napcat_known(napcat_dir: Path) -> tuple[set[str], str]:
    """NapCat 内置偏移表认可的 QQ 版本集合（不带 -x64 后缀）。

    返回 (集合, 错误说明)。读不到就返回空集合 —— 空集合意味着"不知道"，
    调用方要按"不拦"处理，不能因为读不到文件就把抓取挡死。
    """
    f = Path(napcat_dir) / "napcat.mjs"
    if not f.exists():
        return set(), f"找不到 {f}"
    try:
        txt = f.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return set(), f"读不了 {f}：{e}"
    known = table(txt)
    if not known:
        return set(), f"{f} 里没有偏移表（版本可能不对）"
    return known, ""


def _newest(known: set[str]) -> str:
    def key(s: str):
        a, b = s.split("-")
        return tuple(int(x) for x in a.split(".")), int(b)
    return max(known, key=key) if known else ""


def check(napcat_dir: Path | None = None) -> dict:
    """比对 QQ 版本和 NapCat 的偏移表。

    返回：
        ok        True 兼容 / False 不兼容 / None 判断不了（读不到就别拦）
        qq        QQ 版本
        newest    本地 NapCat 认识的最新 QQ 版本
        known_n   表里一共几条
        reason    给用户看的一句话
    """
    nd = Path(napcat_dir) if napcat_dir else Path(settings.napcat_dir())
    qv = qq_version()
    known, err = napcat_known(nd)
    out = {"ok": None, "qq": qv, "newest": _newest(known),
           "known_n": len(known), "reason": ""}

    if not qv:
        out["reason"] = "读不到 QQ 的版本号（QQ 可能没装在默认位置）"
        return out
    if not known:
        # 表读不出来，属于"我们没有能力判断"，不能据此拦人
        out["reason"] = err or "读不到 NapCat 的偏移表"
        return out

    if qv in known:
        out["ok"] = True
        return out

    out["ok"] = False
    newest = out["newest"]
    # 分两种说法：比表里最新的还新（QQ 抢先升级）／比表里最旧的还旧（QQ 被降级）
    def verkey(s: str):
        a, b = s.split("-")
        return (tuple(int(x) for x in a.split(".")), int(b))

    if newest and verkey(qv) > verkey(newest):
        # 别报 build 差值：build 号不连续，差 751 这种数字只会让人以为要等很久。
        # 说"高一个版本"更准 —— 9.9.36 → 9.9.37 就是一次大版本跳。
        out["reason"] = (
            f"你的 QQ 是 {qv}，NapCat 这张偏移表只认到 {newest}。"
            "QQ 自己热更新上去了，NapCat 还没跟上 —— 不是你装坏了。")
    else:
        out["reason"] = (
            f"你的 QQ 是 {qv}，NapCat 这张偏移表里没有它"
            f"（最新支持 {newest}）。可能是 QQ 被降级或换了通道。")
    return out


if __name__ == "__main__":
    r = check()
    print(json.dumps(r, ensure_ascii=False, indent=2))
