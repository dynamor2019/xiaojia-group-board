# -*- coding: utf-8 -*-
"""把 crawl_ws.py 抓到的 @所有人 原始记录，提炼成一份"要我干什么"的提醒清单。

规则式，不依赖任何外部 API：
  1. 从正文里抠截止时间（今天/明天/本周三/11月4日前/10.22 16:00 ...）
  2. 用动词判断这条是不是"要我动手"（提交/填报/核对/参会/领取/报名/回复...）
  3. 按 【过期 / 7 天内 / 之后 / 无明确期限】 分桶排序
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import ctx
import settings

BASE = Path(__file__).resolve().parent
SRC = settings.data_dir() / "群公告_最新.json"      # 兜底名，正常会由 pick_src() 找到最新那份
OUT = settings.data_dir() / "待办提醒_最新.md"

TODAY = date(2027, 1, 1)  # 由 main 覆写


def pick_src(directory: Path | None = None) -> Path:
    """取目录下最新的 群公告_*.json（每天跑文件名会变）。"""
    d = directory or settings.data_dir()
    cands = sorted(d.glob("群公告_*.json"), key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else SRC

# ---------- 动作词 ----------
ACTION_VERBS = [
    "提交", "上交", "报送", "报备", "填写", "填报", "填表", "填", "录入", "上传",
    "核对", "核实", "确认", "审核", "签字", "盖章", "领取", "领取", "交至", "交给",
    "报名", "申报", "申请", "参加", "参会", "出席", "准时", "到场", "签到",
    "回复", "反馈", "报给我", "发我", "私信", "联系我", "统计", "汇总",
    "完成", "截止", "之前", "前完成", "打卡", "投票", "下载", "查看", "维护",
]
# 纯通知/寒暄，降权
NOISE_HINTS = ["谢谢", "辛苦了", "祝", "节日快乐", "恭喜"]

# 主题标签（先匹配先得）
TOPICS = [
    ("课题申报", ["申报", "招标", "课题", "立项", "科研", "揭榜", "创新团队", "项目申报"]),
    ("教学教务", ["课表", "调课", "成绩", "考试", "毕业设计", "答辩", "授课", "课程", "教学", "查重", "毕设", "选课"]),
    ("人事行政", ["人事", "考勤", "请假", "工资", "绩效", "社保", "培训", "证书", "师德", "问卷", "调研", "一师一档", "职称"]),
    ("会议活动", ["例会", "参会", "会议", "讲座", "宣讲", "典礼", "报告会", "大讲堂", "签到"]),
    ("学生管理", ["学生", "辅导员", "班主任", "新生", "研究生", "班级", "安全"]),
    ("后勤通知", ["停电", "停水", "供暖", "班车", "餐厅", "维修", "施工"]),
]


def topic_of(text: str) -> str:
    for name, kws in TOPICS:
        if any(k in text for k in kws):
            return name
    return "综合"

WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

RE_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
RE_THIS_MONTH = re.compile(r"本月\s*(\d{1,2})\s*[日号]")
RE_NEXT_MONTH = re.compile(r"下(?:个)?月\s*(\d{1,2})\s*[日号]")
RE_MONTH_END = re.compile(r"(本|下|这个)?月底|月(?:底|末)")
RE_DOT = re.compile(r"(?<!\d)(\d{1,2})\s*[.．]\s*(\d{1,2})(?!\d)\s*[日号]?")
RE_MD_SLASH = re.compile(r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\d)")
RE_WEEK = re.compile(r"(本|下|这)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])")
RE_REL = re.compile(r"(今天|今日|明天|明日|后天|大后天)")
RE_TIME = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})|(\d{1,2})\s*点")
RE_DEADLINE_WORD = re.compile(r"(截止|截至|之前|以前|前完成|前提交|前报送|前交|deadline)", re.I)


def weekday_of(d: date) -> str:
    return "一二三四五六日"[d.weekday()]


def extract_deadlines(text: str, msg_date: date) -> list[date]:
    """从一条消息里抠出所有可能的截止日期。宁可多抠，后面靠 dedupe 收敛。"""
    out: list[date] = []
    has_deadline_word = bool(RE_DEADLINE_WORD.search(text))

    # 相对日
    for m in RE_REL.finditer(text):
        w = m.group(1)
        delta = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}[w]
        out.append(msg_date + timedelta(days=delta))

    # 周几
    for m in RE_WEEK.finditer(text):
        prefix, ch = m.group(1), m.group(2)
        target = WEEKDAY[ch]
        cur = msg_date.weekday()
        if prefix == "下":
            delta = (7 - cur) + target
        elif prefix == "本" or prefix == "这":
            delta = target - cur
            if delta < 0:
                continue  # 本周已过，不算
        else:  # 裸"周三"，按最近的未来那天算
            delta = (target - cur) % 7
        out.append(msg_date + timedelta(days=delta))

    # X月X日
    for m in RE_MD.finditer(text):
        mo, dy = int(m.group(1)), int(m.group(2))
        if not (1 <= mo <= 12 and 1 <= dy <= 31):
            continue
        y = msg_date.year
        if mo < msg_date.month - 6:  # 明显是明年
            y += 1
        try:
            out.append(date(y, mo, dy))
        except ValueError:
            pass

    # 本月X日 / 下月X日 / 月底
    for m in RE_THIS_MONTH.finditer(text):
        dy = int(m.group(1))
        try:
            out.append(date(msg_date.year, msg_date.month, dy))
        except ValueError:
            pass
    for m in RE_NEXT_MONTH.finditer(text):
        dy = int(m.group(1))
        y, mo = (msg_date.year, msg_date.month + 1)
        if mo > 12:
            y, mo = y + 1, 1
        try:
            out.append(date(y, mo, dy))
        except ValueError:
            pass
    for m in RE_MONTH_END.finditer(text):
        y, mo = msg_date.year, msg_date.month
        if (m.group(1) or "") == "下":
            mo += 1
            if mo > 12:
                y, mo = y + 1, 1
        nxt = date(y + (mo == 12), 1 if mo == 12 else mo + 1, 1)
        out.append(nxt - timedelta(days=1))

    # X.X 形式，只在附近有"截止/前"字样时才认，避免把 9.23 元/版本号当日期
    if has_deadline_word:
        for m in RE_DOT.finditer(text):
            mo, dy = int(m.group(1)), int(m.group(2))
            if not (1 <= mo <= 12 and 1 <= dy <= 31):
                continue
            y = msg_date.year
            if mo < msg_date.month - 6:
                y += 1
            try:
                out.append(date(y, mo, dy))
            except ValueError:
                pass
        for m in RE_MD_SLASH.finditer(text):
            mo, dy = int(m.group(1)), int(m.group(2))
            if not (1 <= mo <= 12 and 1 <= dy <= 31):
                continue
            try:
                out.append(date(msg_date.year, mo, dy))
            except ValueError:
                pass

    # 去重、只留 >= 消息当天（回溯太早的没意义）
    seen, res = set(), []
    for d in out:
        if d in seen:
            continue
        seen.add(d)
        res.append(d)
    res.sort()
    return res


def is_action(text: str) -> bool:
    return any(v in text for v in ACTION_VERBS)


def score(rec: dict, dl: date | None) -> int:
    s = 0
    t = rec["text"]
    if dl:
        days = (dl - TODAY).days
        if days < 0:
            s -= 100  # 已过期
        elif days <= 3:
            s += 60
        elif days <= 7:
            s += 45
        elif days <= 30:
            s += 30
        else:
            s += 15
    if is_action(t):
        s += 10
    if any(n in t for n in NOISE_HINTS):
        s -= 5
    if len(t) < 8:
        s -= 10  # 只有个「@所有人」的空炮
    return s


def load():
    data = json.loads(SRC.read_text(encoding="utf-8"))
    rows = []
    for gkey, msgs in data.items():
        m = re.match(r"^(.*)\((\d+)\)$", gkey)
        gname, gid = (m.group(1).strip(), m.group(2)) if m else (gkey, "")
        for rec in msgs:
            try:
                dt = datetime.strptime(rec["time"], "%Y-%m-%d %H:%M")
            except (ValueError, KeyError):
                continue
            raw = (rec.get("text") or "").replace("「@所有人」", "").strip()
            text = re.sub(r"\s+", " ", raw)
            ctx_text = re.sub(r"\s+", " ", (rec.get("ctx") or "").strip())
            if not ctx.body_of(text) and not ctx_text:
                continue                                   # 只喊了 @所有人，上文也找不到
            text = ctx_text or text                        # 正文在上文里就用上文
            dls = extract_deadlines(text, dt.date())
            future = [d for d in dls if d >= dt.date()]
            dl = future[-1] if future else None  # 取最晚的那个当"最终截止"
            rows.append(
                {
                    "group": gname,
                    "gid": gid,
                    "time": dt,
                    "sender": rec.get("sender", ""),
                    "text": text,
                    "dl": dl,
                    "dl_all": future,
                    "topic": topic_of(text),
                    "score": 0,
                }
            )
    for r in rows:
        r["score"] = score(r, r["dl"])
    rows.sort(key=lambda r: (r["time"], r["score"]), reverse=True)
    return rows


def fmt_dl(dl: date) -> str:
    days = (dl - TODAY).days
    if days < 0:
        tag = f"已过 {-days} 天"
    elif days == 0:
        tag = "今天"
    elif days == 1:
        tag = "明天"
    else:
        tag = f"还有 {days} 天"
    return f"{dl.month:02d}-{dl.day:02d}（周{weekday_of(dl)}，{tag}）"


def line(r: dict, show_group=True) -> str:
    body = r["text"]
    if len(body) > 210:
        body = body[:210] + "…"
    g = f"**[{r['group']}]** " if show_group else ""
    head = f"- {g}`{r.get('topic','综合')}` {r['time']:%m-%d %H:%M} ｜ {r['sender']}"
    s = f"{head}\n  {body}"
    if r["dl"]:
        s += f"\n  ↳ **截止 {fmt_dl(r['dl'])}**"
    return s


def main() -> int:
    global TODAY, SRC
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="", help="基准日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--days", type=int, default=30, help="只看最近 N 天的消息（0=不限）")
    ap.add_argument("--src", default="", help="输入 json，默认取最新的 群公告_*.json")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    TODAY = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else date.today()
    DAYS = a.days
    SRC = Path(a.src) if a.src else pick_src()

    if not SRC.exists():
        print(f"[提示] 还没抓到数据（找不到 {SRC.name}）—— 先跑一次 更新一次.cmd")
        return 1

    all_rows = load()
    if not all_rows:
        print("没有数据")
        return 1

    # 窗口内 + 不过期，这才是要展示的
    rows, expired, older = [], [], []
    for r in all_rows:
        if DAYS and (TODAY - r["time"].date()).days > DAYS:
            continue
        dl = r["dl"]
        if dl is not None and (dl - TODAY).days < 0:
            expired.append(r)          # 过期的不展示，只计数
        else:
            rows.append(r)

    groups = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)

    # 分桶：到期 / 无期限 / 远期
    soon, later, pending = [], [], []
    for r in rows:
        dl = r["dl"]
        if dl is None:
            pending.append(r)
        elif (dl - TODAY).days <= 14:
            soon.append(r)
        else:
            later.append(r)

    soon.sort(key=lambda r: (r["dl"], -r["score"]))
    later.sort(key=lambda r: (r["dl"], -r["score"]))
    pending.sort(key=lambda r: r["time"], reverse=True)

    assert len(soon) + len(later) + len(pending) == len(rows)

    L = []
    L.append(f"# 群 @所有人 待办提醒　{TODAY:%Y-%m-%d}\n")
    L.append(f"只看**最近 {DAYS} 天**、**没过期**的。已过期和一个月前的自动隐藏。\n")
    L.append(
        f"本次视窗内 **{len(groups)} 个群**有 @所有人 喊话，"
        f"共 **{len(rows)} 条**（已去掉只有「@所有人」没正文的空炮）。\n"
    )
    L.append(f"- ⏰ 14 天内到期：**{len(soon)} 条** ← 先看这个")
    L.append(f"- 📌 没写明确期限：**{len(pending)} 条**")
    L.append(f"- 🗓 远期（>14 天）：**{len(later)} 条**")
    L.append(f"\n> 另有 {len(expired)} 条已过截止时间，已隐藏。\n")

    # 群索引
    L.append("## 群索引\n")
    L.append("| 群名 | 群号 | @所有人 | 最新一条 |")
    L.append("|---|---|---:|---|")
    for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        latest = max(r["time"] for r in rs)
        L.append(f"| {g} | {rs[0]['gid']} | {len(rs)} | {latest:%Y-%m-%d} |")
    L.append("")

    if soon:
        L.append(f"## ⏰ 14 天内到期（{len(soon)} 条）\n")
        cur = None
        for r in soon:
            if r["dl"] != cur:
                cur = r["dl"]
                L.append(f"\n### {fmt_dl(cur)}\n")
            L.append(line(r))
            L.append("")

    if pending:
        L.append(f"\n## 📌 没写明确期限（{len(pending)} 条）\n")
        L.append("这些也得看一眼，多半是要你填表/报名/参会，只是没写死日期。\n")
        for r in pending:
            L.append(line(r))
            L.append("")

    if later:
        L.append(f"\n## 🗓 远期（>14 天，{len(later)} 条）\n")
        for r in later:
            L.append(line(r))
            L.append("")

    L.append("\n---\n")
    L.append(f"生成时间 {datetime.now():%Y-%m-%d %H:%M}｜截止时间由规则自动识别，可能有误，请以原文为准。")

    out = Path(a.out) if a.out else settings.data_dir() / f"待办提醒_{TODAY:%Y-%m-%d}.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"OK -> {out.name}  展示 {len(rows)} 条 / {len(groups)} 群（已隐藏过期 {len(expired)} 条）")
    print(f"  14天内 {len(soon)} / 无期限 {len(pending)} / 远期 {len(later)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
