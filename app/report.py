# -*- coding: utf-8 -*-
"""把群 @所有人 数据渲染成一个自包含的 HTML 看板。

布局：**左右两列** —— 左 QQ、右微信，各列独立，绝不混排。
功能：搜索 / 按群筛 / 只看带截止时间 / 标记已完成 / 隐藏已完成。

「已完成」状态的持久化
----------------------
服务在跑时以 data/board_state.json 为唯一真相；直接双击打开 HTML（没有服务）时
退回 localStorage + cookie（cookie 用 path=/，跨端口有效），因为预览面板每次的
127.0.0.1:<随机端口> 会换源，单靠 localStorage 会丢。

标记过「完成」的，重出看板时直接不写进 HTML —— 见 collect() 里的 done 过滤。

模板用 %%占位符%% 替换，避免 f-string 与 CSS、JS 大括号互相打架。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

import ctx
import settings
from version import __version__ as VERSION

BASE = Path(__file__).resolve().parent
DATA = settings.data_dir()
SRC = DATA / "群公告_最新.json"      # 兜底名，正常会由 pick_src() 找到最新那份
OUT = settings.board_file()


ASSETS = settings.assets_dir()
BRAND = "小贾AI群看板"
TAGLINE = "QQ · 微信 双通道 @所有人 汇总"

_uri_cache: dict[str, str] = {}


def asset_uri(name: str) -> str:
    """把 assets 里的图转成 data: URI —— 看板始终是单文件，发给别人也能带图。"""
    if name in _uri_cache:
        return _uri_cache[name]
    p = ASSETS / name
    if not p.exists():
        _uri_cache[name] = ""
        return ""
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    uri = f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
    _uri_cache[name] = uri
    return uri

from digest import extract_deadlines, pick_src, topic_of  # noqa: E402

SRC_CLS = {"QQ": "bqq", "微信": "bwx"}


# --------------------------------------------------------------------------- 数据
def pick_wx_src() -> Path | None:
    """最新的微信侧抓取结果（没有就返回 None，看板照常只显示 QQ）。"""
    files = sorted(DATA.glob("微信群公告_*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def collect(today: date, days: int = 30, sources=None) -> list[dict]:
    if sources is None:
        sources = [(pick_src(), "QQ")]      # 每天文件名带日期，必须取最新那份
        wx = pick_wx_src()
        if wx and wx.exists():
            sources.append((wx, "微信"))

    rows: list[dict] = []
    dropped_old = dropped_expired = dropped_bare = 0
    for path, label in sources:
        path = Path(path)
        if not path.exists():
            print(f"[跳过] 找不到 {path.name} —— {label} 侧还没抓过")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for gkey, msgs in data.items():
            m = re.match(r"^(.*?)\s*\(([^)]+)\)$", gkey)
            gname, gid = (m.group(1).strip(), m.group(2)) if m else (gkey, "")
            for rec in msgs:
                try:
                    dt = datetime.strptime(rec["time"], "%Y-%m-%d %H:%M")
                except (ValueError, KeyError):
                    continue
                if days and (today - dt.date()).days > days:
                    dropped_old += 1
                    continue                                   # 一个月以前的不展示
                raw = (rec.get("text") or "").replace("「@所有人」", "").strip()
                text = re.sub(r"\s+", " ", raw)
                ctx_text = re.sub(r"\s+", " ", (rec.get("ctx") or "").strip())
                if not ctx.body_of(text) and not ctx_text:
                    dropped_bare += 1                          # 只喊了 @所有人，上文也找不到
                    continue
                analysis = ctx_text or text
                dls = [d for d in extract_deadlines(analysis, dt.date()) if d >= dt.date()]
                dl = dls[-1].isoformat() if dls else None
                if dl and date.fromisoformat(dl) < today:
                    dropped_expired += 1
                    continue                                   # 过期的不要展示
                rows.append(
                    {
                        "g": gname,
                        "gid": gid,
                        "t": rec["time"],
                        "s": rec.get("sender", ""),
                        "x": text if ctx.body_of(text) else "",
                        "c": ctx_text,
                        "cf": rec.get("ctx_from", ""),
                        "d": dl,
                        "n": (date.fromisoformat(dl) - today).days if dl else None,
                        "k": topic_of(analysis),
                        "src": label,
                    }
                )
    rows.sort(key=lambda r: r["t"], reverse=True)

    # 稳定 id：只用「不会随重跑变化」的字段。正文取前 40 字，避免同人同分钟撞车。
    seen: dict[str, int] = {}
    for r in rows:
        seed = f"{r['src']}|{r['gid']}|{r['t']}|{r['s']}|{r['x'][:40]}"
        base = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
        n = seen.get(base, 0)
        seen[base] = n + 1
        r["i"] = base if n == 0 else f"{base}-{n}"

    # 已标记「完成」的，重出看板时直接不进去（点一次完成、刷新一次就收起来）。
    # 注意：id 必须在这之后才算得出来，所以只能先算 id 再筛。
    try:
        import board_state
        done_ids = set(board_state.load().get("done") or [])
    except Exception:
        done_ids = set()
    dropped_done = 0
    ghost: dict[str, dict] = {}
    if done_ids:
        kept = []
        for r in rows:
            if r["i"] in done_ids:
                dropped_done += 1
                k = f"{r['src']}|{r['gid']}|{r['g']}"
                g = ghost.setdefault(k, {"src": r["src"], "g": r["g"],
                                         "gid": r["gid"], "n": 0})
                g["n"] += 1
            else:
                kept.append(r)
        rows = kept

    # 整个群都被收起来的，留个空壳。否则用户看到的是「群怎么少了」，
    # 而不是「这几条我标记过了」—— 同一个事实，两种读法，得选对的那个。
    alive = {f"{r['src']}|{r['gid']}|{r['g']}" for r in rows}
    collect.shadow = [v for k, v in ghost.items() if k not in alive]
    collect.dropped = (dropped_old, dropped_expired, dropped_bare, dropped_done)
    return rows


def esc(s: str) -> str:
    return html.escape(str(s))


# --------------------------------------------------------------------------- 模板
TPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%%BRAND%% · %%TODAY%%</title>
<link rel="icon" href="%%FAVICON%%">
<style>
*{box-sizing:border-box}
:root{--bg:#f6f7f9;--panel:#fff;--line:#e6e8ec;--tx:#1a1d24;--tx2:#6b7280;
      --ac:#2563eb;--hot:#dc2626;--warn:#ea580c;--ok:#16a34a;--brand:#0f9d6e}
body{margin:0;background:var(--bg);color:var(--tx);
     font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{padding:16px 26px 14px;background:var(--panel);border-bottom:1px solid var(--line);
       position:sticky;top:0;z-index:20}
.brand{display:flex;align-items:center;gap:11px;margin-bottom:6px}
.brand img{width:42px;height:42px;border-radius:12px;object-fit:cover;flex:none;
       border:1px solid #d7f0e5;box-shadow:0 2px 10px rgba(15,157,110,.22)}
.brand .tt{min-width:0}
h1{margin:0;font-size:19px;font-weight:680;letter-spacing:.4px;line-height:1.3}
h1 .tag{display:inline-block;margin-left:7px;padding:1px 7px;border-radius:999px;
       font-size:10.5px;font-weight:600;letter-spacing:.5px;vertical-align:2px;
       color:#0b7a56;background:#e7f7f0;border:1px solid #cdeee0}
.sub{color:var(--tx2);font-size:12.5px}

.stats{display:flex;gap:22px;margin-top:10px;flex-wrap:wrap}
.stat b{font-size:17px;display:block;line-height:1.2}
.stat span{color:var(--tx2);font-size:11.5px}
.bar{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap;align-items:center}
input[type=search],select{padding:7px 11px;border:1px solid var(--line);border-radius:8px;
       font-size:13px;background:#fff;color:var(--tx);outline:none;font-family:inherit}
input[type=search]{width:280px}
input[type=search]:focus,select:focus{border-color:var(--ac)}
label.chk{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--tx);
       cursor:pointer;user-select:none;white-space:nowrap}
.saver{font-size:11.5px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);
       color:var(--tx2);background:#fafbfc}
.saver.ok{color:#166534;background:#f0fdf4;border-color:#bbf7d0}

/* ---- 顶部动作按钮 ---- */
.acts{margin-left:auto;display:flex;gap:8px;align-items:center;flex:none}
.btn-act{display:inline-flex;align-items:center;gap:6px;padding:7px 13px;border-radius:9px;
  border:1px solid var(--line);background:#fff;color:var(--tx);font-size:13px;
  font-family:inherit;cursor:pointer;white-space:nowrap;transition:.15s}
.btn-act:hover{border-color:#b9e6d4;background:#f7fdfb}
.btn-act.primary{background:var(--brand);border-color:var(--brand);color:#fff;font-weight:600}
.btn-act.primary:hover{background:#0c8a61;border-color:#0c8a61}
.btn-act:disabled{opacity:.65;cursor:default}
.btn-act .spin{width:12px;height:12px;border:2px solid rgba(255,255,255,.45);
  border-top-color:#fff;border-radius:50%;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}

/* ---- 底部控制台 ---- */
.console{position:fixed;right:16px;bottom:16px;width:min(640px,calc(100vw - 32px));
  max-height:46vh;background:#0f172a;color:#d7e2f0;border-radius:12px;z-index:60;
  box-shadow:0 18px 48px rgba(15,23,42,.34);display:none;flex-direction:column;overflow:hidden}
.console.show{display:flex}
.console .ch{display:flex;align-items:center;gap:8px;padding:9px 13px;background:#16233c;
  font-size:12.5px;font-weight:600;color:#e8eef8}
.console .ch .dot{width:7px;height:7px;border-radius:50%;background:#22c55e;flex:none}
.console .ch .dot.run{background:#f59e0b;animation:bl 1s infinite}
@keyframes bl{50%{opacity:.35}}
.console .ch .x{margin-left:auto;cursor:pointer;color:#8fa3bf;font-size:15px;line-height:1}
.console .ch .x:hover{color:#fff}
.console .cbody{padding:10px 13px;overflow:auto;
  font:12px/1.6 ui-monospace,Consolas,"Courier New",monospace;
  white-space:pre-wrap;word-break:break-all}
.console .cbody span{display:block}

/* ---- 演示横幅 ---- */
.demobar{background:#fef3c7;border-bottom:1px solid #fde68a;color:#92400e;
  font-size:12.5px;padding:7px 26px;display:flex;gap:10px;align-items:center}
.demobar b{font-weight:650}
.demobar a{margin-left:auto;color:#92400e;font-weight:600;text-decoration:none}
.demobar a:hover{text-decoration:underline}
.saver.bad{color:#92400e;background:#fffbeb;border-color:#fcd34d}

/* ---- 更新提示条 ---- */
.updbar{background:linear-gradient(90deg,#0f766e,#0d9488);color:#fff;
  font-size:12.5px;padding:8px 26px;display:none;gap:10px;align-items:center}
.updbar.show{display:flex}
.updbar b{font-weight:650;white-space:nowrap}
.updbar .ub-ic{display:inline-flex;width:17px;height:17px;border-radius:50%;flex:none;
  background:rgba(255,255,255,.22);align-items:center;justify-content:center;font-size:11px}
.updbar .ub-sub{color:#ccfbf1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.updbar .ub-btn{padding:4px 11px;border-radius:7px;border:1px solid rgba(255,255,255,.45);
  background:transparent;color:#fff;font-size:12px;font-family:inherit;cursor:pointer;flex:none}
.updbar .ub-btn:hover{background:rgba(255,255,255,.14)}
.updbar .ub-btn.primary{background:#fff;color:#0f766e;font-weight:600;border-color:#fff}
.updbar .ub-btn.primary:hover{background:#f0fdfa}
.updbar .ub-x{margin-left:auto;cursor:pointer;opacity:.75;font-size:15px;line-height:1;flex:none}
.updbar .ub-x:hover{opacity:1}

/* ---- 更新内容弹层 ---- */
.modal{position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:80;
  display:none;align-items:center;justify-content:center;padding:24px}
.modal.show{display:flex}
.modal .mbox{background:#fff;border-radius:14px;width:min(620px,100%);max-height:78vh;
  display:flex;flex-direction:column;overflow:hidden;box-shadow:0 24px 64px rgba(15,23,42,.3)}
.modal .mh{display:flex;align-items:center;gap:8px;padding:14px 18px;font-size:14px;
  border-bottom:1px solid var(--line)}
.modal .mh span{margin-left:auto;cursor:pointer;color:var(--tx2);font-size:16px;line-height:1}
.modal .mb{padding:12px 18px;overflow:auto;font-size:13px;line-height:1.7}
.modal .mb .row{padding:7px 0;border-bottom:1px dashed var(--line);display:flex;gap:10px}
.modal .mb .row:last-child{border-bottom:0}
.modal .mb .cd{color:var(--tx2);font-size:11.5px;flex:none;width:78px;padding-top:2px}
.modal .mb .cm{flex:1;min-width:0;word-break:break-word}
.modal .mf{padding:11px 18px;border-top:1px solid var(--line);display:flex;
  gap:10px;align-items:center}
.modal .mf a{color:var(--ac);font-size:12.5px;text-decoration:none}
.modal .mf a:hover{text-decoration:underline}
.modal .mf .ub-btn{margin-left:auto;padding:6px 14px;border-radius:8px;font-size:13px;
  border:1px solid var(--line);background:#fff;color:var(--tx);font-family:inherit;cursor:pointer}
.modal .mf .ub-btn.primary{background:var(--brand);border-color:var(--brand);color:#fff;font-weight:600}
.ver{color:var(--tx2);font-size:11.5px}

/* ---- 数据源面板 ---- */
.offtag{font-size:10.5px;padding:1px 7px;border-radius:20px;background:#fef2f2;color:#b91c1c;
  border:1px solid #fecaca;cursor:pointer;white-space:nowrap;transform:translateY(-1px)}
.offtag:hover{background:#fee2e2}
#srcModal .mbox{width:min(660px,100%)}
.srcblk{border:1px solid var(--line);border-radius:11px;padding:12px 14px;margin-bottom:12px}
.srcblk.off{background:#fbfbfc;opacity:.62}
.srcblk .sh{display:flex;align-items:center;gap:9px;margin-bottom:2px}
.srcblk .sh b{font-size:13.5px}
.srcblk .sh .st{font-size:11.5px;color:var(--tx2);margin-left:auto;margin-right:2px}
.srcblk.off .sh .st{color:var(--hot)}
.srcblk .desc{font-size:11.5px;color:var(--tx2);line-height:1.6;margin:3px 0 9px}
.fld{display:flex;gap:9px;align-items:center;margin:7px 0 0}
.fld>label{flex:none;width:64px;font-size:12px;color:var(--tx2)}
.fld select,.fld input[type=text]{flex:1;min-width:0;font-family:inherit;font-size:12.5px;
  padding:6px 9px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--tx);
  outline:none;text-overflow:ellipsis}
.fld select:focus,.fld input[type=text]:focus{border-color:var(--brand);
  box-shadow:0 0 0 3px rgba(13,148,136,.12)}
.fld .mini{flex:none;font-size:11.5px;color:var(--tx2);white-space:nowrap}
.fld .pick{padding:5px 10px;border-radius:8px;border:1px solid var(--line);background:#fff;
  color:var(--tx);font-family:inherit;font-size:12px;cursor:pointer;flex:none}
.fld .pick:hover{border-color:var(--brand);color:var(--brand)}
/* 开关 */
.sw{position:relative;display:inline-flex;width:40px;height:22px;flex:none;cursor:pointer}
.sw input{position:absolute;opacity:0;width:0;height:0}
.sw i{position:absolute;inset:0;background:#cbd5e1;border-radius:22px;transition:background .18s}
.sw i::after{content:'';position:absolute;left:2px;top:2px;width:18px;height:18px;border-radius:50%;
  background:#fff;box-shadow:0 1px 3px rgba(15,23,42,.28);transition:transform .18s}
.sw input:checked+i{background:var(--brand)}
.sw input:checked+i::after{transform:translateX(18px)}
.sw input:focus-visible+i{box-shadow:0 0 0 3px rgba(13,148,136,.22)}
.srchint{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:9px;
  padding:9px 12px;font-size:12.5px;line-height:1.65;margin-bottom:12px}
.srchint b{display:block;margin-bottom:2px}
.srchint .h{display:flex;gap:6px}
.srchint .h::before{content:'·';flex:none}
.srcmsg{font-size:12px;border-radius:8px;padding:8px 11px;margin-top:2px;display:none}
.srcmsg.show{display:block}
.srcmsg.ok{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0}
.srcmsg.err{background:#fef2f2;color:#991b1b;border:1px solid #fecaca}

/* —— 两列 —— */
main{padding:18px 26px 60px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:start}
.col{background:transparent;min-width:0}
.col-h{display:flex;align-items:baseline;gap:9px;padding:0 2px 9px;
       border-bottom:2px solid var(--line);margin-bottom:13px}
.col-h .ttl{font-size:15px;font-weight:700}
.col-h .n{font-size:11.5px;color:var(--tx2);margin-left:auto;white-space:nowrap}
.src{font-size:10.5px;padding:1.5px 7px;border-radius:5px;font-weight:600}
.bqq{background:#dbeafe;color:#1e40af}
.bwx{background:#dcfce7;color:#166534}
.col-none{color:var(--tx2);font-size:12.5px;padding:18px;text-align:center;
          border:1px dashed var(--line);border-radius:10px;background:#fcfcfd}

.cards{display:grid;grid-template-columns:1fr;gap:10px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
      padding:12px 14px;border-left:3px solid var(--ac);transition:.18s}
.card.hot{border-left-color:var(--hot)}
.card.warn{border-left-color:var(--warn)}
.card-h{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px}
.badge{font-size:10.5px;padding:1.5px 7px;border-radius:5px;background:#eef2ff;color:#3730a3}
.dl{font-size:11.5px;font-weight:600;color:var(--hot);white-space:nowrap}
.card.warn .dl{color:var(--warn)}
.card-t{font-size:12.5px;line-height:1.6;display:-webkit-box;-webkit-line-clamp:3;
        -webkit-box-orient:vertical;overflow:hidden}
.card-f{margin-top:8px;font-size:10.5px;color:var(--tx2);display:flex;
        justify-content:space-between;align-items:flex-end;gap:8px}
.card-f .who{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card.done{opacity:.5;border-left-color:#9ca3af;background:#fafafa}
.card.done .card-t{text-decoration:line-through;text-decoration-color:#9ca3af;color:var(--tx2)}

.btn-done,.btn-reset{flex:none;border:1px solid var(--line);background:#fff;color:var(--tx2);
        font:inherit;font-size:11px;line-height:1.4;padding:2.5px 8px;border-radius:6px;
        cursor:pointer;white-space:nowrap;transition:.15s}
.btn-done:hover{border-color:var(--ok);color:var(--ok);background:#f0fdf4}
.btn-done.on{background:var(--ok);border-color:var(--ok);color:#fff}
.btn-done.on:hover{background:#15803d;border-color:#15803d;color:#fff}
.btn-reset{margin-left:auto}
.btn-reset:hover{border-color:#9ca3af;color:var(--tx)}

.grp{background:var(--panel);border:1px solid var(--line);border-radius:10px;
     margin-bottom:9px;overflow:hidden}
.grp>summary{padding:10px 13px;cursor:pointer;display:flex;justify-content:space-between;
     gap:10px;align-items:baseline;list-style:none;font-weight:600;font-size:13px}
.grp>summary::-webkit-details-marker{display:none}
.grp>summary:hover{background:#fafbfc}
.gname{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gmeta{font-size:10.5px;color:var(--tx2);font-weight:400;white-space:nowrap}
/* 整个群都被「已完成」收起来之后的空壳：群还在，只是个提示 */
.grp.ghost{background:#fcfcfd;border-style:dashed}
.grp.ghost>summary{font-weight:500;color:var(--tx2)}
.grp.ghost>summary:hover{background:#f7f8fa}
.ghost-body{padding:9px 13px;border-top:1px dashed var(--line);
  font-size:12px;color:var(--tx2);line-height:1.7}
.ghost-body b{color:var(--ok);font-weight:600}
.msg{padding:9px 13px;border-top:1px solid #f1f2f5;transition:.15s}
.msg:hover{background:#fafbfc}
.msg-h{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;
       font-size:11px;color:var(--tx2);margin-bottom:3px}
.msg-h b{color:#374151;font-weight:600;font-size:11.5px}
.msg-h .btn-done{margin-left:auto}
.msg-x{font-size:12.5px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
/* 「先发正文、再单独 @所有人」的补全：正文是从上文捞回来的，得让人看得出来 */
.msg-c{margin-top:3px;padding:5px 8px;border-left:2px solid #cbd5e1;border-radius:0 4px 4px 0;
     background:#f8fafc;color:#4b5563;font-size:12px;line-height:1.6;
     white-space:pre-wrap;word-break:break-word}
.msg-c span{display:block;margin-top:2px;color:#9aa3b2;font-size:10.5px}
.msg-cf{margin-top:3px;color:#9aa3b2;font-size:10.5px}
.msg.done{opacity:.45}
.msg.done .msg-x{text-decoration:line-through;text-decoration-color:#9ca3af}
.tag{font-size:10px;padding:1px 6px;border-radius:4px;background:#f3f4f6;color:#4b5563}
.dtag{font-size:10px;padding:1px 6px;border-radius:4px;background:#fef2f2;
      color:#b91c1c;font-weight:600}
mark{background:#fff3a3;padding:0 1px;border-radius:2px}
.empty{color:var(--tx2);padding:22px;text-align:center;background:var(--panel);
       border:1px dashed var(--line);border-radius:10px;font-size:12.5px}
.warn{margin:12px 26px 0;padding:10px 13px;background:#fffbeb;border:1px solid #fcd34d;
      border-radius:9px;color:#92400e;font-size:12.5px;line-height:1.7}
.warn b{color:#78350f}

/* —— 取消关注 —— */
.btn-mute{flex:none;border:1px solid transparent;background:transparent;color:#c3c7cf;
      font:inherit;font-size:11px;line-height:1;padding:3px 6px;border-radius:5px;
      cursor:pointer;transition:.15s}
.btn-mute:hover{color:var(--hot);border-color:#fecaca;background:#fef2f2}
.mutedbox{margin-left:2px}
.mutedbox>summary{cursor:pointer;list-style:none;font-size:12px;color:var(--tx2);
      padding:4px 9px;border:1px solid var(--line);border-radius:999px;background:#fafbfc;
      white-space:nowrap}
.mutedbox>summary::-webkit-details-marker{display:none}
.mutedbox>summary:hover{border-color:#9ca3af;color:var(--tx)}
.mutedbox[open]>summary{border-color:var(--ac);color:var(--ac)}
.mutedpanel{position:absolute;margin-top:6px;background:#fff;border:1px solid var(--line);
      border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.1);padding:8px;min-width:260px;
      max-height:320px;overflow:auto;z-index:30;max-width:calc(100vw - 60px)}
.mutedpanel .mrow{display:flex;align-items:center;gap:8px;padding:5px 7px;border-radius:7px;
      font-size:12.5px}
.mutedpanel .mrow:hover{background:#fafbfc}
.mutedpanel .mrow span.mn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
      white-space:nowrap}
.mutedpanel .none{color:var(--tx2);font-size:12px;padding:8px 7px}
.btn-back{flex:none;border:1px solid var(--line);background:#fff;color:var(--tx2);
      font:inherit;font-size:11px;padding:2px 8px;border-radius:6px;cursor:pointer}
.btn-back:hover{border-color:var(--ok);color:var(--ok);background:#f0fdf4}
/* 页脚：服务在后台跑，这里给个低调的停止入口 */
.foot{max-width:1240px;margin:-6px auto 30px;padding:0 26px;display:none;
      align-items:center;gap:10px;flex-wrap:wrap;
      color:#9aa3b2;font-size:12px;line-height:1.7}
.foot .sp{opacity:.55}
.foot a{color:#9aa3b2;text-decoration:none;border-bottom:1px dashed currentColor;
      cursor:pointer;transition:color .15s}
.foot a:hover{color:var(--hot);border-bottom-color:var(--hot)}
.bye{max-width:560px;margin:120px auto;padding:0 24px;text-align:center;
      color:var(--tx);font-size:19px;font-weight:640;line-height:2.2}
.bye span{display:block;color:var(--tx2);font-size:13.5px;font-weight:400}
@media(max-width:900px){
  .cols{grid-template-columns:1fr}
  main,header{padding-left:14px;padding-right:14px}
  input[type=search]{width:100%}
  .gmeta{display:none}
  .foot{padding-left:14px;padding-right:14px}
}
</style></head><body>
%%DEMOBAR%%
<div class="updbar" id="updbar">
  <span class="ub-ic">↑</span>
  <b>有新版本</b>
  <span class="ub-sub" id="updSub"></span>
  <button class="ub-btn" id="updWhat">更新了什么</button>
  <button class="ub-btn primary" id="updGo">立即更新</button>
  <span class="ub-x" id="updSkip" title="这次不提醒">✕</span>
</div>
<header>
  <div class="brand">
    <img src="%%LOGO%%" alt="小贾">
    <div class="tt">
      <h1>%%BRAND%%<span class="tag">QQ · 微信</span></h1>
      <div class="sub">抓取时间 %%NOW%% ｜ 只显示最近 <b>%%DAYS%%</b> 天的、未过期的 ｜ %%DROPPED%%</div>
    </div>
    <div class="acts" id="acts">
      <button class="btn-act primary" id="btnRefresh">↻ 刷新数据</button>
      <button class="btn-act" id="btnSrc">数据源</button>
      <button class="btn-act" id="btnDoctor">环境体检</button>
      <button class="btn-act" id="btnDemo">看示例</button>
    </div>
  </div>
  <div class="stats">
    <div class="stat"><b>%%TOTAL%%</b><span>@所有人 消息</span></div>
    <div class="stat"><b>%%GN%%</b><span>有喊话的群</span></div>
    <div class="stat"><b style="color:var(--hot)">%%SOON%%</b><span>14 天内到期</span></div>
    <div class="stat" title="标记过「完成」的都收起来了，点条目的「完成」按钮可标记"><b id="doneN" style="color:var(--ok)">0</b><span>已完成</span></div>
  </div>
  <div class="bar">
    <input type="search" id="q" placeholder="搜内容 / 发件人 / 群名，空格分隔多个词">
    <select id="gsel"><option value="">全部群</option></select>
    <label class="chk"><input type="checkbox" id="onlyTodo"> 只看带截止时间的</label>
    <label class="chk"><input type="checkbox" id="hideDone"> 隐藏已完成</label>
    <span class="saver" id="saver">状态检测中…</span>
    <details class="mutedbox" id="mutedBox">
      <summary>已取消关注 <b id="muteN">0</b> 个群</summary>
      <div class="mutedpanel" id="mutedList"></div>
    </details>
    <button class="btn-reset" id="resetDone">重置已完成</button>
  </div>
</header>
<div class="warn" id="warn" style="display:none">
  <b>⚠️ 这个环境存不住「已完成」标记</b>（浏览器禁止沙箱页面写本地存储），刷新就会丢。<br>
  请关掉预览，直接双击打开 <code>%%BOARDFILE%%</code>，在正常浏览器窗口里标记就能记住。
</div>
<main>
  <div class="cols">
    <section class="col" id="colQQ">
      <div class="col-h"><span class="src bqq">QQ</span><span class="ttl">QQ 群</span>
        <span class="offtag" id="offQQ" style="display:none">读取已关闭</span>
        <span class="n" id="nQQ"></span></div>
      <div class="cards" id="soonQQ"></div>
      <div id="listQQ"></div>
    </section>
    <section class="col" id="colWX">
      <div class="col-h"><span class="src bwx">微信</span><span class="ttl">微信群</span>
        <span class="offtag" id="offWX" style="display:none">读取已关闭</span>
        <span class="n" id="nWX"></span></div>
      <div class="cards" id="soonWX"></div>
      <div id="listWX"></div>
    </section>
  </div>
</main>

<footer class="foot" id="foot">
  <span>数据只在你本机 · 关掉这个页面不会停服务 · <span class="ver">v%%VERSION%%</span></span>
  <span class="sp">|</span>
  <a id="btnQuit">停止看板服务</a>
</footer>

<div class="modal" id="srcModal">
  <div class="mbox">
    <div class="mh"><b>数据源</b><span id="srcClose">✕</span></div>
    <div class="mb">
      <div class="srchint" id="srcHint" style="display:none"></div>

      <div class="srcblk" id="blkQQ">
        <div class="sh">
          <label class="sw"><input type="checkbox" id="qqOn"><i></i></label>
          <b>QQ 群</b>
          <span class="st" id="qqSt">已开启</span>
        </div>
        <div class="desc">靠 NapCat 读本机 QQ 的消息。关掉就完全不碰 QQ —— 觉得有风险时用这个。</div>
        <div class="fld">
          <label>QQ 程序</label>
          <select id="qqExe"></select>
          <button class="pick" id="qqExePick" title="手动选 QQ.exe">选…</button>
        </div>
        <div class="fld">
          <label>用哪个号</label>
          <select id="qqUin"></select>
        </div>
      </div>

      <div class="srcblk" id="blkWX">
        <div class="sh">
          <label class="sw"><input type="checkbox" id="wxOn"><i></i></label>
          <b>微信</b>
          <span class="st" id="wxSt">已开启</span>
        </div>
        <div class="desc">直接从本机微信数据库解密读群消息，不登录第三方、不走网络。</div>
        <div class="fld">
          <label>用哪个号</label>
          <select id="wxDir"></select>
          <button class="pick" id="wxDirPick" title="手动选数据目录">选…</button>
        </div>
        <div class="fld">
          <label>实际路径</label>
          <input type="text" id="wxPathShow" readonly placeholder="（还没选）">
        </div>
      </div>

      <div class="srcmsg" id="srcMsg"></div>
      <div style="font-size:11.5px;color:var(--tx2);line-height:1.7;margin-top:4px">
        改完点「保存」，再点一次 <b>↻ 刷新数据</b> 才会按新设置去读。<br>
        这些设置写在本机 <code>config.json</code> 里，不上传。
      </div>
    </div>
    <div class="mf">
      <a href="#" id="srcOpenCfg" style="display:none"></a>
      <button class="ub-btn" id="srcCancel">取消</button>
      <button class="ub-btn primary" id="srcSave">保存</button>
    </div>
  </div>
</div>

<div class="modal" id="updModal">
  <div class="mbox">
    <div class="mh"><b>更新内容</b><span id="updClose">✕</span></div>
    <div class="mb" id="updList"></div>
    <div class="mf">
      <a id="updGit" target="_blank" rel="noopener">在 GitHub 上看 →</a>
      <button class="ub-btn primary" id="updGo2">立即更新</button>
    </div>
  </div>
</div>

<div class="console" id="console">
  <div class="ch"><span class="dot" id="cDot"></span><span id="cTitle">输出</span>
    <span class="x" id="cClose">✕</span></div>
  <div class="cbody" id="cBody"></div>
</div>
<script>
const DATA = %%DATA%%;
const GROUPS = %%GROUPSJSON%%;
const SHADOW = %%SHADOW%%;
const SRCOFF = %%SRCOFF%%;
const SERVED = %%SERVED%%;
const DEMO = %%DEMO%%;
const KEY = 'qq_at_done_v3';
const CKEY = 'qad3';

const q = document.getElementById('q');
const gsel = document.getElementById('gsel');
const onlyTodo = document.getElementById('onlyTodo');
const hideDone = document.getElementById('hideDone');
const doneN = document.getElementById('doneN');
const saver = document.getElementById('saver');

/* ---------- 状态存储：localStorage ∪ cookie ---------- */
let lsOK = false, ckOK = false;
try {
  const p = '__probe_' + Date.now();
  localStorage.setItem(p, '1'); localStorage.removeItem(p);
  lsOK = true;
} catch (e) {}

function readCookie() {
  const m = document.cookie.match(/(?:^|;\\s*)qad3=([^;]*)/);
  if (!m) return [];
  try { return JSON.parse(decodeURIComponent(m[1])) || []; } catch (e) { return []; }
}
function writeCookie(ids) {
  try {
    const v = encodeURIComponent(JSON.stringify(ids));
    if (v.length > 3500) return false;            // 超长就不写，避免浏览器截断
    document.cookie = CKEY + '=' + v + ';path=/;max-age=63072000;SameSite=Lax';
    return document.cookie.indexOf(CKEY + '=') !== -1;
  } catch (e) { return false; }
}

/* 初始状态由服务端直接注入，不用异步拉取，页面不闪 */
let done = new Set(%%DONE%% || []);
let muted = new Map();
(%%MUTED%% || []).forEach(m => muted.set(m.src + '|' + m.gid, m));

/* 服务在跑的时候，data/board_state.json 就是唯一真相，不再合并浏览器里的旧标记
   —— 否则「重置已完成」清完，本机残留的 id 又会被合回来，标过的条目反而一直显示成划掉。 */
if (!SERVED) {
  if (lsOK) {
    try { (JSON.parse(localStorage.getItem(KEY) || '[]') || []).forEach(x => done.add(x)); } catch (e) {}
  }
  readCookie().forEach(x => done.add(x));
}
ckOK = writeCookie([...done]);

function refreshSaver() {
  if (SERVED) {
    saver.className = 'saver ok';
    saver.textContent = '已写入本机文件 · 爬虫会跳过已取消的群';
  } else if (lsOK || ckOK) {
    saver.className = 'saver bad';
    saver.textContent = '仅本浏览器有效 · 取消关注不生效，请用「启动看板.cmd」';
  } else {
    saver.className = 'saver bad';
    saver.textContent = '标记仅本次有效';
    document.getElementById('warn').style.display = 'block';
  }
}
refreshSaver();

function save() {
  const st = { done: [...done], muted: [...muted.values()] };
  if (SERVED) {
    fetch('/api/state', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(st)
    }).catch(() => {});
    return;
  }
  const ids = [...done];
  if (lsOK) { try { localStorage.setItem(KEY, JSON.stringify(ids)); } catch (e) { lsOK = false; } }
  ckOK = writeCookie(ids);
  refreshSaver();
}

/* ---------- 取消关注 ---------- */
function muteGroup(src, gid, name) {
  muted.set(src + '|' + gid, { src: src, gid: gid, name: name });
  save(); render(); renderMuted();
}
function unmuteGroup(k) {
  muted.delete(k);
  save(); render(); renderMuted();
}
function renderMuted() {
  document.getElementById('muteN').textContent = muted.size;
  const box = document.getElementById('mutedList');
  if (!muted.size) {
    box.innerHTML = '<div class="none">还没有取消关注的群</div>';
    return;
  }
  box.innerHTML = [...muted.entries()]
    .sort((a, b) => (a[1].name > b[1].name ? 1 : -1))
    .map(([k, m]) =>
      '<div class="mrow"><span class="src ' + (m.src === '微信' ? 'bwx' : 'bqq') + '">' +
        m.src + '</span><span class="mn" title="' + esc(m.name) + '">' + esc(m.name) +
        '</span><button class="btn-back" data-un="' + k + '">恢复</button></div>').join('');
}

Object.keys(GROUPS).sort((a, b) => GROUPS[b].n - GROUPS[a].n).forEach(g => {
  const o = document.createElement('option');
  o.value = g; o.textContent = g + ' (' + GROUPS[g].n + ')' +
    (GROUPS[g].ghost ? ' · 已收起' : '');
  gsel.appendChild(o);
});

const esc = s => String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

function hl(s, qs) {
  let out = esc(s);
  qs.forEach(t => {
    if (!t) return;
    const safe = t.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
    out = out.replace(new RegExp('(' + safe + ')', 'gi'), '<mark>$1</mark>');
  });
  return out;
}

function btnHTML(i) {
  const on = done.has(i);
  return '<button class="btn-done' + (on ? ' on' : '') + '" data-b="' + i + '">' +
         (on ? '✓ 已完成' : '标记完成') + '</button>';
}

function cardHTML(r) {
  const dn = r.n, cls = dn <= 1 ? 'hot' : (dn <= 3 ? 'warn' : '');
  const when = dn === 0 ? '今天' : (dn === 1 ? '明天' : dn + ' 天后');
  return '<div class="card ' + cls + '">' +
    '<div class="card-h"><span class="badge">' + esc(r.k) + '</span>' +
      '<span class="dl">' + r.d.slice(5) + ' · ' + when + '</span></div>' +
    '<div class="card-t">' + esc((r.x || r.c || '').slice(0, 260)) + '</div>' +
    '<div class="card-f"><span class="who">' + esc(r.g) + ' ｜ ' + esc(r.s) + '</span>' +
      btnHTML(r.i) + '</div></div>';
}

function groupHTML(g, rs, qs) {
  const last = rs.reduce((m, r) => r.t > m ? r.t : m, '');
  const src = rs[0].src || 'QQ', gid = rs[0].gid || '';
  const open = (qs.length || onlyTodo.checked || gsel.value || hideDone.checked) ? ' open' : '';
  const body = rs.map(r => {
    const main = r.x || r.c || '';
    const extra = (r.x && r.c)
      ? '<div class="msg-c">↑ 上下文：' + hl(r.c, qs) + '<span>' + esc(r.cf) + '</span></div>'
      : ((!r.x && r.c) ? '<div class="msg-cf">↑ 这条只发了 @所有人，正文取自上下文 · ' +
                          esc(r.cf) + '</div>' : '');
    return '<div class="msg' + (done.has(r.i) ? ' done' : '') + '">' +
      '<div class="msg-h"><b>' + esc(r.s || '未知') + '</b><span>' + r.t + '</span>' +
        '<span class="tag">' + r.k + '</span>' +
        (r.d ? '<span class="dtag">截止 ' + r.d + '</span>' : '') +
        btnHTML(r.i) + '</div>' +
      '<div class="msg-x">' + hl(main, qs) + '</div>' + extra + '</div>';
  }).join('');
  return '<details class="grp"' + open + '><summary>' +
    '<span class="gname">' + esc(g) + '</span>' +
    '<span class="gmeta">' + rs.length + ' 条 · 最新 ' + last.slice(0, 10) + '</span>' +
    '<button class="btn-mute" title="取消关注：以后不再扫描这个群" ' +
      'data-src="' + esc(src) + '" data-gid="' + esc(gid) + '" data-name="' + esc(g) + '">✕</button>' +
    '</summary>' + body + '</details>';
}

function ghostHTML(s) {
  return '<details class="grp ghost"><summary>' +
    '<span class="gname">' + esc(s.g) + '</span>' +
    '<span class="gmeta">' + s.n + ' 条已完成 · 已收起</span>' +
    '</summary><div class="ghost-body">' +
    '这个群的 @所有人 <b>' + s.n + ' 条</b>已经标记成「完成」，所以不再列出来（群还在，不用慌）。' +
    '想找回来：点上方「重置已完成」，再点一次「刷新数据」。</div></details>';
}

const SUF = { 'QQ': 'QQ', '微信': 'WX' };

function renderCol(src, qs, gv, ot, hd) {
  const sfx = SUF[src] || src;
  const rows = DATA.filter(r => (r.src || 'QQ') === src)
    .filter(r => !muted.has(src + '|' + r.gid))
    .filter(r => {
      if (gv && r.g !== gv) return false;
      if (ot && !r.d) return false;
      if (hd && done.has(r.i)) return false;
      if (qs.length) {
        const hay = (r.x + ' ' + (r.c || '') + ' ' + r.s + ' ' + r.g + ' ' + r.k).toLowerCase();
        if (!qs.every(t => hay.includes(t))) return false;
      }
      return true;
    });

  const soon = rows.filter(r => r.n !== null && r.n >= 0 && r.n <= 14)
    .sort((a, b) => (a.d < b.d ? -1 : a.d > b.d ? 1 : (a.t < b.t ? -1 : 1)));
  const buckets = new Map();
  for (const r of rows) {
    if (!buckets.has(r.g)) buckets.set(r.g, []);
    buckets.get(r.g).push(r);
  }
  const order = [...buckets.keys()].sort((a, b) => buckets.get(b).length - buckets.get(a).length);

  /* 空壳群：整群都被「已完成」收起来了。群得留着，不然看板上就是"群怎么少了" */
  const ghosts = SHADOW.filter(s => (s.src || 'QQ') === src)
    .filter(s => !muted.has(src + '|' + s.gid))
    .filter(s => !gv || s.g === gv)
    .filter(s => {
      if (ot) return false;                     // 只看带截止时间的，空壳没意义
      if (!qs.length) return true;
      const hay = (s.g || '').toLowerCase();
      return qs.every(t => hay.includes(t));
    })
    .map(ghostHTML);

  const nGrp = order.length + ghosts.length;
  document.getElementById('soon' + sfx).innerHTML =
    soon.length ? soon.map(cardHTML).join('') : '';
  document.getElementById('list' + sfx).innerHTML = nGrp
    ? order.map(g => groupHTML(g, buckets.get(g), qs)).join('') + ghosts.join('')
    : '<div class="col-none">' + (
        DATA.length ? '没有匹配的记录'
        : (done.size ? '这一侧都标记成「完成」了 —— 点右上角「重置已完成」再刷新，就回来了'
        : (SERVED ? '还没有数据 —— 点右上角「刷新数据」抓一次'
                  : '还没有数据 —— 双击「启动看板.cmd」后点「刷新数据」'))
      ) + '</div>';

  const nEl = document.getElementById('n' + sfx);
  nEl.textContent = rows.length + ' 条 · ' + nGrp + ' 群' +
                    (soon.length ? ' · ' + soon.length + ' 条临近截止' : '');
  return rows.length;
}

function render() {
  const qs = q.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);
  const gv = gsel.value, ot = onlyTodo.checked, hd = hideDone.checked;
  renderCol('QQ', qs, gv, ot, hd);
  renderCol('微信', qs, gv, ot, hd);
  /* 标记过的都算「已完成」，哪怕它已经被收起来（DATA 里已经没有它了） */
  doneN.textContent = done.size;
}

document.addEventListener('click', e => {
  const mb = e.target.closest('.btn-mute');
  if (mb) {
    e.preventDefault(); e.stopPropagation();
    const name = mb.dataset.name, src = mb.dataset.src, gid = mb.dataset.gid;
    const tip = SERVED ? '' : '⚠️ 当前是直接打开的网页文件，这里取消关注只影响显示，\\n不会让爬虫跳过。要真正生效请用「启动看板.cmd」打开。\\n\\n';
    if (confirm(tip + '取消关注「' + name + '」？\\n\\n以后不再扫描这个群，看板上也不再显示它。\\n随时可以在「已取消关注」里恢复。')) {
      muteGroup(src, gid, name);
    }
    return;
  }
  const bk = e.target.closest('.btn-back');
  if (bk) {
    e.preventDefault(); e.stopPropagation();
    unmuteGroup(bk.dataset.un);
    return;
  }
  const b = e.target.closest('.btn-done');
  if (!b) return;
  e.preventDefault();
  const i = b.dataset.b;
  if (done.has(i)) done.delete(i); else done.add(i);
  save();
  render();
});

document.getElementById('resetDone').addEventListener('click', () => {
  if (!done.size) return;
  if (!confirm('清除全部 ' + done.size + ' 条「已完成」标记？\\n\\n清完点「刷新数据」，它们会重新出现在看板上。')) return;
  done.clear(); save(); render();
});

q.addEventListener('input', render);
gsel.addEventListener('change', render);
onlyTodo.addEventListener('change', render);
hideDone.addEventListener('change', render);
renderMuted();
render();

// ================= 服务端动作：刷新 / 体检 / 安装依赖 =================
const btnRefresh = document.getElementById('btnRefresh');
const btnDoctor = document.getElementById('btnDoctor');
const btnDemo = document.getElementById('btnDemo');
const acts = document.getElementById('acts');
const con = document.getElementById('console');
const cBody = document.getElementById('cBody');
const cDot = document.getElementById('cDot');
const cTitle = document.getElementById('cTitle');
document.getElementById('cClose').onclick = () => con.classList.remove('show');

let pollTimer = null;

function conShow(title, running) {
  cTitle.textContent = title;
  cDot.className = 'dot' + (running ? ' run' : '');
  con.classList.add('show');
}
function conClear() { cBody.textContent = ''; }
function conLine(text, color) {
  const sp = document.createElement('span');
  sp.textContent = text;
  if (color) sp.style.color = color;
  cBody.appendChild(sp);
  cBody.scrollTop = cBody.scrollHeight;
  return sp;
}

function pollRefresh() {
  fetch('/api/refresh').then(r => r.json()).then(s => {
    conClear();
    s.lines.forEach(l => {
      let c = '';
      if (l.indexOf('返回') >= 0 || l.indexOf('出错') >= 0 || l.indexOf('失败') >= 0) c = '#fca5a5';
      else if (l.indexOf('完成') >= 0 || l.indexOf('OK') >= 0) c = '#86efac';
      else if (l.indexOf('──') >= 0) c = '#93c5fd';
      conLine(l, c);
    });
    if (s.running) {
      conShow('刷新中… ' + s.elapsed + ' 秒', true);
      return;
    }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    btnRefresh.disabled = false;
    btnRefresh.textContent = '↻ 刷新数据';
    if (s.rc === 0) {
      conShow('刷新完成，正在重载看板…', false);
      setTimeout(() => location.reload(), 1100);
    } else {
      conShow('刷新结束，有报错（见上）', false);
    }
  }).catch(() => {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  });
}

btnRefresh.onclick = () => {
  btnRefresh.disabled = true;
  btnRefresh.textContent = '刷新中…';
  conClear();
  conShow('准备刷新…', true);
  fetch('/api/refresh', { method: 'POST' }).then(r => r.json()).then(x => {
    if (!x.ok) conLine('已经有一个刷新在跑了，等它结束。', '#fcd34d');
    pollRefresh();
    pollTimer = setInterval(pollRefresh, 800);
  });
};

function renderDoctor(d) {
  conClear();
  d.checks.forEach(c => {
    const mark = c.ok ? '✓ ' : (c.level === 'fail' ? '✕ ' : '! ');
    conLine(mark + c.name + '　' + c.detail,
            c.ok ? '#86efac' : (c.level === 'fail' ? '#fca5a5' : '#fcd34d'));
    if (!c.ok && c.fix === 'napcat') addNapcatFix();
  });
  if (d.checks.some(c => !c.ok && c.fix === 'deps')) {
    const sp = conLine('→ 点这里安装 / 修复依赖', '#7dd3fc');
    sp.style.cursor = 'pointer';
    sp.style.textDecoration = 'underline';
    sp.onclick = () => {
      sp.textContent = '正在安装，请稍候…';
      fetch('/api/install', { method: 'POST' }).then(r => r.json()).then(x => {
        sp.textContent = x.ok ? '安装完成，再点「环境体检」复查一次' : '安装失败，输出见下';
        if (x.output) conLine(x.output, '#94a3b8');
      });
    };
  }
  conShow(d.ok ? '体检通过' : ('有 ' + d.fails + ' 项待处理'), false);
}

// NapCat 一键下载：POST 起任务，GET 轮询进度，装完自动复查体检
let napTimer = null;

function addNapcatFix() {
  const sp = conLine('   ⬇ 下载 NapCat（约 34 MB，解压到项目的 napcat 目录）', '#7dd3fc');
  sp.style.cursor = 'pointer';
  sp.style.textDecoration = 'underline';
  sp.onclick = () => {
    if (napTimer) return;
    sp.textContent = '   正在连接 GitHub…';
    sp.style.cursor = 'default';
    sp.style.textDecoration = 'none';
    fetch('/api/napcat', { method: 'POST' }).then(r => r.json()).then(x => {
      if (!x.ok) conLine('   已经有一个下载在跑了，等它结束。', '#fcd34d');
      pollNapcat();
      napTimer = setInterval(pollNapcat, 800);
    }).catch(() => conLine('   接口调不通（是不是直接双击打开了 html？）', '#fca5a5'));
  };
}

function pollNapcat() {
  fetch('/api/napcat').then(r => r.json()).then(s => {
    conClear();
    s.lines.forEach(l => {
      let c = '#cbd5e1';
      if (l.indexOf('失败') >= 0) c = '#fca5a5';
      else if (l.indexOf('完成') >= 0 || l.indexOf('就绪') >= 0) c = '#86efac';
      else if (l.indexOf('版本') >= 0) c = '#93c5fd';
      conLine('   ' + l, c);
    });
    if (s.running) { conShow('正在下载 NapCat…', true); return; }
    if (napTimer) { clearInterval(napTimer); napTimer = null; }
    if (s.ok) {
      conShow('NapCat 装好了，复查中…', false);
      setTimeout(() => {
        fetch('/api/doctor').then(r => r.json()).then(renderDoctor).catch(() => {});
      }, 500);
    } else {
      conShow('NapCat 下载失败', false);
      conLine('   也可以手动下：github.com/NapNeko/NapCatQQ/releases 里的 NapCat.Shell.zip，', '#7dd3fc');
      conLine('   解压出来的文件直接放进上面提示的那个 napcat 目录。', '#7dd3fc');
    }
  }).catch(() => { if (napTimer) { clearInterval(napTimer); napTimer = null; } });
}

btnDoctor.onclick = () => {
  conShow('体检中…', true);
  fetch('/api/doctor').then(r => r.json()).then(renderDoctor)
    .catch(() => conShow('体检接口不可用（是不是直接双击打开了 html？）', false));
};

if (DEMO) {
  btnDemo.textContent = '退出示例';
  btnDemo.onclick = () => { location.href = '/'; };
} else {
  btnDemo.onclick = () => { location.href = '/?demo=1'; };
}

if (!SERVED) {
  acts.style.display = 'none';
} else {
  const foot = document.getElementById('foot');
  const btnQuit = document.getElementById('btnQuit');
  foot.style.display = 'flex';
  btnQuit.onclick = () => {
    if (!confirm('停掉之后这个页面就打不开了，下次双击「启动看板.cmd」重新开。确定要停？')) return;
    btnQuit.textContent = '正在停止…';
    fetch('/api/quit', { method: 'POST' }).then(() => {
      document.body.innerHTML = '<div class=bye>看板服务已停止' +
        '<span>这个页面可以关掉，下次双击「启动看板.cmd」再开。</span></div>';
    }).catch(() => {
      btnQuit.textContent = '停止看板服务';
      conShow('停不掉 —— 服务可能已经不在跑了', false);
    });
  };
  // 第一次打开先自查一遍，有问题直接把面板弹出来
  fetch('/api/doctor').then(r => r.json()).then(d => {
    if (!d.ok) renderDoctor(d);
  }).catch(() => {});
}

/* ---------- 数据源面板（QQ / 微信 开关 + 位置 + 多账号） ---------- */
const srcModal = document.getElementById('srcModal');
const srcHint = document.getElementById('srcHint');
const srcMsg = document.getElementById('srcMsg');
const qqOn = document.getElementById('qqOn');
const wxOn = document.getElementById('wxOn');
const qqSt = document.getElementById('qqSt');
const wxSt = document.getElementById('wxSt');
const qqExe = document.getElementById('qqExe');
const qqUin = document.getElementById('qqUin');
const wxDir = document.getElementById('wxDir');
const wxPathShow = document.getElementById('wxPathShow');
let SRC = null;

function srcOpen(on) {
  srcModal.classList.toggle('show', !!on);
  if (on) { srcMsg.className = 'srcmsg'; srcLoad(); }
}

function srcMsgShow(text, kind) {
  srcMsg.textContent = text;
  srcMsg.className = 'srcmsg show ' + (kind || 'ok');
}

function srcSt(blk, sw, lab) {
  document.getElementById(blk).classList.toggle('off', !sw.checked);
  lab.textContent = sw.checked ? '已开启' : '已关闭';
}

function opt(v, t, extra) {
  const o = document.createElement('option');
  o.value = v; o.textContent = t;
  if (extra) o.dataset.extra = extra;
  return o;
}

function srcRender(d) {
  SRC = d;

  // 提示（多账号 / 找不到）
  if (d.hints && d.hints.length) {
    srcHint.style.display = 'block';
    srcHint.innerHTML = '<b>需要你确认一下</b>' +
      d.hints.map(h => '<div class="h">' + esc(h) + '</div>').join('');
  } else {
    srcHint.style.display = 'none';
  }

  // QQ 开关
  qqOn.checked = !!d.qq.enabled;
  srcSt('blkQQ', qqOn, qqSt);

  // QQ 程序位置
  qqExe.textContent = '';
  qqExe.appendChild(opt('', '自动找（推荐）'));
  (d.qq.exe_list || []).forEach(p => qqExe.appendChild(opt(p, p)));
  const curExe = d.qq.exe || (d.qq.exe_list || [])[0] || '';
  if (curExe && !(d.qq.exe_list || []).includes(curExe)) qqExe.appendChild(opt(curExe, curExe));
  qqExe.value = curExe;
  document.getElementById('qqExePick').style.display = '';

  // QQ 号（多个就问）
  qqUin.textContent = '';
  const uins = d.qq.uin_list || [];
  if (uins.length > 1) qqUin.appendChild(opt('', '— 请选择用哪个号 —'));
  if (!uins.length) qqUin.appendChild(opt('', '这台机器还没登录过 QQ'));
  uins.forEach(u => qqUin.appendChild(opt(u, u)));
  qqUin.value = d.qq.uin || (uins.length === 1 ? uins[0] : '');

  // 微信开关
  wxOn.checked = !!d.wechat.enabled;
  srcSt('blkWX', wxOn, wxSt);

  // 微信账号
  wxDir.textContent = '';
  const cands = d.wechat.candidates || [];
  if (cands.length > 1) wxDir.appendChild(opt('', '— 请选择用哪个号 —'));
  if (!cands.length) wxDir.appendChild(opt('', '没找到微信数据目录'));
  cands.forEach(c => wxDir.appendChild(opt(
    c.path,
    c.account + ' · ' + c.db_count + ' 个库 · ' + c.mtime_txt + (c.official ? '' : '（旧位置）')
  )));
  // 多个账号就留空，逼用户自己选 —— 选错不是抓不到，是抓出来一片空
  wxDir.value = d.wechat.db_dir || (cands.length === 1 ? cands[0].path : '');
  wxPathShow.value = wxDir.value || '';

  srcMsg.className = 'srcmsg';
}

function srcLoad() {
  if (!SERVED) {
    srcMsgShow('这个面板要用服务模式打开才生效 —— 双击「启动看板.cmd」再进来看。', 'err');
    return;
  }
  fetch('/api/sources').then(r => r.json()).then(srcRender)
    .catch(() => srcMsgShow('读不到设置，服务可能刚重启，刷新一下页面。', 'err'));
}

// 手动填路径：浏览器给不了真实路径，只能让人粘
function srcPickPath(kind) {
  const cur = kind === 'exe' ? (qqExe.value || '') : (wxDir.value || wxPathShow.value || '');
  const tip = kind === 'exe'
    ? '把 QQ.exe 的完整路径粘进来。\\n\\n找法：资源管理器里找到 QQ.exe → 按住 Shift 右键 → 「复制文件地址」。'
    : '把微信数据文件夹的完整路径粘进来（里面要有 message 目录）。\\n\\n找法：资源管理器里打开该文件夹 → 点一下地址栏 → Ctrl+C。';
  const v = prompt(tip, cur);
  if (v === null) return;
  const path = v.trim().replace(/^"|"$/g, '');
  if (!path) return;
  if (kind === 'exe') {
    if (!/qq[.]exe$/i.test(path)) {
      srcMsgShow('要的是 QQ.exe 本身，不是文件夹。', 'err');
      return;
    }
    if (![...qqExe.options].some(o => o.value === path)) qqExe.appendChild(opt(path, path));
    qqExe.value = path;
    srcMsgShow('已经填成：' + path + '　记得点保存。', 'ok');
  } else {
    if (![...wxDir.options].some(o => o.value === path)) wxDir.appendChild(opt(path, path));
    wxDir.value = path;
    wxPathShow.value = path;
    srcMsgShow('已经填成：' + path + '　记得点保存。', 'ok');
  }
}

function srcSave() {
  if (!SRC) { srcMsgShow('还没读到当前设置，等一下再点保存。', 'err'); return; }
  const body = {
    qq: { enabled: qqOn.checked, uin: qqUin.value || '', exe: qqExe.value || '' },
    wechat: { enabled: wxOn.checked, db_dir: wxDir.value || '' }
  };
  if (body.qq.enabled && !body.qq.uin && (SRC.qq.uin_list || []).length > 1) {
    srcMsgShow('QQ 有 ' + SRC.qq.uin_list.length + ' 个号，得先选一个。', 'err');
    return;
  }
  if (body.wechat.enabled && !body.wechat.db_dir && (SRC.wechat.candidates || []).length > 1) {
    srcMsgShow('微信有 ' + SRC.wechat.candidates.length + ' 个账号，得先选一个。', 'err');
    return;
  }
  const btn = document.getElementById('srcSave');
  btn.disabled = true; btn.textContent = '保存中…';
  fetch('/api/sources', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(r => r.json()).then(x => {
    btn.disabled = false; btn.textContent = '保存';
    if (!x.ok) { srcMsgShow(x.error || '没保存成功', 'err'); return; }
    srcRender(x.info);
    srcMsgShow('已保存。点右上角「↻ 刷新数据」按新设置读一次，看板上就会跟着变。', 'ok');
  }).catch(() => {
    btn.disabled = false; btn.textContent = '保存';
    srcMsgShow('保存失败，服务可能已经停了。', 'err');
  });
}

if (document.getElementById('btnSrc')) {
  document.getElementById('btnSrc').onclick = () => srcOpen(true);
  document.getElementById('srcClose').onclick = () => srcOpen(false);
  document.getElementById('srcCancel').onclick = () => srcOpen(false);
  srcModal.onclick = e => { if (e.target === srcModal) srcOpen(false); };
  document.getElementById('srcSave').onclick = srcSave;
  document.getElementById('qqExePick').onclick = () => srcPickPath('exe');
  document.getElementById('wxDirPick').onclick = () => srcPickPath('dir');
  qqOn.onchange = () => srcSt('blkQQ', qqOn, qqSt);
  wxOn.onchange = () => srcSt('blkWX', wxOn, wxSt);
  wxDir.onchange = () => { wxPathShow.value = wxDir.value || ''; };

  // 列头「读取已关闭」小标签，点一下直接开面板
  const offQQ = document.getElementById('offQQ');
  const offWX = document.getElementById('offWX');
  if (SRCOFF.qq) { offQQ.style.display = ''; offQQ.onclick = () => srcOpen(true); }
  if (SRCOFF.wechat) { offWX.style.display = ''; offWX.onclick = () => srcOpen(true); }
  // 第一次进来如果有多账号没定，主动弹一次（一天最多烦一次）
  if (SRCOFF.ask) {
    let nag = false;
    try {
      const last = parseInt(localStorage.getItem('qad_src_ask') || '0', 10);
      nag = !last || (Date.now() - last) > 86400000;
      if (nag) localStorage.setItem('qad_src_ask', String(Date.now()));
    } catch (e) { nag = false; }
    if (nag) setTimeout(() => srcOpen(true), 900);
  }
}

/* ---------- 更新提示（每天一次心跳，结果由服务端缓存） ---------- */
const updbar = document.getElementById('updbar');
const updModal = document.getElementById('updModal');
const updList = document.getElementById('updList');
let UPD = null, updTimer = null;

function updModalShow(on) { updModal.classList.toggle('show', !!on); }

function updRender() {
  if (!UPD || !UPD.notes) return;
  updList.innerHTML = UPD.notes.map(n =>
    '<div class="row"><div class="cd">' + esc(n.date || '') + '</div>' +
    '<div class="cm">' + esc(n.msg || '') + '</div></div>').join('')
    || '<div class="row"><div class="cm">这次没有提交说明，直接更新就行。</div></div>';
  document.getElementById('updGit').href = UPD.url || '#';
}

function updLoad(force) {
  if (!SERVED || DEMO) return;
  fetch('/api/update' + (force ? '?force=1' : ''))
    .then(r => r.json()).then(d => {
      UPD = d;
      if (d.has_update) {
        document.getElementById('updSub').textContent =
          '远端 ' + d.latest + (d.latest_date ? ' · ' + d.latest_date : '') +
          '，你这里是 ' + (d.version || '') + '（' + d.current + '）';
        updbar.classList.add('show');
        updRender();
      } else {
        updbar.classList.remove('show');
      }
    }).catch(() => {});
}

function updApply() {
  updModalShow(false);
  if (!confirm('现在更新到最新版？\\n\\n只覆盖程序文件，config.json、data/、napcat/ 都不会动。\\n更完会自动重启看板。')) return;
  conClear();
  conShow('正在更新…', true);
  fetch('/api/update/apply', { method: 'POST' }).then(r => r.json()).then(x => {
    if (!x.ok) conLine('已经有一个更新在跑了，等它结束。', '#fcd34d');
    updPoll();
    updTimer = setInterval(updPoll, 700);
  }).catch(() => conShow('更新接口不可用（是不是直接双击打开了 html？）', false));
}

function updPoll() {
  fetch('/api/update/apply').then(r => r.json()).then(s => {
    conClear();
    (s.lines || []).forEach(l => {
      let c = '#cbd5e1';
      if (l.indexOf('失败') >= 0 || l.indexOf('没成功') >= 0) c = '#fca5a5';
      else if (l.indexOf('已更新') >= 0 || l.indexOf('完成') >= 0) c = '#86efac';
      else if (l.indexOf('下载') >= 0 || l.indexOf('覆盖') >= 0) c = '#93c5fd';
      conLine(l, c);
    });
    if (s.running) { conShow('正在更新…', true); return; }
    if (updTimer) { clearInterval(updTimer); updTimer = null; }
    if (s.result && s.result.ok) {
      conShow('更新完成，正在重启看板…', false);
      restartBoard();
    } else {
      conShow('更新失败', false);
      if (s.result && s.result.error) conLine('   ' + s.result.error, '#fca5a5');
      conLine('   也可以手动处理：' + (UPD ? UPD.url : ''), '#7dd3fc');
    }
  }).catch(() => { if (updTimer) { clearInterval(updTimer); updTimer = null; } });
}

function restartBoard() {
  conShow('正在重启看板…', true);
  fetch('/api/restart', { method: 'POST' }).catch(() => {});
  let n = 0;
  const t = setInterval(() => {
    n++;
    if (n > 45) {
      clearInterval(t);
      conShow('重启超时了，请双击「启动看板.cmd」', false);
      return;
    }
    fetch('/api/health').then(r => r.json()).then(() => {
      clearInterval(t);
      location.reload();
    }).catch(() => {});
  }, 700);
}

if (updbar) {
  document.getElementById('updWhat').onclick = () => { updModalShow(true); updRender(); };
  document.getElementById('updClose').onclick = () => updModalShow(false);
  updModal.onclick = e => { if (e.target === updModal) updModalShow(false); };
  document.getElementById('updGo').onclick = updApply;
  document.getElementById('updGo2').onclick = updApply;
  document.getElementById('updSkip').onclick = () => {
    updbar.classList.remove('show');
    fetch('/api/update/ignore', { method: 'POST' }).catch(() => {});
  };
  setTimeout(() => updLoad(false), 1200);
}
</script>
</body></html>"""


