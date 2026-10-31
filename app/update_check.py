# -*- coding: utf-8 -*-
"""GitHub 心跳：每天问一次官方仓库有没有新提交，有就提示、并且能一键更新。

为什么不比版本号
----------------
解压版用户手里既没有 tag 也没有 .git，`version.py` 又靠人肉维护，很容易忘改。
所以比的是**提交 SHA**：

    本地是 git 克隆 → 读 HEAD
    解压版         → 第一次检查时以远端 HEAD 为基线，之后每次更新成功才推进

这样"有没有新东西"是确定的，不取决于我记不记得改版本号。

节奏
----
状态落在 `data/updates.json`，正常一天只联网一次（`update.hours`，默认 24）。
失败也不会死命重试：记下 error，1 小时后才允许再问，免得把 GitHub 的无认证
速率限制（60 次/小时）耗光。

更新方式
--------
    git 克隆  → `git pull --ff-only`，干净、只传增量
    解压安装  → 下整包覆盖，只覆盖代码/文档，绝不碰 config.json、data/、napcat/、
                app/vendor/decrypted（那里是明文聊天记录）

两种方式都返回 `restart: True`，因为 .py 已经换掉了，跑着的还是老代码。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import settings
from version import BUILD, __version__

ROOT = settings.root()
STATE = settings.data_dir() / "updates.json"

DEFAULT_REPO = "dynamor2019/xiaojia-group-board"
BRANCH = "main"

# 覆盖更新时这些整目录跳过（用户数据 / 第三方二进制 / 明文库）
SKIP_DIRS = {"data", "napcat", ".git", "__pycache__", "decrypted", "logs"}
# 这些文件属于用户，永远不覆盖
KEEP_FILES = {"config.json", "小贾AI群看板.html", "all_keys.json"}

_EMPTY = {
    "checked_at": 0.0,
    "installed_sha": "",
    "remote_sha": "",
    "remote_date": "",
    "notes": [],
    # 最近几条提交（不管本地落后不落后）。「更新内容」弹层不能只在有新版本时才给东西看 ——
    # 用户点完更新，提示条一消失，就再也看不到刚才改了什么。见 _summary()。
    "recent": [],
    "error": "",
    "ignored": "",
}


# ------------------------------------------------------------------ 小工具
def _repo() -> str:
    return (settings.get("update", "repo", DEFAULT_REPO) or DEFAULT_REPO).strip().strip("/")


def repo_url() -> str:
    return f"https://github.com/{_repo()}"


def _hours() -> float:
    try:
        return float(settings.get("update", "hours", 24) or 24)
    except (TypeError, ValueError):
        return 24.0


def enabled() -> bool:
    return bool(settings.get("update", "enabled", True))


def _open(url: str, timeout: float = 12):
    req = urllib.request.Request(url, headers={
        "User-Agent": f"xiaojia-board/{__version__}",
        "Accept": "application/vnd.github+json",
    })
    return urllib.request.urlopen(req, timeout=timeout)


def _get_json(url: str):
    with _open(url) as r:
        return json.loads(r.read().decode("utf-8"))


def _err(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 403:
            return "GitHub 限流了（403），过一会儿再试"
        if e.code == 404:
            return f"仓库地址不对或已改名（404）：{_repo()}"
        return f"GitHub 返回 {e.code}"
    if isinstance(e, urllib.error.URLError):
        reason = getattr(e, "reason", e)
        return f"连不上 GitHub（{reason}）—— 可能需要代理"
    return f"{type(e).__name__}: {e}"[:160]


def _fmt(iso: str) -> str:
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return (iso or "")[:16].replace("T", " ")


# ------------------------------------------------------------------ 状态
def load() -> dict:
    d = dict(_EMPTY)
    try:
        d.update(json.loads(STATE.read_text(encoding="utf-8")))
    except Exception:                                   # noqa: BLE001
        pass
    if not isinstance(d.get("notes"), list):
        d["notes"] = []
    return d


def save(d: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE)
    except OSError:
        pass


# ------------------------------------------------------------------ git
def _git_exe() -> str | None:
    g = shutil.which("git")
    if g:
        return g
    for c in (r"C:\Program Files\Git\cmd\git.exe",
              r"C:\Program Files (x86)\Git\cmd\git.exe",
              str(Path.home() / "AppData/Local/Programs/Git/cmd/git.exe")):
        if Path(c).exists():
            return c
    return None


def is_git() -> bool:
    return (ROOT / ".git").exists() and _git_exe() is not None


def git_head() -> str:
    if not is_git():
        return ""
    try:
        p = subprocess.run([_git_exe(), "-C", str(ROOT), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=20)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def local_rev() -> str:
    """当前装的是哪个提交。

    git 工作区里**一律以 HEAD 为准**，不认 updates.json 里记的那个 —— 用户（或者
    我们）完全可能自己 `git pull`、`git commit` 过，那些都不会经过 apply()。
    只信记录的话，本地明明是最新的，看板还会天天喊"有新版本"。
    解压版没有 .git，就只能退回记录值。
    """
    return git_head() or str(load().get("installed_sha") or "")


# ------------------------------------------------------------------ 检查
def due() -> bool:
    if not enabled():
        return False
    return (time.time() - float(load().get("checked_at") or 0)) >= _hours() * 3600


_PT = re.compile(r"^(?:[-*·]|\d+[.)、]|[①②③④⑤⑥])\s*")


def commit_points(lines: list, limit: int = 6) -> list[str]:
    """把提交正文压成几条要点。

    git 的正文是硬换行的（一行七八十字就断），直接按行拆会把一句话切成两截，
    读起来像电报。所以先合并续行（中文之间不加空格），再只挑列表项 ——
    「1.」「- 」开头的那些就是改动清单，前面那句引言行不必占位。
    """
    merged: list[str] = []
    for raw in lines:
        t = str(raw).strip()
        if not t:
            continue
        if _PT.match(t) or not merged:
            merged.append(t)
        else:
            merged[-1] += t
    pts = [m for m in merged if _PT.match(m)]
    if pts:
        out = [_PT.sub("", m).strip() for m in pts]
        return [x for x in out if x][:limit]
    return merged[:2]


def fetch_commits(n: int = 30) -> list[dict]:
    data = _get_json(f"https://api.github.com/repos/{_repo()}/commits"
                     f"?per_page={n}&sha={BRANCH}")
    out = []
    for c in data or []:
        cm = c.get("commit") or {}
        msg = (cm.get("message") or "").strip().splitlines()
        # 正文也带上：只看首行等于只说了一半，用户想知道"到底改了什么"
        body = commit_points([ln for ln in msg[1:]
                              if not ln.strip().lower().startswith(("co-authored-by", "signed-off-by"))])
        out.append({
            "sha": c.get("sha", ""),
            "short": (c.get("sha") or "")[:8],
            "date": _fmt((cm.get("author") or {}).get("date", "")),
            "msg": (msg[0][:140] if msg else "(无提交说明)"),
            "body": body[:12],
        })
    return out


def _summary(st: dict, cached: bool = False) -> dict:
    cur = local_rev()
    head = st.get("remote_sha") or ""
    notes = [n for n in (st.get("notes") or []) if isinstance(n, dict)]
    recent = [n for n in (st.get("recent") or []) if isinstance(n, dict)]
    has = bool(enabled() and cur and head and head != cur and head != st.get("ignored"))
    return {
        "enabled": enabled(),
        "ok": not st.get("error"),
        "error": st.get("error") or "",
        "version": __version__,
        "build": BUILD,
        "current": cur[:8],
        "latest": head[:8],
        "latest_date": st.get("remote_date") or "",
        "has_update": has,
        "notes": notes if has else [],
        # 没有新版本时也给最近几次改动：弹层永远有东西看，而不是弹出来一句"没有内容"
        "recent": recent,
        "checked_at": st.get("checked_at") or 0,
        "checked_txt": _fmt(datetime.fromtimestamp(
            st.get("checked_at") or 0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        if st.get("checked_at") else "",
        "cached": cached,
        "method": "git" if is_git() else "zip",
        "url": repo_url(),
    }


def check(force: bool = False) -> dict:
    """问一次 GitHub。force=False 时 24 小时内直接用上次结果，不联网。"""
    st = load()
    if not enabled():
        return _summary(st, cached=True)
    if not force and not due():
        return _summary(st, cached=True)

    try:
        commits = fetch_commits()
    except Exception as e:                              # noqa: BLE001
        st["error"] = _err(e)
        # 失败不在同一个 24 小时里反复撞：1 小时后才允许重试
        st["checked_at"] = time.time() - _hours() * 3600 + 3600
        save(st)
        return _summary(st, cached=False)

    if not commits:
        st["error"] = "仓库里没读到提交"
        st["checked_at"] = time.time()
        save(st)
        return _summary(st, cached=False)

    cur = local_rev()
    baseline = False
    if not cur:
        # 解压版第一次跑：以远端为基线，别一上来就喊"有更新"
        cur = commits[0]["sha"]
        baseline = True

    idx = next((i for i, c in enumerate(commits) if c["sha"] == cur), -1)
    if idx == 0:
        notes = []
    elif idx > 0:
        notes = commits[:idx]
    else:
        notes = list(commits)        # 本地 sha 不在最近 30 条里：整批都是新的

    st.update({
        "installed_sha": cur,
        "remote_sha": commits[0]["sha"],
        "remote_date": commits[0]["date"],
        "notes": notes,
        # 无论落后不落后都留一份最近的，供「更新内容」弹层随时查看
        "recent": list(commits[:6]),
        "error": "",
        "checked_at": time.time(),
    })
    save(st)
    return _summary(st, cached=False)


def ignore() -> dict:
    """这次不提醒：记住远端 SHA，它再变才重新提示。"""
    st = load()
    st["ignored"] = st.get("remote_sha") or ""
    save(st)
    return _summary(st, cached=True)


# ------------------------------------------------------------------ 更新
def _say_default(msg: str) -> None:
    try:
        print(msg, flush=True)
    except Exception:                                   # noqa: BLE001
        pass


def apply(progress=None) -> dict:
    say = progress or _say_default
    st = load()
    if not st.get("remote_sha"):
        r = check(force=True)
        st = load()
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "还没查到远端版本"}
    target = st.get("remote_sha") or ""
    if not target:
        return {"ok": False, "error": "还没查到远端版本，先点一次检查"}
    if is_git():
        r = _apply_git(target, say)
        # git 拉不动很常见（SSH 没配 key、公司网络挡 22 端口、代理拦截证书）。
        # 公共仓库整包下载只要读权限，所以自动退一步 —— 但本地改过代码时不退，
        # 那种情况覆盖会把用户自己的修改抹掉。
        if r.get("ok") or r.get("local_changes"):
            return r
        say("git 走不通，改用整包覆盖（只覆盖程序文件，不动 config/data/napcat）…")
        z = _apply_zip(target, say)
        z["fallback"] = "git"
        return z
    return _apply_zip(target, say)


def _apply_git(target: str, say) -> dict:
    g = _git_exe()
    say(f"git 安装，执行 git pull --ff-only origin {BRANCH} …")
    try:
        p = subprocess.run([g, "-C", str(ROOT), "pull", "--ff-only", "origin", BRANCH],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=240)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "method": "git", "error": f"git 调用失败：{e}"}
    for ln in (p.stdout or "").splitlines():
        if ln.strip():
            say("  " + ln.rstrip())
    if p.returncode != 0:
        err_lines = [ln.rstrip() for ln in (p.stderr or "").splitlines() if ln.strip()]
        for ln in err_lines:
            say("  " + ln)
        text = "\n".join(err_lines).lower()
        dirty = any(k in text for k in ("would be overwritten", "local changes",
                                        "not possible to fast-forward",
                                        "uncommitted changes"))
        if dirty:
            return {"ok": False, "method": "git", "local_changes": True,
                    "error": "本地改过程序文件，没法自动合并。想保留就手动 git pull；"
                             "想直接用最新版，先把改动提交或撤销再点一次。"}
        return {"ok": False, "method": "git",
                "error": "git 拉取失败：" + (err_lines[-1] if err_lines else "未知原因")}

    new = git_head() or target
    st = load()
    st.update({"installed_sha": new, "remote_sha": new, "notes": [],
               "error": "", "checked_at": time.time(), "ignored": ""})
    save(st)
    say(f"已更新到 {new[:8]}")
    return {"ok": True, "method": "git", "to": new[:8], "restart": True,
            "files": _changed_files()}


def _changed_files() -> list[str]:
    """更新完告诉用户到底动了什么（拿不到就算了）。"""
    g = _git_exe()
    try:
        p = subprocess.run([g, "-C", str(ROOT), "show", "--stat", "--oneline",
                            "-1", "HEAD"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20)
        return [ln.rstrip() for ln in (p.stdout or "").splitlines()[1:] if ln.strip()][:12]
    except (OSError, subprocess.SubprocessError):
        return []


def _same_bytes(a: bytes, b: bytes) -> bool:
    """内容一样就当一样 —— 忽略 CRLF/LF。

    Windows 上仓库 checkout 出来是 CRLF，zip 包里是 LF，逐字节比会认为"全变了"。
    真去覆盖的话 git 工作区会莫名其妙变脏，下一次 git pull 反而因为
    "local changes would be overwritten" 彻底走不通。所以这里必须宽容。
    """
    if a == b:
        return True
    return a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n")


def _apply_zip(target: str, say) -> dict:
    url = f"https://codeload.github.com/{_repo()}/zip/refs/heads/{BRANCH}"
    say("解压安装（没有 .git），改用整包覆盖 …")
    tmp = Path(tempfile.mkdtemp(prefix="xjboard_"))
    zp = tmp / "src.zip"
    try:
        with _open(url, timeout=90) as r, open(zp, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:                              # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        say("  下载失败：" + _err(e))
        return {"ok": False, "method": "zip",
                "error": f"下载更新包失败：{_err(e)}。可以手动去 {repo_url()} 下 zip 覆盖。"}
    say(f"  下载完成 {zp.stat().st_size / 1024 / 1024:.1f} MB")

    try:
        with zipfile.ZipFile(zp) as z:
            z.extractall(tmp / "x")
        roots = [p for p in (tmp / "x").iterdir() if p.is_dir()]
        if not roots:
            raise ValueError("压缩包里没有目录")
        src = roots[0]
    except Exception as e:                              # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "method": "zip", "error": f"解压失败：{e}"}

    n = 0
    skipped = 0
    changed: list[str] = []
    try:
        for p in src.rglob("*"):
            rel = p.relative_to(src)
            if p.is_dir() or rel.name in KEEP_FILES:
                continue
            if set(rel.parts) & SKIP_DIRS:
                continue
            if p.suffix in (".pyc", ".log"):
                continue
            dst = ROOT / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                try:
                    if _same_bytes(p.read_bytes(), dst.read_bytes()):
                        skipped += 1
                        continue
                except OSError:
                    pass
            shutil.copy2(p, dst)
            n += 1
            if len(changed) < 12:
                changed.append(str(rel))
    except OSError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "method": "zip", "error": f"写入失败：{e}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    st = load()
    st.update({"installed_sha": target, "remote_sha": target, "notes": [],
               "error": "", "checked_at": time.time(), "ignored": ""})
    save(st)
    if n:
        say(f"覆盖了 {n} 个文件（{skipped} 个内容一样，跳过）")
    else:
        say(f"文件已经是最新的（比对 {skipped} 个，没有需要覆盖的）")
    return {"ok": True, "method": "zip", "to": target[:8], "restart": True,
            "files": changed, "count": n}


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    arg = (sys.argv[1] if len(sys.argv) > 1 else "check")
    if arg == "apply":
        print(json.dumps(apply(print), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(check(force=arg == "force"), ensure_ascii=False, indent=2))
