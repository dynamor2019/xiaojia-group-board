# -*- coding: utf-8 -*-
"""NapCat 自愈：换上去**真能解决**这次不兼容时，才换。

为什么要有这个模块
------------------
NapCat 是靠「build 号 → 内存偏移」的表来给 QQ 打补丁的。QQ 一热更新，
表里没有新 build，注入就静默失败 —— QQ 正常起来了，但 NapCat 从来没加载，
ws 端口一直没人连（2026-11-11 就是这么栽的：QQ 9.9.37-54395 vs 表里最高 9.9.36-53644）。

这件事没法在本地绕：偏移是二进制里的地址，猜不出来，也没有 pattern scan 兜底。
唯一真正的修法是**让 NapCat 跟上**。而「等用户自己发现、自己去 GitHub 下载、
自己解压覆盖」这条链路，绝大多数人是走不通的 —— 他们只会在三周后说
「这破看板又不动了」。

所以这里做的是：抓之前发现版本对不上时，去 GitHub 问一句最新版，
把它的偏移表读出来**先验一遍**：

- 表里有你这个 build → 下载、备份、覆盖、重读校验，这次就能抓；
- 表里还是没有 → 什么都不做，临时文件删掉，明确告诉你「最新的 x.y.z 也只认到 a.b.c」。

关键在「先验后换」：不是看到有新版本就升。升完还是抓不到，等于白折腾一遍，
还可能把本来能用的环境弄坏。

安全边界
--------
只写 `<项目根>/napcat` 下的程序文件。`config/`（里面是 OneBot 端口和 token）、
`cache/`、`logs/`、`*.db*` 一律不动 —— 那些是用户的数据，不是软件本体。
覆盖前把每个要动的文件先拷到 `data/backup/napcat-<tag>-<时间>/`，坏了能退回。
"""
from __future__ import annotations

import json
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import qq_compat

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
UA = {"User-Agent": "xiaojia-board", "Accept": "application/vnd.github+json"}
STATE = ROOT / "data" / "napcat_update.json"

# 这些目录整个跳过：里面是运行态配置和缓存，覆盖了用户就得重新配 token
KEEP_DIRS = {"config", "cache", "logs"}


class NapCatError(RuntimeError):
    pass


