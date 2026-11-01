# -*- coding: utf-8 -*-
"""从 git 历史生成 docs/CHANGELOG.md。

为什么不手写：手写的一定会忘。这份文件的唯一真相是提交记录，
所以用脚本生成，发版时跑一次就行：

    python tools/changelog.py

规则很简单，照着 commit 走：
  - 首行是标题（一句话说清"用户能看到什么变了"）
  - 正文里 `- ` 开头的行是要点，原样搬过来
  - 版本号取那次提交里的 app/version.py（没有就归到 v1.0）
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "CHANGELOG.md"


def git(*args: str, quiet: bool = False) -> str:
    p = subprocess.run(["git", "-C", str(ROOT), *args],
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        if not quiet:
            print(p.stderr.strip(), file=sys.stderr)
        return ""
    return p.stdout


def version_of(sha: str) -> str:
    """那次提交里 version.py 写的版本号；早期还没有这个文件。"""
    # 早期提交没有 app/version.py，git show 会返回非零 —— 这是正常情况，别刷到终端上
    txt = git("show", f"{sha}:app/version.py", quiet=True)
    m = re.search(r'__version__\s*=\s*"([\d.]+)"', txt)
    return m.group(1) if m else ""


def main() -> int:
    log = git("log", "--reverse", "--format=%H%x1f%ad%x1f%s%x1f%b%x1e",
              "--date=format:%Y-%m-%d")
    if not log.strip():
        print("读不到 git 历史")
        return 2

    commits = []
    for rec in log.split("\x1e"):
        rec = rec.strip()
        if not rec:
            continue
        sha, date, subject, body = (rec.split("\x1f") + ["", "", "", ""])[:4]
        pts = [ln.strip()[2:].strip() for ln in body.splitlines()
               if ln.strip().startswith("- ")]
        commits.append({"sha": sha[:8], "date": date, "subject": subject.strip(),
                        "ver": version_of(sha), "pts": pts})

    # 按版本分组，保持提交顺序（旧 → 新），最后倒过来写
    groups: list[tuple[str, str, list[dict]]] = []      # (版本, 首日, 提交)
    for c in commits:
        v = c["ver"] or "1.0"
        if groups and groups[-1][0] == v:
            groups[-1][2].append(c)
        else:
            groups.append((v, c["date"], [c]))
    groups.reverse()

    lines = [
        "# 更新日志",
        "",
        "每个版本改了什么，按时间倒序。这份文件由 `python tools/changelog.py` ",
        "从 git 提交生成 —— 看板「更新内容」里显示的也是同一批说明，",
        "只是那里只列你落后的那几条，这里留全量。",
        "",
    ]
    for ver, date, cs in groups:
        lines.append(f"## v{ver} · {date}")
        lines.append("")
        for c in cs:
            lines.append(f"### {c['subject']}")
            lines.append("")
            if c["pts"]:
                lines += [f"- {p}" for p in c["pts"]]
                lines.append("")
            lines.append(f"<sub>`{c['sha']}` · {c['date']}</sub>")
            lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    nver = len(groups)
    print(f"OK -> {OUT.relative_to(ROOT)}  {nver} 个版本 / {len(commits)} 次提交")
    return 0


if __name__ == "__main__":
    sys.exit(main())
