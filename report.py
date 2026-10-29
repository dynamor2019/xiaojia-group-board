# -*- coding: utf-8 -*-
"""把群 @所有人 数据渲染成一个自包含的 HTML 看板。

布局：**左右两列** —— 左 QQ、右微信，各列独立，绝不混排。
功能：搜索 / 按群筛 / 只看带截止时间 / 标记已完成 / 隐藏已完成。

「已完成」状态的持久化
----------------------
同时写 localStorage 和 cookie（cookie 用 path=/，跨端口有效），
因为预览面板每次的 127.0.0.1:<随机端口> 会换源，单靠 localStorage 会丢。
两处取并集读、两处都写，任一处可用就不会丢。

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

import settings

BASE = Path(__file__).resolve().parent
DATA = settings.data_dir()
SRC = DATA / "群公告_最新.json"      # 兜底名，正常会由 pick_src() 找到最新那份
OUT = settings.board_file()


ASSETS = BASE / "assets"
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
        sources = [(SRC, "QQ")]
        wx = pick_wx_src()
        if wx and wx.exists():
            sources.append((wx, "微信"))

    rows: list[dict] = []
    dropped_old = dropped_expired = 0
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
                text = (rec.get("text") or "").replace("「@所有人」", "").strip()
                text = re.sub(r"\s+", " ", text)
                if len(text) < 4:                              # 只有「@所有人」没正文
                    continue
                dls = [d for d in extract_deadlines(text, dt.date()) if d >= dt.date()]
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
                        "x": text,
                        "d": dl,
                        "n": (date.fromisoformat(dl) - today).days if dl else None,
                        "k": topic_of(text),
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

    collect.dropped = (dropped_old, dropped_expired)
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
.saver.bad{color:#92400e;background:#fffbeb;border-color:#fcd34d}

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
.msg{padding:9px 13px;border-top:1px solid #f1f2f5;transition:.15s}
.msg:hover{background:#fafbfc}
.msg-h{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;
       font-size:11px;color:var(--tx2);margin-bottom:3px}
.msg-h b{color:#374151;font-weight:600;font-size:11.5px}
.msg-h .btn-done{margin-left:auto}
.msg-x{font-size:12.5px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
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
@media(max-width:900px){
  .cols{grid-template-columns:1fr}
  main,header{padding-left:14px;padding-right:14px}
  input[type=search]{width:100%}
  .gmeta{display:none}
}
</style></head><body>
<header>
  <div class="brand">
    <img src="%%LOGO%%" alt="小贾">
    <div class="tt">
      <h1>%%BRAND%%<span class="tag">QQ · 微信</span></h1>
      <div class="sub">抓取时间 %%NOW%% ｜ 只显示最近 <b>%%DAYS%%</b> 天的、未过期的 ｜ %%DROPPED%%</div>
    </div>
  </div>
  <div class="stats">
    <div class="stat"><b>%%TOTAL%%</b><span>@所有人 消息</span></div>
    <div class="stat"><b>%%GN%%</b><span>有喊话的群</span></div>
    <div class="stat"><b style="color:var(--hot)">%%SOON%%</b><span>14 天内到期</span></div>
    <div class="stat"><b id="doneN" style="color:var(--ok)">0</b><span>已完成</span></div>
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
        <span class="n" id="nQQ"></span></div>
      <div class="cards" id="soonQQ"></div>
      <div id="listQQ"></div>
    </section>
    <section class="col" id="colWX">
      <div class="col-h"><span class="src bwx">微信</span><span class="ttl">微信群</span>
        <span class="n" id="nWX"></span></div>
      <div class="cards" id="soonWX"></div>
      <div id="listWX"></div>
    </section>
  </div>
</main>
<script>
const DATA = %%DATA%%;
const GROUPS = %%GROUPSJSON%%;
const SERVED = %%SERVED%%;
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

if (lsOK) {
  try { (JSON.parse(localStorage.getItem(KEY) || '[]') || []).forEach(x => done.add(x)); } catch (e) {}
}
readCookie().forEach(x => done.add(x));
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
  o.value = g; o.textContent = g + ' (' + GROUPS[g].n + ')';
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
    '<div class="card-t">' + esc(r.x.slice(0, 260)) + '</div>' +
    '<div class="card-f"><span class="who">' + esc(r.g) + ' ｜ ' + esc(r.s) + '</span>' +
      btnHTML(r.i) + '</div></div>';
}

function groupHTML(g, rs, qs) {
  const last = rs.reduce((m, r) => r.t > m ? r.t : m, '');
  const src = rs[0].src || 'QQ', gid = rs[0].gid || '';
  const open = (qs.length || onlyTodo.checked || gsel.value || hideDone.checked) ? ' open' : '';
  const body = rs.map(r =>
    '<div class="msg' + (done.has(r.i) ? ' done' : '') + '">' +
      '<div class="msg-h"><b>' + esc(r.s || '未知') + '</b><span>' + r.t + '</span>' +
        '<span class="tag">' + r.k + '</span>' +
        (r.d ? '<span class="dtag">截止 ' + r.d + '</span>' : '') +
        btnHTML(r.i) + '</div>' +
      '<div class="msg-x">' + hl(r.x, qs) + '</div></div>').join('');
  return '<details class="grp"' + open + '><summary>' +
    '<span class="gname">' + esc(g) + '</span>' +
    '<span class="gmeta">' + rs.length + ' 条 · 最新 ' + last.slice(0, 10) + '</span>' +
    '<button class="btn-mute" title="取消关注：以后不再扫描这个群" ' +
      'data-src="' + esc(src) + '" data-gid="' + esc(gid) + '" data-name="' + esc(g) + '">✕</button>' +
    '</summary>' + body + '</details>';
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
        const hay = (r.x + ' ' + r.s + ' ' + r.g + ' ' + r.k).toLowerCase();
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

  document.getElementById('soon' + sfx).innerHTML =
    soon.length ? soon.map(cardHTML).join('') : '';
  document.getElementById('list' + sfx).innerHTML = order.length
    ? order.map(g => groupHTML(g, buckets.get(g), qs)).join('')
    : '<div class="col-none">没有匹配的记录</div>';

  const nEl = document.getElementById('n' + sfx);
  nEl.textContent = rows.length + ' 条 · ' + order.length + ' 群' +
                    (soon.length ? ' · ' + soon.length + ' 条临近截止' : '');
  return rows.length;
}

function render() {
  const qs = q.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);
  const gv = gsel.value, ot = onlyTodo.checked, hd = hideDone.checked;
  renderCol('QQ', qs, gv, ot, hd);
  renderCol('微信', qs, gv, ot, hd);
  let hit = 0;
  for (const r of DATA) if (done.has(r.i)) hit++;
  doneN.textContent = hit;
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
  if (!confirm('清除全部 ' + done.size + ' 条「已完成」标记？')) return;
  done.clear(); save(); render();
});

q.addEventListener('input', render);
gsel.addEventListener('change', render);
onlyTodo.addEventListener('change', render);
hideDone.addEventListener('change', render);
renderMuted();
render();
</script>
</body></html>"""


# --------------------------------------------------------------------------- 组装
def build(today: date, days: int = 30, sources=None, served: bool = False) -> str:
    rows = collect(today, days, sources)
    n_old, n_exp = getattr(collect, "dropped", (0, 0))
    groups: dict[str, dict] = {}
    for r in rows:
        c = groups.setdefault(r["g"], {"gid": r["gid"], "n": 0, "last": ""})
        c["n"] += 1
        c["last"] = max(c["last"], r["t"])

    soon = sum(1 for r in rows if r["n"] is not None and 0 <= r["n"] <= 14)

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
        "%%DROPPED%%": f"{n_old} 条超过 {days} 天、{n_exp} 条已过期，已隐藏",
        "%%TOTAL%%": str(len(rows)),
        "%%GN%%": str(len(groups)),
        "%%SOON%%": str(soon),
        "%%GROUPSJSON%%": json.dumps(groups, ensure_ascii=False, separators=(",", ":")),
        "%%DATA%%": json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        "%%DONE%%": json.dumps(st.get("done") or [], ensure_ascii=False, separators=(",", ":")),
        "%%MUTED%%": json.dumps(st.get("muted") or [], ensure_ascii=False, separators=(",", ":")),
        "%%SERVED%%": "true" if served else "false",
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