# ------------------------------------------------------------------ 状态缓存
def _load_state() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(d: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ------------------------------------------------------------------ 查发布
def _shell_asset(data: dict) -> dict | None:
    assets = data.get("assets") or []
    for a in assets:
        if a.get("name") == "NapCat.Shell.zip":
            return a
    for a in assets:                     # 退一步：任何 Shell 但排除 OneKey/Node
        n = a.get("name", "")
        if "Shell" in n and "OneKey" not in n and "Node" not in n and n.endswith(".zip"):
            return a
    return None


def latest(timeout: int = 30) -> dict:
    """最新 release 里的 Windows Shell 包：{tag, id, url, size}。"""
    try:
        req = urllib.request.Request(API, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # 2026-11-12：NapCatQQ 官方仓库把代码和 release 一起撤了，
            # 只剩一行 README「## Ending 到这里告一段落」。
            # 这不是网络问题，是项目终止 —— 反复重试没有意义，说清楚就行。
            raise NapCatError(
                "NapCat 上游已经终止（2026-11-12 仓库只剩一行 README：Ending）。"
                "不会再有新版，等是等不到的。") from e
        raise NapCatError(f"查 NapCat 最新版失败：HTTP {e.code}") from e
    except Exception as e:                                  # noqa: BLE001
        raise NapCatError(f"查 NapCat 最新版失败：{e}") from e

    a = _shell_asset(data)
    if not a:
        raise NapCatError("这个 release 里没有 NapCat.Shell.zip")
    return {"tag": str(data.get("tag_name") or "?"), "id": a.get("id"),
            "url": a.get("browser_download_url") or "", "size": int(a.get("size") or 0)}


# ------------------------------------------------------------------ 下载
def _download(info: dict, dest: Path, say=None, timeout: int = 600) -> Path:
    """优先走 release 的直链；直链被网络中间层挡掉就走 API 资产接口。

    2026-11-11 实测：本机代理对 github.com/releases/download 返回 502，
    但 api.github.com/.../releases/assets/<id> 加 Accept: octet-stream 能下。
    两条都留着，谁通用谁。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tries = []
    if info.get("id"):
        tries.append(("https://api.github.com/repos/NapNeko/NapCatQQ/releases/assets/"
                      f"{info['id']}", {**UA, "Accept": "application/octet-stream"}))
    if info.get("url"):
        tries.append((info["url"], {"User-Agent": "xiaojia-board"}))

    last = None
    for url, hdr in tries:
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as o:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                step = max(1, total // 4)
                nxt = step
                while True:
                    buf = r.read(262144)
                    if not buf:
                        break
                    o.write(buf)
                    got += len(buf)
                    if say and total and got >= nxt:
                        say(f"      已下载 {got // 1048576} / {total // 1048576} MB")
                        nxt += step
            if dest.exists() and dest.stat().st_size > 1024 * 1024:
                return dest
            last = f"{url} 下回来的文件只有 {dest.stat().st_size if dest.exists() else 0} 字节"
        except Exception as e:                              # noqa: BLE001
            last = f"{url} → {e}"
    raise NapCatError(f"下载 NapCat 失败：{last}")


def table_of(zip_path: Path) -> set[str]:
    """zip 里那个 napcat.mjs 的偏移表认哪些版本。读不到就抛。"""
    try:
        with zipfile.ZipFile(zip_path) as z:
            name = next((n for n in z.namelist() if n.endswith("napcat.mjs")), "")
            if not name:
                raise NapCatError("包里没有 napcat.mjs")
            txt = z.read(name).decode("utf-8", errors="ignore")
    except zipfile.BadZipFile as e:
        raise NapCatError(f"下载到的东西不是 zip：{e}") from e
    known = qq_compat.table(txt)
    if not known:
        raise NapCatError("包里的 napcat.mjs 没有偏移表")
    return known


# ------------------------------------------------------------------ 换文件
def _skip(rel: str) -> bool:
    parts = Path(rel).parts
    if not parts:
        return True
    if parts[0] in KEEP_DIRS:
        return True
    low = parts[-1].lower()
    return low.endswith(".db") or ".db-" in low


def swap(zip_path: Path, napcat_dir: Path, backup_dir: Path, say=None) -> int:
    """把包里的程序文件盖到 napcat_dir，返回写了几个文件。

    KEEP_DIRS 与 *.db* 直接跳过：那里面是用户的登录态、OneBot 端口/token、缓存。
    覆盖前把要动的**已存在**文件先拷一份到 backup_dir。
    """
    napcat_dir.mkdir(parents=True, exist_ok=True)
    wrote = 0
    with zipfile.ZipFile(zip_path) as z:
        for item in z.infolist():
            name = item.filename.replace("\\", "/")
            if name.endswith("/") or not item.file_size:
                continue
            if _skip(name):
                continue
            dst = (napcat_dir / name).resolve()
            try:                       # 防 zip slip：解出来的路径必须还在 napcat 下
                dst.relative_to(napcat_dir.resolve())
            except ValueError:
                continue
            if dst.exists():
                keep = backup_dir / name
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, keep)
            dst.parent.mkdir(parents=True, exist_ok=True)
            with z.open(item) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
            wrote += 1
    if say:
        say(f"      覆盖了 {wrote} 个程序文件")
    return wrote


# ------------------------------------------------------------------ 主流程
def heal(qq: str, napcat_dir: Path | None = None, say=None, force: bool = False) -> dict:
    """版本对不上时试着换个能用的 NapCat。

    返回 {ok, tag, qq, wrote, msg}
        ok=True   换好了，且重新读表确认认得这个 QQ
        ok=False  没换（最新版也不认 / 网络不通 / 已经试过），msg 说明原因
    注意：ok=False 是**正常结局**，不是异常 —— 上游还没跟上时本来就该什么都不做。
    """
    def log(m: str) -> None:
        if say:
            say(m)

    nd = Path(napcat_dir) if napcat_dir else Path(settings_napcat())
    out = {"ok": False, "tag": "", "qq": qq, "wrote": 0, "msg": ""}

    st = _load_state()
    # 上游若已宣告终止，就别每次刷新都去敲一扇已经关掉的门 ——
    # 记住结论，一个月内直接用上次的话回答。
    ended = float(st.get("ended_at") or 0)
    if ended and time.time() - ended < 30 * 86400 and not force:
        out["msg"] = ("NapCat 上游已经终止（2026-11-12 仓库只剩一行 README：Ending）。"
                      "不会再有新版，等是等不到的。")
        return out

    try:
        info = latest()
    except NapCatError as e:
        out["msg"] = str(e)
        if "终止" in str(e):
            st["ended_at"] = time.time()
            _save_state(st)
        return out
    out["tag"] = info["tag"]

    cached = (st.get("tables") or {}).get(info["tag"]) or []
    same_tag_fresh = (st.get("tag") == info["tag"]
                      and time.time() - float(st.get("at") or 0) < 24 * 3600)
    tmp = ROOT / "data" / "_napcat_new.zip"
    tab: set[str] = set()

    if cached and same_tag_fresh and not force:
        tab = set(cached)
        log(f"      最新版 {info['tag']} 昨天已经查过，不用再下 34MB")
    else:
        log(f"      最新版是 {info['tag']}，下下来看看它认不认 {qq}…")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            _download(info, tmp, say=say)
            tab = table_of(tmp)
        except NapCatError as e:
            out["msg"] = str(e)
            tmp.unlink(missing_ok=True)
            return out
        st.setdefault("tables", {})[info["tag"]] = sorted(tab)
        st["tag"] = info["tag"]
        st["at"] = time.time()
        _save_state(st)

    if qq not in tab:
        # 上游还没跟上。**不要动现有环境** —— 升上去还是抓不到，白折腾。
        newest = ""
        try:
            newest = max(tab, key=lambda s: (tuple(int(x) for x in s.split("-")[0].split(".")),
                                             int(s.split("-")[1])))
        except Exception:
            pass
        out["msg"] = (f"最新的 NapCat {info['tag']} 也只认到 {newest}，还没有 {qq}。"
                      "上游还没跟上，这次先不动现有环境。")
        tmp.unlink(missing_ok=True)
        return out

    # 表里有，但这次是从缓存读的、zip 没下来（上次换失败了 / 换完被谁改回去了）
    # —— 光有结论没有包，换不了东西。补下。
    if not tmp.exists():
        log(f"      {info['tag']} 认这个 QQ，把包下下来装上…")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            _download(info, tmp, say=say)
        except NapCatError as e:
            out["msg"] = str(e)
            return out

    # 换。备份目录带时间戳，坏了能一步步退回去。
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = ROOT / "data" / "backup" / f"napcat-{info['tag']}-{stamp}"
    log(f"      {info['tag']} 认这个 QQ，换上（原文件备份在 {backup.name}）")
    wrote = swap(tmp, nd, backup, say=say)
    out["wrote"] = wrote

    # 换完重读一遍磁盘上的表。不校验就宣布成功，等于把「我以为」当「事实」。
    known, err = qq_compat.napcat_known(nd)
    if qq in known:
        out["ok"] = True
        out["msg"] = f"已把 NapCat 换到 {info['tag']}，它认 {qq}"
        st = _load_state()
        st["installed_tag"] = info["tag"]
        st["at"] = time.time()
        _save_state(st)
    else:
        out["msg"] = f"换上了 {info['tag']}，但重读偏移表仍不认 {qq}（{err or '表里没有'}）"
    tmp.unlink(missing_ok=True)
    return out


def settings_napcat() -> str:
    import settings
    return settings.napcat_dir()


if __name__ == "__main__":
    import sys
    cp = qq_compat.check()
    print(json.dumps(cp, ensure_ascii=False, indent=2))
    if cp.get("ok") is False:
        r = heal(cp["qq"], say=print)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        sys.exit(0 if r["ok"] else 1)
