# -*- coding: utf-8 -*-
"""给「只喊了 @所有人、没有正文」的消息找回它的上文。

为什么需要
----------
不少群的习惯是：先把通知正文发出来，再**单独**发一条 `@所有人` 触发红点。
于是抓到的命中行只有三个字，正文在紧挨着的前一条里。

原来这套流程在三个地方各丢一次，通知就彻底没了：

    wx_crawl.scan()    纯喊话 → stats["dropped_empty"]，直接 discard
    report.collect()   正文长度 < 4 → continue
    （QQ 侧虽然留下来了，但卡片上就一行「@所有人」，看不出是什么事）

这个模块只做两件事：判断"是不是空喊话"，以及从相邻消息里挑一条最像正文的。

为什么不能无脑取上一条
----------------------
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


def describe(pick: dict) -> str:
    """给看板显示的一行来源说明，比如「同一人 · 2 分钟前」。"""
    who = "同一人" if pick.get("same") else (pick.get("display") or "上一条")
    gap = pick.get("gap_min") or 0
    return f"{who} · {gap} 分钟前" if gap else f"{who} · 刚刚"


def has_at_token(text: str) -> bool:
    t = text or ""
    return any(k in t for k in AT_TOKENS)


def pick_context(cands, hit_sender, hit_ts, used=None,
                 max_gap_min: float = 10.0, same_gap_min: float = 30.0,
                 min_score: int = 3, look: int = 8) -> dict | None:
    """从候选里挑一条当正文。

    cands 必须是「紧邻的前几条」，最近的排在最前（下标 0）。
    每项：{"ts", "sender", "display", "text", "key"(可选，去重用)}
    used 是已被别的喊话用掉的 key 集合 —— **一段正文只能解释一条喊话**，
    否则同一则通知会在看板上被挂到两三条喊话下面，看起来像发了好几遍。
    返回 {"text", "same", "gap_min", "display", "score", "key"} 或 None。

    两条经验规则：
    - 同一个人发的，容忍更久（30 分钟）——"先发正文、忙完再补一条 @所有人"很常见，
      别人发的通常就在眼前，10 分钟以上多半是两件事。
    - 上一条自己就带 @所有人 的，不拿来当正文：那说明通知已经被单独抓到了。
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
        if is_bare(txt) or _noise(txt):        # 另一条空喊话 / 一句"收到"都不算正文
            continue
        if has_at_token(txt):                  # 已经是抓到的通知本体，别再重复挂
            continue
        if not _notice_like(txt):              # 闸门
            continue
        sc = _score(txt, same, gap)
        if sc < min_score:
            continue
        if best is None or sc > best["score"]:      # 分高的优先，同分留更近的
            best = {"text": body_of(txt), "same": same, "gap_min": int(gap),
                    "display": c.get("display") or c.get("sender") or "",
                    "score": sc, "key": key}
    return best
