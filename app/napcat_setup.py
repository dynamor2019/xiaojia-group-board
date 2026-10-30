# -*- coding: utf-8 -*-
"""自动下载 NapCat（Windows Shell 版）解到项目目录下。

为什么自己下而不是让用户去网页点
--------------------------------
NapCat 的 release 里有四个包：

    NapCat.Framework.zip              35 MB   给 Linux/macOS 的框架版
    NapCat.Shell.zip                  34 MB   ← Windows 要的就是这个，解压即用
    NapCat.Shell.Windows.Node.zip    118 MB   带 Node 运行时（本机已装 node 就不需要）
    NapCat.Shell.Windows.OneKey.zip    1 MB   里面是 NapCatInstaller.exe，要人点

Shell 包解开后就是完整目录（NapCatWinBootMain.exe / napcat.mjs / qqnt.json …），
不需要那个要交互的安装器，所以可以全自动。

安全边界：只往 <项目根>/napcat 里写文件，不碰 QQ 安装目录、不碰注册表。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

API = "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
UA = {"User-Agent": "xiaojia-board", "Accept": "application/vnd.github+json"}

# 解压后必须存在的文件，缺一个就算装失败
REQUIRED = ("NapCatWinBootMain.exe", "NapCatWinBootHook.dll",
            "napcat.mjs", "qqnt.json", "loadNapCat.js")


class NapCatError(RuntimeError):
    pass


def latest_release(timeout: int = 30) -> dict:
    """返回 {tag, name, url, size}，指向 Windows Shell 包。"""
    try:
        req = urllib.request.Request(API, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise NapCatError(f"查最新版本失败：HTTP {e.code}") from e
    except Exception as e:                              # noqa: BLE001
        raise NapCatError(f"查最新版本失败：{e}") from e

    assets = data.get("assets") or []
    pick = None
    for a in assets:                                   # 精确优先
        if a.get("name") == "NapCat.Shell.zip":
            pick = a
            break
    if pick is None:                                   # 退一步：任何 Shell 但排除 OneKey/Node
        for a in assets:
            n = a.get("name", "")
            if "Shell" in n and "OneKey" not in n and "Node" not in n and n.endswith(".zip"):
                pick = a
                break
    if pick is None:
        raise NapCatError("这个 release 里没找到 NapCat.Shell.zip")

    return {"tag": data.get("tag_name", "?"), "name": pick["name"],
            "url": pick["browser_download_url"], "size": pick.get("size") or 0}


def is_installed(dest: Path) -> bool:
    return all((dest / n).exists() for n in REQUIRED)


def download(dest: Path, on_progress=None, timeout: int = 600) -> dict:
    """下载并解压到 dest。返回 {tag, files, bytes}。

    on_progress(text) 会被反复调用，用来往控制台/网页吐进度。
    """
    def say(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    info = latest_release()
    total = info["size"]
    say(f"版本 {info['tag']}（{total / 1048576:.1f} MB）")

    dest.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="napcat_"))
    zpath = tmpdir / "napcat.zip"
    try:
        say("开始下载…")
        done = 0
        step = 0
        req = urllib.request.Request(info["url"], headers={"User-Agent": "xiaojia-board"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(zpath, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                step += 1
                if total and step % 8 == 0:
                    say(f"下载中 {done * 100 // total}%　"
                        f"{done / 1048576:.1f}/{total / 1048576:.1f} MB")
        say(f"下载完成 {done / 1048576:.1f} MB，正在解压…")

        if not zipfile.is_zipfile(zpath):
            raise NapCatError("下载到的不是 zip，可能被网络劫持或限流")
        n = 0
        with zipfile.ZipFile(zpath) as z:
            for m in z.infolist():
                # config/ 下有你这个号的登录配置，升级时不能覆盖
                if m.filename.startswith("config/") and (dest / m.filename).exists():
                    continue
                z.extract(m, dest)
                n += 1
        say(f"解压完成 {n} 个文件")

        missing = [x for x in REQUIRED if not (dest / x).exists()]
        if missing:
            raise NapCatError("解压后缺少：" + "、".join(missing))
        say(f"就绪：{dest}")
        return {"tag": info["tag"], "files": n, "bytes": done, "dest": str(dest)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    import sys

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd() / "napcat"
    print(json.dumps(download(out, on_progress=print), ensure_ascii=False, indent=2))