# --------------------------------------------------------------------------- 组装
def build(today: date, days: int = 30, sources=None,
          served: bool = False, demo: bool = False) -> str:
    rows = collect(today, days, sources)
    dropped = getattr(collect, "dropped", (0, 0, 0))
    n_old, n_exp = dropped[0], dropped[1]
    n_bare = dropped[2] if len(dropped) > 2 else 0
    n_done = dropped[3] if len(dropped) > 3 else 0
    hide_parts = []
    if n_done:
        hide_parts.append(f"{n_done} 条已标记完成")
    if n_old:
        hide_parts.append(f"{n_old} 条超过 {days} 天")
    if n_exp:
        hide_parts.append(f"{n_exp} 条已过期")
    if n_bare:
        hide_parts.append(f"{n_bare} 条只喊了 @所有人、上下文也找不到正文")
    dropped_txt = ("，".join(hide_parts) + "，已隐藏") if hide_parts else ""
    groups: dict[str, dict] = {}
    for r in rows:
        c = groups.setdefault(r["g"], {"gid": r["gid"], "n": 0, "last": ""})
        c["n"] += 1
        c["last"] = max(c["last"], r["t"])

    # 空壳群也算「有喊话的群」，不然统计数字会跟着掉，看着像抓漏了
    shadow = getattr(collect, "shadow", [])
    for s in shadow:
        cur = groups.get(s["g"])
        if cur is None:
            groups[s["g"]] = {"gid": s["gid"], "n": s["n"], "last": "", "ghost": 1}
        else:
            cur["n"] += s["n"]          # 同名群（QQ 和微信各有一个）就并起来数

    soon = sum(1 for r in rows if r["n"] is not None and 0 <= r["n"] <= 14)

    # 数据源状态：哪一侧被关掉了、哪一侧有多个账号还没定
    src_off = {"qq": False, "wechat": False, "ask": False}
    try:
        qq_off = not bool(settings.get("qq", "enabled", True))
        wx_off = not bool(settings.get("wechat", "enabled", True))
        src_off["qq"], src_off["wechat"] = qq_off, wx_off
        if not wx_off and not str(settings.get("wechat", "db_dir") or "").strip() \
                and len(settings.db_candidates()) > 1:
            src_off["ask"] = True
        if not qq_off and not str(settings.get("qq", "uin") or "").strip() \
                and len(settings.qq_uins()) > 1:
            src_off["ask"] = True
    except Exception:
        pass
    if demo:
        src_off["ask"] = False        # 示例模式别弹面板，那是假数据

    try:
        import board_state
        st = board_state.load()
    except Exception:
        st = {"done": [], "muted": []}

    subs = {
        "%%BRAND%%": BRAND,
        "%%LOGO%%": asset_uri("logo-112.png"),
        "%%FAVICON%%": asset_uri("favicon.png"),
        "%%BOARDFILE%%": settings.board_file().name,
        "%%TODAY%%": f"{today:%Y-%m-%d}",
        "%%NOW%%": f"{datetime.now():%Y-%m-%d %H:%M}",
        "%%DAYS%%": str(days) if days else "不限",
        "%%DROPPED%%": dropped_txt,
        "%%TOTAL%%": str(len(rows)),
        "%%GN%%": str(len(groups)),
        "%%SOON%%": str(soon),
        "%%GROUPSJSON%%": json.dumps(groups, ensure_ascii=False, separators=(",", ":")),
        "%%DATA%%": json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        "%%DONE%%": json.dumps(st.get("done") or [], ensure_ascii=False, separators=(",", ":")),
        "%%MUTED%%": json.dumps(st.get("muted") or [], ensure_ascii=False, separators=(",", ":")),
        "%%SHADOW%%": json.dumps(shadow, ensure_ascii=False, separators=(",", ":")),
        "%%SRCOFF%%": json.dumps(src_off, ensure_ascii=False, separators=(",", ":")),
        "%%SERVED%%": "true" if served else "false",
        "%%DEMO%%": "true" if demo else "false",
        "%%VERSION%%": VERSION,
        "%%DEMOBAR%%": (
            '<div class="demobar"><b>示例模式</b>　现在显示的是 demo/ 里的假数据，'
            '不会动你的真实群。<a href="/">回到我的看板 →</a></div>'
        ) if demo else "",
    }
    out = TPL
    for k, v in subs.items():
        out = out.replace(k, v)
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="", help="基准日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--days", type=int, default=30, help="只看最近 N 天（0=不限）")
    ap.add_argument("--src", default="", help="QQ 侧输入 json，默认取最新的 群公告_*.json")
    ap.add_argument("--src-wx", default="", help="微信侧输入 json，默认取最新的 微信群公告_*.json")
    ap.add_argument("--no-wx", action="store_true", help="只渲染 QQ，不合并微信")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    sources = []
    if a.src:
        SRC = Path(a.src)
    else:
        SRC = pick_src()
    sources.append((SRC, "QQ"))

    wx = None if a.no_wx else (Path(a.src_wx) if a.src_wx else pick_wx_src())
    if wx and Path(wx).exists():
        sources.append((Path(wx), "微信"))

    today = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else date.today()
    out = Path(a.out) if a.out else OUT
    out.write_text(build(today, a.days, sources=sources), encoding="utf-8")
    names = " + ".join(f"{p.name}({lb})" for p, lb in sources)
    print(f"OK -> {out.name}  {out.stat().st_size / 1024:.0f} KB  (源 {names})")
