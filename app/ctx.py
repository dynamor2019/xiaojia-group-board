# -*- coding: utf-8 -*-
"""给「只喊了 @所有人、没有正文」的消息找回它的正文。

为什么需要
----------
不少群的习惯是：正文和 `@所有人` 分两条发。抓到的命中行只有三个字，正文在别处。
两种方向都常见：

    正文 → @所有人     先写通知，再补一条把人喊出来（最常见的尾巴式）
    @所有人 → 正文     先喊人，再把正文贴出来（同一人紧接着发）

所以两侧都要看，两侧都有就合并 —— 但**方向不同，判据也不同**：

    往前：谁发的不一定，只能靠"像不像通知"来筛
    往后：只认同一个人紧接着发的那条，别人插话不算

原来这套流程在三个地方各丢一次，通知就彻底没了：

    wx_crawl.scan()    纯喊话 → 直接 discard（现在记 stats["bare"]，先留着）
    report.collect()   正文长度 < 4 → continue
    （QQ 侧虽然留下来了，但卡片上就一行「@所有人」，看不出是什么事）

现在改法：两侧都**不丢**，只记数（stats["bare"] / collect.dropped[2]），
由这里尽可能补正文；补不上的才在看板页头合并成一句「N 条只喊了 @所有人…已隐藏」。

为什么往前看不能无脑取上一条
----------------------------
真实数据里三种形态混在一起（统计自本机 457 条命中，其中 70 条是纯喊话）：

    1. 同一人 2 分钟前发通知，再 @所有人         → 这才是我们要的
    2. 别人发的通知，另一个人 @所有人 催一下      → 也要（"上面几条是……"）
    3. 上一条是闲聊 / 隔了 141 分钟 / 压根没有    → 挂了就是脏数据

所以顺序是「先过闸门，再打分」：
    - 闸门（_notice_like）：不像通知的直接出局，不看分数
    - 打分：同一人权重最高，其次时间近、含日期、够长

宁可漏，不可错 —— 因为错误的上文会让人以为通知就是这个内容。
"""
from __future__ import annotations

import re

# 各通道把 @ 拆成了不同的写法：QQ 的 plain_text 给出「@所有人」占位
AT_TOKENS = ("「@所有人」", "「@全体成员」", "@全体成员", "@所有人")

PUNCT = " \t\r\n\u2005\u3000，,。:：;；、/|~～!！?？-—_…·"

# 通知里几乎一定出现的时间锚点
DATE_RE = re.compile(
    r"(今天|明天|后天|大后天|本周|这周|下周|下下周|"
    r"周[一二三四五六日天]|星期[一二三四五六日天]|礼拜[一二三四五六日天]|"
    r"\d{1,2}\s*[月日号]|\d{1,2}\s*[:：]\s*\d{2}|\d{1,2}\s*点)")

NOTICE_WORDS = (
    "请", "务必", "截止", "之前", "前完成", "报名", "提交", "上交", "填写", "填报",
    "会议", "开会", "报到", "签到", "参加", "参会", "地点", "时间", "安排",
    "通知", "注意", "要求", "统一", "尽快", "领取", "负责", "确认", "备注",
    "各位", "老师", "同学", "家长", "名单", "表格", "文件", "链接",
)

# 疑问句几乎都是聊天。"周三谁有监考""家长们要不要买文具"这类会被日期骗过闸门，
# 但正文里带「请/务必/截止/通知」的正式通知经常也含疑问词（"请确认有没有问题"），
# 所以只在没有任何强标记时才据此否决。
QUESTION_RE = re.compile(r"(谁|哪[个里些儿]|吗|呢|多少|几点|是不是|要不要|可以吗|行不行|怎么|啥|咋)")
STRONG_MARKS = ("请", "务必", "截止", "通知", "要求", "【")

NOISE_SET = {
    "收到", "好的", "好滴", "好", "行", "可以", "嗯", "嗯嗯", "哦", "噢", "谢谢",
    "辛苦了", "哈", "哈哈", "哈哈哈", "在吗", "1", "+1", "ok", "OK", "是的", "对",
    "同上", "赞", "玫瑰", "抱拳", "呲牙",
}


def body_of(text: str) -> str:
    """去掉 @所有人 与首尾标点，剩下的就是"正文"；空字符串代表纯喊话。"""
    s = text or ""
    for t in AT_TOKENS:
        s = s.replace(t, "")
    return s.strip(PUNCT)


def is_bare(text: str) -> bool:
    """只有 @所有人（可能带个空格/标点），没有实际内容。"""
    return not body_of(text)


def _noise(text: str) -> bool:
    b = body_of(text)
    return len(b) <= 6 and b in NOISE_SET


def _notice_like(text: str) -> bool:
    """闸门：这条像不像一则通知。不像就一分不给。"""
    t = body_of(text)
    if len(t) < 6:
        return False
    if QUESTION_RE.search(t) and not any(m in t for m in STRONG_MARKS):
        return False                       # 没有强标记的疑问句，当聊天处理
    if DATE_RE.search(t):
        return True
    if sum(1 for w in NOTICE_WORDS if w in t) >= 2:
        return True
    return t.lstrip().startswith(("【", "〖", "［"))


def _score(text: str, same_sender: bool, gap_min: float) -> int:
    t = body_of(text)
    s = 0
    if same_sender:
        s += 3                      # 同一人先发正文再补一条 @所有人，是最典型的形态
    if gap_min <= 5:
        s += 2
    elif gap_min <= 10:
        s += 1
    if DATE_RE.search(t):
        s += 2
    if len(t) >= 40:
        s += 1
    return s


