"""通过反向 WebSocket 直接跟 NapCat 对话，抓取各群的 @所有人 消息。

默认增量：只抓上次之后的新消息，抓到旧消息就停；结果合并进累计库 data_store.json。
首次（没有累计库）或 --full 时才回溯 --days 天的全量。

不依赖 HTTP 端口（3000 在本机被系统保留），也不需要重启 QQ。

用法：
    python crawl_ws.py                 # 增量：只抓新的
    python crawl_ws.py --full          # 全量重刷最近 30 天
    python crawl_ws.py --days 30 --pages 15 --groups 123,456
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import re
import sys
from pathlib import Path

try:
    from websockets.asyncio.server import serve
except ImportError:  # 老版本
    from websockets.server import serve

import ctx
import settings

HOST = "127.0.0.1"
PORT = int(settings.get("qq", "ws_port", 8765))
PATH = "/ws/onebot"

AT_ALL = {"all", "everyone", "0", ""}
STORE = Path("data_store.json")

conn = None
pending: dict[str, asyncio.Future] = {}
seq_no = 0


# ----------------------------------------------------------------- 累计库
def load_store(path) -> dict[str, list]:
    path = Path(path)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            print(f"! 累计库损坏，按空库处理: {path}", flush=True)
    return {}


def save_store(path, data: dict[str, list]) -> None:
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def ts_of(item: dict) -> int:
    try:
        return int(dt.datetime.strptime(item["time"], "%Y-%m-%d %H:%M").timestamp())
    except Exception:
        return 0


def merge(old: list, new: list, cutoff_ts: int) -> list:
    """按 (时间, 正文) 去重，保留 cutoff 之后的。"""
    seen, out = set(), []
    for it in list(old) + list(new):
        if not it.get("time") or ts_of(it) < cutoff_ts:
            continue
        k = (it["time"], it.get("sender", ""), (it.get("text") or "")[:80])
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    out.sort(key=lambda x: x["time"])
    return out


# ----------------------------------------------------------------- OneBot
async def handler(ws):
    global conn
    try:
        path = getattr(getattr(ws, "request", None), "path", PATH)
    except Exception:
        path = PATH
    print(f"[ws] NapCat 已连接  path={path}", flush=True)
    conn = ws
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            echo = msg.get("echo")
            if echo is not None and str(echo) in pending:
                fut = pending.pop(str(echo))
                if not fut.done():
                    fut.set_result(msg)
    except Exception as e:
        print(f"[ws] 连接结束: {type(e).__name__} {e}", flush=True)
    finally:
        if conn is ws:
            conn = None
        print("[ws] NapCat 断开了", flush=True)


async def call(action: str, params: dict | None = None, timeout: float = 40.0):
    global seq_no
    if conn is None:
        raise RuntimeError("NapCat 还没连上来")
    seq_no += 1
    echo = str(seq_no)
    fut = asyncio.get_running_loop().create_future()
    pending[echo] = fut
    await conn.send(json.dumps({"action": action, "params": params or {}, "echo": echo},
                               ensure_ascii=False))
    try:
        return await asyncio.wait_for(fut, timeout)
    finally:
        pending.pop(echo, None)


def plain_text(segments) -> str:
    if isinstance(segments, str):
        return re.sub(r"\s+", " ", segments).strip()
    out = []
    for seg in segments or []:
        t, d = seg.get("type"), seg.get("data") or {}
        if t == "text":
            out.append(d.get("text", ""))
        elif t == "at":
            qq = str(d.get("qq", ""))
            out.append("「@所有人」" if qq in AT_ALL else f"@{d.get('name') or qq}")
        elif t == "image":
            out.append("[图片]")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def is_at_all(m: dict) -> bool:
    segs = m.get("message")
    if isinstance(segs, str):
        return "@所有人" in segs
    for seg in segs or []:
        if seg.get("type") == "at" and str((seg.get("data") or {}).get("qq", "")) in AT_ALL:
            return True
    return "@所有人" in plain_text(segs)


def pick_hits(all_msgs: list[dict], floor_ts: int) -> list[dict]:
    """从按时间正序的消息里挑出 @所有人，并给"纯喊话"补上它的正文。

    单独抽出来是为了能脱机测试：不需要 NapCat 在线也能验这段逻辑。
    """
    hits: list[dict] = []
    used: set = set()                       # 已被用来做正文的消息，不许重复挂
    for i, it in enumerate(all_msgs):
        if not it["at"]:
            continue
        if floor_ts and it["ts"] <= floor_ts:
            continue
        rec = {
            "time": dt.datetime.fromtimestamp(it["ts"]).strftime("%Y-%m-%d %H:%M"),
            "sender": it["sender"],
            "text": it["text"],
        }
        if ctx.is_bare(it["text"]):
            lo = max(0, i - 10)
            prev = [{"ts": x["ts"], "sender": x["sender"], "display": x["sender"],
                     "text": x["text"], "key": j} for j, x in enumerate(all_msgs[lo:i], start=lo)]
            prev.reverse()                                    # 最近的在最前
            nxt = [{"ts": x["ts"], "sender": x["sender"], "display": x["sender"],
                    "text": x["text"], "key": j}
                   for j, x in enumerate(all_msgs[i + 1:i + 16], start=i + 1)]
            pick = ctx.resolve(prev, nxt, it["sender"], it["ts"], used=used)
            if pick:
                rec["ctx"] = pick["text"]
                rec["ctx_from"] = ctx.describe(pick)
                used.update(pick["keys"])
        hits.append(rec)
    hits.sort(key=lambda x: x["time"])
    return hits


async def crawl_group(gid, name, pages: int, pace: float, floor_ts: int):
    """只收 time > floor_ts 的；某页整体早于 floor_ts 就停止翻页。

    注意：整页消息都要留着，不能过滤完就扔 —— 有的群习惯"先发正文、再单独
    发一条 @所有人"，正文就在紧挨着的前一条里。以前只留命中行，正文白丢了。
    """
    seen, seq, batches = set(), 0, []
    for page in range(pages):
        try:
            r = await call("get_group_msg_history",
                           {"group_id": int(gid), "message_seq": seq, "count": 100})
        except Exception as e:
            print(f"    ! 第{page+1}页失败: {e}", flush=True)
            break
        msgs = ((r or {}).get("data") or {}).get("messages") or []
        if not msgs:
            break
        new = 0
        oldest = None
        batch = []
        for m in msgs:
            mid = m.get("message_id") or m.get("msgId")
            if mid in seen:
                continue
            seen.add(mid)
            new += 1
            ts = int(m.get("time") or 0)
            if oldest is None or ts < oldest:
                oldest = ts
            batch.append({
                "ts": ts,
                "sender": (m.get("sender") or {}).get("card")
                          or (m.get("sender") or {}).get("nickname")
                          or str(m.get("user_id") or ""),
                "text": plain_text(m.get("message")),
                "at": is_at_all(m),
            })
        if new == 0:
            break
        batches.append(batch)
        if floor_ts and oldest is not None and oldest <= floor_ts:
            break  # 已经翻到上次抓过的位置，后面都是旧的
        seq = msgs[0].get("message_seq") or msgs[0].get("message_id") or 0
        if not seq:
            break
        await asyncio.sleep(pace)

    # 翻页是"从新往旧"，按页倒过来拼才是时间正序
    all_msgs = [x for b in reversed(batches) for x in b]
    return pick_hits(all_msgs, floor_ts)


async def main_async(args):
    server = await serve(handler, HOST, PORT, max_size=None, ping_interval=20)
    print(f"监听 ws://{HOST}:{PORT}{PATH} ，等 NapCat 连上来...", flush=True)
    for _ in range(120):
        if conn is not None:
            break
        await asyncio.sleep(1)
    if conn is None:
        print("等了 120 秒 NapCat 仍未连接，退出")
        return 2

    info = await call("get_login_info")
    me = (info.get("data") or {})
    print(f"已登录: {me.get('nickname')} ({me.get('user_id')})", flush=True)

    groups = (await call("get_group_list")).get("data") or []
    if args.groups:
        want = {g.strip() for g in args.groups.split(",")}
        groups = [g for g in groups if str(g.get("group_id")) in want]

    # 看板上「取消关注」的群直接跳过，省时间也不占日志
    try:
        import board_state
        muted = {m["gid"] for m in board_state.load()["muted"]
                 if (m.get("src") or "QQ") == "QQ"}
    except Exception:
        muted = set()
    if muted:
        before = len(groups)
        groups = [g for g in groups if str(g.get("group_id")) not in muted]
        skipped = before - len(groups)
        if skipped:
            print(f"跳过 {skipped} 个已取消关注的群\n", flush=True)

    now = dt.datetime.now()
    cutoff_ts = int((now - dt.timedelta(days=args.days)).timestamp()) if args.days > 0 else 0

    store = {} if args.full else load_store(args.store)
    mode = "全量" if (args.full or not store) else "增量"
    print(f"模式：{mode} ｜ 窗口：近 {args.days} 天 ｜ 共 {len(groups)} 个群，每群最多 {args.pages} 页\n",
          flush=True)

    added_total = 0
    for i, g in enumerate(groups, 1):
        gid = g.get("group_id")
        name = g.get("group_name") or str(gid)
        key = f"{name} ({gid})"
        old = store.get(key, [])
        last_ts = max((ts_of(x) for x in old), default=0)
        floor = max(last_ts, cutoff_ts)

        print(f"[{i}/{len(groups)}] {name}" + (f"  ← 上次 {dt.datetime.fromtimestamp(last_ts):%m-%d %H:%M}"
                                              if last_ts else ""), flush=True)
        try:
            hits = await crawl_group(gid, name, args.pages, args.pace, floor)
        except Exception as e:
            print(f"    ! 跳过: {e}", flush=True)
            continue

        merged = merge(old, hits, cutoff_ts)
        if merged:
            store[key] = merged
        added_total += max(0, len(merged) - len(old))
        if hits:
            print(f"    -> 新增 {len(hits)} 条，累计 {len(merged)} 条", flush=True)

    # 清掉窗口内的空群，避免库里越积越脏
    store = {k: v for k, v in store.items() if v}

    stamp = now.date().isoformat()
    out = Path(args.out) if args.out else settings.data_dir() / f"群公告_{stamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    path = Path(out)
    total = sum(len(v) for v in store.values())
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 群 @所有人 汇总（{stamp}）\n\n")
        f.write(f"近 {args.days} 天累计 {len(store)} 个群、{total} 条；本次新增 {added_total} 条。\n\n")
        for gname, items in sorted(store.items(), key=lambda kv: -len(kv[1])):
            f.write(f"## {gname} · {len(items)} 条\n\n")
            for it in sorted(items, key=lambda x: x["time"], reverse=True):
                f.write(f"- **{it['time']}** ｜ {it['sender']}\n  {it['text']}\n")
            f.write("\n")
    path.with_suffix(".json").write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    save_store(args.store, store)

    print(f"\n完成：本次新增 {added_total} 条 ｜ 库内 {total} 条 / {len(store)} 群 -> {out}", flush=True)
    server.close()
    await server.wait_closed()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=15, help="每群最多翻几页（每页100条）")
    ap.add_argument("--pace", type=float, default=0.35)
    ap.add_argument("--groups", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--days", type=int, default=30, help="窗口天数；0 = 不限")
    ap.add_argument("--full", action="store_true", help="忽略累计库，全量重刷")
    ap.add_argument("--store", default=str(settings.data_dir() / "data_store.json"),
                    help="累计库路径")

    a = ap.parse_args()
    sys.exit(asyncio.run(main_async(a)))
