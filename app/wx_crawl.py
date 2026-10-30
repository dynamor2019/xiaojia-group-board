# -*- coding: utf-8 -*-
"""
wx_crawl.py — 从已解密的微信库里抽取群 @所有人 喊话

前置：vendor/wcdb-key-tool 已完成 extract + decrypt
     （见 update_wx.py，全自动串起来）

输出与 QQ 侧同构，便于合并进同一张看板：
    {"群名 (群ID)": [{"time": "YYYY-MM-DD HH:MM", "sender": "...", "text": "..."}]}

数据来源说明
------------
* 群消息表：message_N.db 里的 Msg_<md5(群ID)>，local_type=1 为文本
* 群名单：contact.db 的 chat_room（username 形如 1234@chatroom）
* 群名称：contact.db 的 contact（remark 优先，其次 nick_name）
* 群公告：contact.db 的 chat_room_info_detail.announcement_（可选）
* WCDB_CT_message_content=4 表示内容被 zstd 压缩，纯文本消息一般是 0，直接跳过压缩行
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, date
from pathlib import Path

import ctx
import settings

HERE = Path(__file__).resolve().parent
DATA = settings.data_dir()
DECRYPTED = settings.work_dir() / "decrypted"

KEYWORDS = ("@所有人", "@全体成员")

SKIP_TABLES = {
    "TimeStamp", "Name2Id", "DeleteInfo", "DeleteResInfo",
    "sqlite_sequence", "wcdb_builtin_compression_record",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def load_groups() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """返回 (群ID->显示名, md5(群ID)->群ID, 任意用户名->显示名)。"""
    db = DECRYPTED / "contact" / "contact.db"
    if not db.exists():
        raise SystemExit(f"[错误] 找不到 {db}，请先运行 update_wx.py")

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    names: dict[str, str] = {}
    people: dict[str, str] = {}

    try:
        for username, remark, nick in con.execute(
            "SELECT username, remark, nick_name FROM contact"
        ):
            if not username:
                continue
            disp = (remark or nick or "").strip()
            if disp:
                people[username] = disp
    except sqlite3.Error:
        pass

    for u, d in people.items():
        if u.endswith("@chatroom"):
            names[u] = d

    rooms = [r[0] for r in con.execute("SELECT username FROM chat_room") if r and r[0]]
    con.close()

    for r in rooms:
        names.setdefault(r, r)

    hash2gid = {hashlib.md5(r.encode()).hexdigest(): r for r in names}
    return names, hash2gid, people


def parse_group_content(content: str) -> tuple[str, str]:
    """群消息文本形如 'wxid_xxx:\\n正文'，拆出 (发送者, 正文)。"""
    if not isinstance(content, str):
        return "", ""
    if ":\n" in content:
        sender, text = content.split(":\n", 1)
        return sender.strip(), text
    return "", content


def strip_keywords(text: str) -> str:
    out = text
    for k in KEYWORDS:
        out = out.replace(k, " ")
    # 常见的全角/半角分隔符
    return out.strip(" \u2005\u3000\r\n\t，,。:：")


def _look_around(con, tbl: str, sort_seq, ts, sender, people, used=None) -> dict | None:
    """纯喊话时，往前往后各找一遍正文。

    必须按 sort_seq 而不是 local_id：微信回填历史时是"老的先入库、新的后入库"
    的反向插入，local_id 几乎完全是倒序的（实测 23 行里 22 个逆序点），
    照 local_id 取会取到**更晚**的消息。sort_seq 是真正的会话顺序。
    """
    def fetch(where: str, order: str):
        try:
            return con.execute(
                f'SELECT sort_seq, create_time, real_sender_id, message_content FROM "{tbl}" '
                f'WHERE sort_seq {where} ? AND local_type = 1 AND WCDB_CT_message_content = 0 '
                f'ORDER BY sort_seq {order} LIMIT 16', (sort_seq,)).fetchall()
        except sqlite3.Error:
            return []

    def to_cands(rows):
        out = []
        for pseq, pts, _psid, pcontent in rows:
            psender, ptext = parse_group_content(pcontent)
            out.append({"ts": pts, "sender": psender, "key": pseq,
                        "display": people.get(psender, psender), "text": ptext})
        return out

    prev = to_cands(fetch("<", "DESC"))          # 最近的在最前
    nxt = to_cands(fetch(">", "ASC"))
    return ctx.resolve(prev, nxt, sender, ts, used=used)


def scan(days: int = 30) -> tuple[dict, dict]:
    names, hash2gid, people = load_groups()
    cutoff = time.time() - days * 86400
    cutoff_int = int(cutoff)

    # 看板上「取消关注」的群，直接跳过，不扫描
    try:
        import board_state
        muted_gids = {m["gid"] for m in board_state.load()["muted"]
                      if (m.get("src") or "") == "微信"}
    except Exception:
        muted_gids = set()

    result: dict[str, list] = {}
    stats = {"groups_scanned": 0, "tables": 0, "rows_read": 0,
             "hits": 0, "bare": 0, "skipped_muted": 0}
    _skipped_seen: set[str] = set()

    for db_path in sorted(DECRYPTED.glob("message/message_*.db")):
        if "fts" in db_path.name or "resource" in db_path.name:
            continue
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        tables = [
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            )
        ]
        for tbl in tables:
            gid = hash2gid.get(tbl[4:])
            if not gid:
                continue
            if gid in muted_gids:
                _skipped_seen.add(gid)
                continue
            stats["tables"] += 1
            used_seq: set = set()      # 一段正文只能解释一条喊话
            sql = (
                f'SELECT sort_seq, create_time, real_sender_id, message_content '
                f'FROM "{tbl}" '
                f'WHERE create_time >= ? AND local_type = 1 '
                f'AND WCDB_CT_message_content = 0 '
                f'AND (message_content LIKE ? OR message_content LIKE ?) '
                f'ORDER BY create_time'
            )
            try:
                rows = con.execute(
                    sql,
                    (cutoff_int, f"%{KEYWORDS[0]}%", f"%{KEYWORDS[1]}%"),
                ).fetchall()
            except sqlite3.Error as e:
                log(f"  [警告] {db_path.name}/{tbl} 查询失败: {e}")
                continue
            if not rows:
                continue
            stats["groups_scanned"] += 1
            stats["rows_read"] += len(rows)
            label = f"{names.get(gid, gid)} ({gid})"
            bucket = result.setdefault(label, [])
            for sseq, ts, _sid, content in rows:
                sender, text = parse_group_content(content)
                if not any(k in text for k in KEYWORDS):
                    continue
                sender_disp = people.get(sender, sender)
                item = {
                    "time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
                    "sender": sender_disp,
                    "text": text.strip(),
                }
                if ctx.is_bare(item["text"]):
                    # 先发正文、再单独 @所有人 —— 正文在上一条里，捞回来
                    pick = _look_around(con, tbl, sseq, ts, sender, people, used_seq)
                    if pick:
                        item["ctx"] = pick["text"]
                        item["ctx_from"] = ctx.describe(pick)
                        used_seq.update(pick["keys"])
                    else:
                        stats["bare"] += 1        # 找不到正文，交给看板决定怎么处理
                bucket.append(item)
                stats["hits"] += 1
        con.close()

    for v in result.values():
        v.sort(key=lambda r: r["time"], reverse=True)
    stats["skipped_muted"] = len(_skipped_seen)
    return result, stats


def crawl(days: int = 30) -> dict:
    data, stats = scan(days)
    return data


def write_outputs(data: dict, days: int) -> tuple[Path, Path]:
    today = date.today().isoformat()
    DATA.mkdir(parents=True, exist_ok=True)
    out_json = DATA / f"微信群公告_{today}.json"
    out_md = DATA / f"微信群公告_{today}.md"

    out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"# 微信群 @所有人 喊话（近 {days} 天）", ""]
    total = sum(len(v) for v in data.values())
    lines.append(f"共 {len(data)} 个群 / {total} 条")
    lines.append("")
    for label in sorted(data, key=lambda k: max(r["time"] for r in data[k]), reverse=True):
        items = data[label]
        lines.append(f"## {label}（{len(items)} 条）")
        for it in items:
            lines.append(f"- **{it['time']}** {it['sender']}：{it['text']}")
        lines.append("")
    out_md.write_text("\n".join(lines), encoding="utf-8")
    return out_json, out_md


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    days = 30
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])

    log(f"[微信] 扫描近 {days} 天的群消息…")
    data, stats = scan(days)
    out_json, out_md = write_outputs(data, days)

    log(f"[微信] {stats['tables']} 个会话表 / 命中 {stats['groups_scanned']} 个群 / "
        f"{stats['hits']} 条 @所有人"
        f"（其中 {stats['bare']} 条只有 @所有人、且上文里找不到正文；"
        f"跳过 {stats['skipped_muted']} 个已取消关注的群）")
    log(f"[微信] 输出 → {out_json.name} , {out_md.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