def has_at_token(text: str) -> bool:
    t = text or ""
    return any(k in t for k in AT_TOKENS)


# --------------------------------------------------------------------- 往两边看
def _cand_before(cands, hit_sender, hit_ts, used=None,
                 max_gap_min: float = 10.0, same_gap_min: float = 30.0,
                 min_score: int = 3, look: int = 8) -> dict | None:
    """往回找：「正文 → @所有人」这种发法。

    别人发的只给 10 分钟，同一人给 30 分钟——"先写正文、忙完再补喊一声"经常拖一会儿。
    """
    best = None
    for c in cands[:look]:
        try:
            gap = (float(hit_ts) - float(c["ts"])) / 60.0
        except (KeyError, TypeError, ValueError):
            continue
        if gap < 0:
            continue
        key = c.get("key")
        if used and key is not None and key in used:
            continue
        same = str(c.get("sender") or "") == str(hit_sender or "")
        if gap > (same_gap_min if same else max_gap_min):
            continue
        txt = c.get("text") or ""
        if is_bare(txt) or _noise(txt) or has_at_token(txt):
            continue
        if not _notice_like(txt):              # 闸门
            continue
        sc = _score(txt, same, gap)
        if sc < min_score:
            continue
        if best is None or sc > best["score"]:
            best = {"text": body_of(txt), "same": same, "gap_min": int(gap),
                    "display": c.get("display") or c.get("sender") or "",
                    "score": sc, "key": key, "side": "before"}
    return best


def _cand_after(cands, hit_sender, hit_ts, used=None,
                max_gap_min: float = 10.0, look: int = 15) -> dict | None:
    """往后找：「@所有人 → 正文」这种发法。

    只认**同一个人**随后发的消息——别人紧接着插的话不算。所以这侧的置信度
    来自"同一个人 + 就在后面"，闸门可以放宽，但不能完全没有：
    实测不加约束会把「下午不在这」「@胡微 好的，我修改一下」这种聊天当成正文。

    采用条件（满足其一）：
      * 看着像通知（含日期/≥2 个通知词/【开头）
      * 是一段成句的长消息（≥12 字）且不是在对别人说话（不以 @ 开头）
    """
    for c in cands[:look]:
        try:
            gap = (float(c["ts"]) - float(hit_ts)) / 60.0
        except (KeyError, TypeError, ValueError):
            continue
        if gap < 0 or gap > max_gap_min:
            continue
        key = c.get("key")
        if used and key is not None and key in used:
            continue
        if str(c.get("sender") or "") != str(hit_sender or ""):
            continue                            # 下一条必须是同一个人发的
        txt = c.get("text") or ""
        if is_bare(txt) or has_at_token(txt):   # 又是纯喊话 / 另一条已经抓到的通知
            continue
        body = body_of(txt)
        if len(body) < 4 or _noise(body):
            continue
        if not (_notice_like(txt) or (len(body) >= 12 and not body.startswith("@"))):
            continue
        return {"text": body, "same": True, "gap_min": int(gap),
                "display": c.get("display") or c.get("sender") or "",
                "score": _score(txt, True, gap), "key": key, "side": "after"}
    return None


def _merge(before: dict | None, after: dict | None, limit: int = 480) -> dict | None:
    """两边都有就合起来，先上后下（顺序念起来才是通顺的）。"""
    parts = [p for p in (before, after) if p]
    if not parts:
        return None
    texts: list[str] = []
    for p in parts:
        t = (p.get("text") or "").strip()
        if not t:
            continue
        merged = False
        for i, o in enumerate(texts):
            if t in o:                       # 新的是旧的子串，旧的留着
                merged = True
                break
            if o in t:                       # 旧的是新的子串，换成更全的
                texts[i] = t
                merged = True
                break
        if not merged:
            texts.append(t)
    if not texts:
        return None
    text = "\n".join(texts)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return {
        "text": text,
        "parts": parts,
        "same": all(p["same"] for p in parts),
        "gap_min": min((p["gap_min"] for p in parts), default=0),
        "score": max(p["score"] for p in parts),
        "keys": [p["key"] for p in parts if p.get("key") is not None],
    }


def describe(pick: dict) -> str:
    """给看板显示的一行来源说明，比如「同一人 · 2 分钟前」。"""
    parts = pick.get("parts") or []
    if not parts:
        return ""
    same = all(p["same"] for p in parts)
    who = "同一人" if same else (parts[0].get("display") or "他人")
    if len(parts) == 1:
        p = parts[0]
        if p["side"] == "after":
            return f"{who} · 后一条"
        gap = p.get("gap_min") or 0
        return f"{who} · {gap} 分钟前" if gap else f"{who} · 刚刚"
    return f"{who} · 前后各一条" if same else "前一条 + 后一条"


def resolve(prev, nxt, hit_sender, hit_ts, used=None) -> dict | None:
    """一条纯喊话的正文：往前看一条，也往后看同一个人补的那条，都有就合并。

    prev：紧邻的前几条，最近的在最前
    nxt ：紧随其后的几条，最近的排在最前
    两者都必须是同一群的连续消息，key 用于去重（一段正文只能解释一条喊话）。
    """
    before = _cand_before(prev, hit_sender, hit_ts, used)
    after = _cand_after(nxt, hit_sender, hit_ts, used)
    return _merge(before, after)


# 兼容旧调用：只看上文
def pick_context(cands, hit_sender, hit_ts, used=None, **kw):
    return _cand_before(cands, hit_sender, hit_ts, used, **kw)


