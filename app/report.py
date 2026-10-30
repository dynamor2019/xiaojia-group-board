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
<script>
/* 首屏定主题：必须排在样式表之前，否则深色用户会先看到一帧白底再翻黑 */
(function () {
  var t = '';
  try { t = (JSON.parse(localStorage.getItem('qad_prefs_v1') || '{}') || {}).theme || ''; } catch (e) {}
  if (t !== 'light' && t !== 'dark') {
    try {
      var m = document.cookie.match(/(?:^|;\\s*)qadp=([^;]*)/);
      if (m) t = (JSON.parse(decodeURIComponent(m[1])) || {}).theme || '';
    } catch (e) {}
  }
  if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
})();
</script>
<style>
*{box-sizing:border-box}
:root{
  --bg:#060a12;--bg2:#0b1220;--bg3:#101a2c;--bg4:#16233a;
  --line:rgba(126,166,255,.14);--line2:rgba(126,166,255,.28);
  --tx:#e9eefb;--tx2:#94a4c0;--tx3:#5f6d87;
  --ac:#4c9aff;--brand:#16e3a5;--brand2:#0aa8ff;
  --hot:#ff5f6d;--warn:#ffb020;--ok:#2fe3a4;
  --qq:#4c9aff;--wx:#22d39a;
  --shadow:0 18px 48px rgba(0,0,0,.45);
  --glass:rgba(11,18,32,.72);
  --on-brand:#04120d;          /* 渐变主按钮上的字：深色主题下用深字才够亮 */
  --hover:rgba(255,255,255,.07);
}
:root[data-theme=light]{
  --bg:#f3f6fb;--bg2:#ffffff;--bg3:#f7f9fd;--bg4:#eef3fb;
  --line:#e2e8f2;--line2:#cbd5e6;
  --tx:#101828;--tx2:#5a677d;--tx3:#8b96a9;
  --ac:#2563eb;--brand:#0f9d6e;--brand2:#0b7fe0;
  --hot:#dc2626;--warn:#ea580c;--ok:#16a34a;
  --qq:#2563eb;--wx:#0f9d6e;
  --shadow:0 14px 40px rgba(16,24,40,.12);
  --glass:rgba(255,255,255,.78);
  --on-brand:#ffffff;          /* 浅色主题的渐变更饱和，深字会糊，改用白字 */
  --hover:rgba(16,24,40,.05);
}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--tx);min-height:100vh;
  font:14px/1.68 "Inter",-apple-system,"Segoe UI","HarmonyOS Sans SC","Microsoft YaHei UI",
       "Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
  font-variant-numeric:tabular-nums}
/* 背景：一层极淡的网格 + 两团色晕，深色下才开，浅色不抢戏 */
body::before{content:'';position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:
    radial-gradient(900px 520px at 12% -8%,rgba(22,227,165,.16),transparent 62%),
    radial-gradient(820px 480px at 92% 4%,rgba(10,168,255,.14),transparent 60%),
    linear-gradient(180deg,rgba(255,255,255,.03),transparent 30%);}
:root:not([data-theme=light]) body::before{
  background:
    radial-gradient(900px 520px at 12% -8%,rgba(22,227,165,.18),transparent 62%),
    radial-gradient(820px 480px at 92% 4%,rgba(10,168,255,.16),transparent 60%),
    linear-gradient(rgba(126,166,255,.045) 1px,transparent 1px) 0 0/56px 56px,
    linear-gradient(90deg,rgba(126,166,255,.045) 1px,transparent 1px) 0 0/56px 56px,
    linear-gradient(180deg,rgba(255,255,255,.03),transparent 30%);}
::selection{background:rgba(22,227,165,.3)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--line2);border-radius:8px;border:2px solid transparent;
  background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:var(--tx3);background-clip:padding-box}
::-webkit-scrollbar-track{background:transparent}

/* ================= 顶栏 =================
   注意：这里**不做 position:sticky**。顶栏含品牌区 + 数字卡 + 筛选条，
   撑到 190px 左右，一吸顶就吃掉五分之一屏幕，往下翻消息时反而更难用。 */
header{padding:15px 30px 13px;background:var(--glass);border-bottom:1px solid var(--line);
  position:relative;z-index:20}
header::after{content:'';position:absolute;left:0;right:0;bottom:-1px;height:1px;
  background:linear-gradient(90deg,transparent,rgba(22,227,165,.55),rgba(10,168,255,.5),transparent);
  opacity:.55}
.brand{display:flex;align-items:center;gap:13px;margin-bottom:4px}
.brand img{width:44px;height:44px;border-radius:13px;object-fit:cover;flex:none;
  border:1px solid var(--line2);box-shadow:0 0 0 3px rgba(22,227,165,.08),0 8px 24px rgba(0,0,0,.35)}
.brand .tt{min-width:0}
h1{margin:0;font-size:20px;font-weight:680;letter-spacing:.3px;line-height:1.28;
  background:linear-gradient(92deg,var(--tx) 20%,var(--brand) 120%);
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
h1 .tag{display:inline-block;margin-left:8px;padding:1.5px 8px;border-radius:999px;
  font-size:10px;font-weight:650;letter-spacing:.6px;vertical-align:2px;
  color:var(--brand);background:rgba(22,227,165,.1);border:1px solid rgba(22,227,165,.28);
  -webkit-text-fill-color:currentColor}
.sub{color:var(--tx2);font-size:12.5px;letter-spacing:.1px}

.stats{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
.stat{position:relative;padding:8px 14px 8px 13px;border-radius:11px;min-width:96px;
  background:var(--bg3);border:1px solid var(--line);cursor:default;
  transition:border-color .18s,transform .18s,background .18s}
.stat::before{content:'';position:absolute;left:0;top:8px;bottom:8px;width:2px;border-radius:2px;
  background:linear-gradient(180deg,var(--brand),var(--brand2));opacity:.75}
.stat b{font-size:19px;display:block;line-height:1.15;font-weight:700;
  font-family:ui-monospace,"SF Mono",Consolas,monospace;letter-spacing:-.3px}
.stat span{color:var(--tx2);font-size:11px;letter-spacing:.2px}
.stat.click{cursor:pointer}
.stat.click:hover{border-color:var(--line2);background:var(--bg4);transform:translateY(-1px)}
.stat[title]:hover{border-color:var(--line2)}

.bar{display:flex;gap:9px;margin-top:12px;flex-wrap:wrap;align-items:center}
input[type=search],select{padding:7px 12px;border:1px solid var(--line);border-radius:9px;
  font-size:13px;background:var(--bg3);color:var(--tx);outline:none;font-family:inherit;
  transition:border-color .15s,box-shadow .15s,background .15s}
input[type=search]{width:300px}
input[type=search]::placeholder{color:var(--tx3)}
input[type=search]:focus,select:focus{border-color:var(--brand);
  box-shadow:0 0 0 3px rgba(22,227,165,.13);background:var(--bg2)}
select{cursor:pointer}
label.chk{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;color:var(--tx2);
  cursor:pointer;user-select:none;white-space:nowrap;padding:5px 10px;border-radius:8px;
  border:1px solid transparent;transition:.15s}
label.chk:hover{color:var(--tx);background:var(--bg3)}
label.chk input{accent-color:var(--brand);width:13px;height:13px;cursor:pointer}
label.chk:has(input:checked){color:var(--brand)}
.saver{font-size:11px;padding:2.5px 9px;border-radius:999px;border:1px solid var(--line);
  color:var(--tx2);background:var(--bg3);white-space:nowrap}
.saver.ok{color:var(--ok);background:rgba(47,227,164,.1);border-color:rgba(47,227,164,.3)}
.saver.bad{color:var(--warn);background:rgba(255,176,32,.1);border-color:rgba(255,176,32,.32)}

/* ---- 顶部动作按钮 ---- */
.acts{margin-left:auto;display:flex;gap:8px;align-items:center;flex:none}
.btn-act{display:inline-flex;align-items:center;gap:6px;padding:7px 13px;border-radius:10px;
  border:1px solid var(--line);background:var(--bg3);color:var(--tx);font-size:12.5px;
  font-family:inherit;cursor:pointer;white-space:nowrap;transition:.16s;letter-spacing:.2px}
.btn-act:hover{border-color:var(--line2);background:var(--bg4);transform:translateY(-1px)}
.btn-act:active{transform:translateY(0)}
.btn-act.primary{background:linear-gradient(135deg,var(--brand),var(--brand2));
  border-color:transparent;color:var(--on-brand);font-weight:700;
  box-shadow:0 6px 20px rgba(22,227,165,.28)}
.btn-act.primary:hover{filter:brightness(1.08);box-shadow:0 8px 26px rgba(22,227,165,.4)}
.btn-act:disabled{opacity:.6;cursor:default;transform:none}
.btn-act.ic{padding:7px 10px;font-size:15px;line-height:1;min-width:36px;justify-content:center}
.btn-act.ic span{display:block;transition:transform .35s cubic-bezier(.2,.9,.3,1.4)}
.btn-act.ic:hover span{transform:rotate(-18deg) scale(1.08)}
.btn-act .spin{width:12px;height:12px;border:2px solid rgba(0,0,0,.25);
  border-top-color:currentColor;border-radius:50%;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}

/* ---- 底部控制台 ---- */
.console{position:fixed;right:16px;bottom:16px;width:min(660px,calc(100vw - 32px));
  max-height:46vh;background:rgba(6,10,18,.94);color:#cfe0f5;border-radius:14px;z-index:60;
  border:1px solid var(--line2);box-shadow:var(--shadow);
  -webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);
  display:none;flex-direction:column;overflow:hidden}
.console.show{display:flex}
.console .ch{display:flex;align-items:center;gap:9px;padding:10px 14px;
  background:linear-gradient(180deg,rgba(255,255,255,.06),transparent);
  font-size:12.5px;font-weight:600;color:#eaf2ff;border-bottom:1px solid var(--line)}
.console .ch .dot{width:7px;height:7px;border-radius:50%;background:var(--ok);flex:none;
  box-shadow:0 0 8px var(--ok)}
.console .ch .dot.run{background:var(--warn);box-shadow:0 0 8px var(--warn);animation:bl 1s infinite}
@keyframes bl{50%{opacity:.3}}
.console .ch .x{margin-left:auto;cursor:pointer;color:#7d8ba3;font-size:15px;line-height:1}
.console .ch .x:hover{color:#fff}
.console .cbody{padding:11px 14px;overflow:auto;
  font:12px/1.62 ui-monospace,"SF Mono",Consolas,"Courier New",monospace;
  white-space:pre-wrap;word-break:break-all}
.console .cbody span{display:block}

/* ---- 演示横幅 ---- */
.demobar{background:linear-gradient(90deg,rgba(255,176,32,.16),rgba(255,176,32,.06));
  border-bottom:1px solid rgba(255,176,32,.28);color:var(--warn);
  font-size:12.5px;padding:8px 30px;display:flex;gap:10px;align-items:center}
.demobar b{font-weight:650}
.demobar a{margin-left:auto;color:var(--warn);font-weight:600;text-decoration:none}
.demobar a:hover{text-decoration:underline}

/* ---- 更新提示条 ---- */
.updbar{background:linear-gradient(90deg,rgba(22,227,165,.16),rgba(10,168,255,.12));
  border-bottom:1px solid rgba(22,227,165,.26);color:var(--tx);
  font-size:12.5px;padding:9px 30px;display:none;gap:11px;align-items:center}
.updbar.show{display:flex}
.updbar b{font-weight:650;white-space:nowrap;color:var(--brand)}
.updbar .ub-ic{display:inline-flex;width:18px;height:18px;border-radius:50%;flex:none;
  background:rgba(22,227,165,.18);color:var(--brand);border:1px solid rgba(22,227,165,.35);
  align-items:center;justify-content:center;font-size:11px}
.updbar .ub-sub{color:var(--tx2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.updbar .ub-btn{padding:4px 11px;border-radius:8px;border:1px solid var(--line2);
  background:transparent;color:var(--tx);font-size:12px;font-family:inherit;cursor:pointer;flex:none;
  transition:.15s}
.updbar .ub-btn:hover{background:var(--hover)}
.updbar .ub-btn.primary{background:linear-gradient(135deg,var(--brand),var(--brand2));
  color:var(--on-brand);font-weight:700;border-color:transparent}
.updbar .ub-x{margin-left:auto;cursor:pointer;opacity:.6;font-size:15px;line-height:1;flex:none}
.updbar .ub-x:hover{opacity:1}

/* ---- 弹层 ---- */
.modal{position:fixed;inset:0;background:rgba(3,6,12,.62);z-index:80;
  display:none;align-items:center;justify-content:center;padding:24px;
  -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
.modal.show{display:flex;animation:fade .18s ease}
@keyframes fade{from{opacity:0}to{opacity:1}}
.modal .mbox{background:var(--bg2);border:1px solid var(--line2);border-radius:16px;
  width:min(620px,100%);max-height:78vh;display:flex;flex-direction:column;overflow:hidden;
  box-shadow:var(--shadow);animation:pop .2s cubic-bezier(.2,.9,.3,1.2)}
@keyframes pop{from{transform:translateY(8px) scale(.985);opacity:0}to{transform:none;opacity:1}}
.modal .mh{display:flex;align-items:center;gap:8px;padding:14px 18px;font-size:14px;
  border-bottom:1px solid var(--line);font-weight:650}
.modal .mh span{margin-left:auto;cursor:pointer;color:var(--tx3);font-size:16px;line-height:1}
.modal .mh span:hover{color:var(--tx)}
.modal .mb{padding:12px 18px;overflow:auto;font-size:13px;line-height:1.75}
.modal .mb .row{padding:7px 0;border-bottom:1px dashed var(--line);display:flex;gap:10px}
.modal .mb .row:last-child{border-bottom:0}
.modal .mb .cd{color:var(--tx3);font-size:11.5px;flex:none;width:78px;padding-top:2px;
  font-family:ui-monospace,Consolas,monospace}
.modal .mb .cm{flex:1;min-width:0;word-break:break-word}
.updmeta{display:flex;align-items:baseline;gap:7px;flex-wrap:wrap;
  background:rgba(22,227,165,.07);border:1px solid rgba(22,227,165,.22);border-radius:10px;
  padding:9px 12px;margin-bottom:10px;font-size:12.5px;color:var(--brand)}
.updmeta b{font-size:13px;font-weight:650;color:var(--brand);
  font-family:ui-monospace,Consolas,monospace}
.updmeta span{color:var(--tx2);font-size:11.5px}
.updmeta i{font-style:normal;color:var(--tx3)}
.updmeta em{margin-left:auto;font-style:normal;font-size:11.5px;color:var(--brand);
  background:rgba(22,227,165,.14);border-radius:20px;padding:1px 9px}
.modal .mf{padding:12px 18px;border-top:1px solid var(--line);display:flex;
  gap:10px;align-items:center}
.modal .mf a{color:var(--ac);font-size:12.5px;text-decoration:none}
.modal .mf a:hover{text-decoration:underline}
.modal .mf .ub-btn{margin-left:auto;padding:6px 14px;border-radius:9px;font-size:13px;
  border:1px solid var(--line);background:var(--bg3);color:var(--tx);font-family:inherit;
  cursor:pointer;transition:.15s}
.modal .mf .ub-btn:hover{border-color:var(--line2);background:var(--bg4)}
.modal .mf .ub-btn.primary{background:linear-gradient(135deg,var(--brand),var(--brand2));
  border-color:transparent;color:var(--on-brand);font-weight:700}
.ver{color:var(--tx3);font-size:11.5px;font-family:ui-monospace,Consolas,monospace}

/* ---- 数据源面板 ---- */
.offtag{font-size:10px;padding:1.5px 8px;border-radius:20px;
  background:rgba(255,95,109,.12);color:var(--hot);border:1px solid rgba(255,95,109,.3);
  cursor:pointer;white-space:nowrap;transform:translateY(-1px);letter-spacing:.3px}
.offtag:hover{background:rgba(255,95,109,.2)}
#srcModal .mbox{width:min(680px,100%)}
.srcblk{border:1px solid var(--line);border-radius:13px;padding:13px 15px;margin-bottom:12px;
  background:var(--bg3)}
.srcblk.off{background:transparent}
/* 只淡化说明和表单，开关要保持清晰 —— 否则看不出"还能再打开" */
.srcblk.off .desc,.srcblk.off .fld{opacity:.45}
.srcblk .sh{display:flex;align-items:center;gap:10px;margin-bottom:2px}
.srcblk .sh b{font-size:13.5px;font-weight:650}
.srcblk .sh .st{font-size:11.5px;color:var(--tx3);margin-left:auto;margin-right:2px}
.srcblk.off .sh .st{color:var(--hot)}
.srcblk .desc{font-size:11.5px;color:var(--tx2);line-height:1.65;margin:3px 0 10px}
.fld{display:flex;gap:9px;align-items:center;margin:8px 0 0}
.fld>label{flex:none;width:64px;font-size:12px;color:var(--tx2)}
.fld select,.fld input[type=text]{flex:1;min-width:0;font-family:inherit;font-size:12.5px;
  padding:7px 10px;border:1px solid var(--line);border-radius:9px;background:var(--bg2);
  color:var(--tx);outline:none;text-overflow:ellipsis;transition:.15s}
.fld select:focus,.fld input[type=text]:focus{border-color:var(--brand);
  box-shadow:0 0 0 3px rgba(22,227,165,.12)}
.fld .mini{flex:none;font-size:11.5px;color:var(--tx3);white-space:nowrap}
.fld .pick{padding:6px 11px;border-radius:9px;border:1px solid var(--line);background:var(--bg2);
  color:var(--tx2);font-family:inherit;font-size:12px;cursor:pointer;flex:none;transition:.15s}
.fld .pick:hover{border-color:var(--brand);color:var(--brand)}
/* 开关 */
.sw{position:relative;display:inline-flex;width:40px;height:22px;flex:none;cursor:pointer}
.sw input{position:absolute;opacity:0;width:0;height:0}
.sw i{position:absolute;inset:0;background:var(--bg4);border:1px solid var(--line2);
  border-radius:22px;transition:background .18s,border-color .18s}
.sw i::after{content:'';position:absolute;left:2px;top:2px;width:16px;height:16px;border-radius:50%;
  background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.4);transition:transform .18s}
.sw input:checked+i{background:linear-gradient(135deg,var(--brand),var(--brand2));
  border-color:transparent;box-shadow:0 0 14px rgba(22,227,165,.35)}
.sw input:checked+i::after{transform:translateX(18px)}
.sw input:focus-visible+i{box-shadow:0 0 0 3px rgba(22,227,165,.25)}
.srchint{background:rgba(255,176,32,.08);border:1px solid rgba(255,176,32,.26);color:var(--warn);
  border-radius:10px;padding:10px 13px;font-size:12.5px;line-height:1.68;margin-bottom:12px}
.srchint b{display:block;margin-bottom:3px;color:var(--warn)}
.srchint .h{display:flex;gap:7px}
.srchint .h::before{content:'›';flex:none;opacity:.7}
.srcmsg{font-size:12px;border-radius:9px;padding:9px 12px;margin-top:4px;display:none}
.srcmsg.show{display:block}
.srcmsg.ok{background:rgba(47,227,164,.09);color:var(--ok);border:1px solid rgba(47,227,164,.28)}
.srcmsg.err{background:rgba(255,95,109,.09);color:var(--hot);border:1px solid rgba(255,95,109,.3)}

/* ================= 两列 ================= */
main{padding:20px 30px 64px;max-width:1680px;margin:0 auto}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:24px;align-items:start}
.col{background:transparent;min-width:0}
.col-h{display:flex;align-items:center;gap:9px;padding:0 2px 10px;
  border-bottom:1px solid var(--line);margin-bottom:14px;position:relative}
.col-h::after{content:'';position:absolute;left:0;bottom:-1px;width:56px;height:2px;
  border-radius:2px;background:linear-gradient(90deg,var(--brand),transparent)}
.col-h .ttl{font-size:15px;font-weight:700;letter-spacing:.3px}
.col-h .n{font-size:11.5px;color:var(--tx3);margin-left:auto;white-space:nowrap;
  font-family:ui-monospace,Consolas,monospace}
.src{font-size:10px;padding:2px 8px;border-radius:6px;font-weight:700;letter-spacing:.5px}
.bqq{background:rgba(76,154,255,.14);color:var(--qq);border:1px solid rgba(76,154,255,.3)}
.bwx{background:rgba(34,211,154,.13);color:var(--wx);border:1px solid rgba(34,211,154,.3)}
.col-none{color:var(--tx3);font-size:12.5px;padding:22px;text-align:center;
  border:1px dashed var(--line);border-radius:12px;background:var(--bg2)}

.cards{display:grid;grid-template-columns:1fr;gap:10px;margin-bottom:18px}
.card{background:var(--bg2);border:1px solid var(--line);border-radius:12px;
  padding:13px 15px;border-left:3px solid var(--ac);transition:.2s;position:relative;
  animation:rise .32s ease both}
.card:hover{transform:translateY(-2px);border-color:var(--line2);box-shadow:var(--shadow)}
.card.hot{border-left-color:var(--hot)}
.card.warn{border-left-color:var(--warn)}
.card-h{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:7px}
.badge{font-size:10px;padding:2px 8px;border-radius:6px;background:rgba(76,154,255,.13);
  color:var(--ac);border:1px solid rgba(76,154,255,.24);letter-spacing:.3px}
.dl{font-size:11.5px;font-weight:700;color:var(--hot);white-space:nowrap;
  font-family:ui-monospace,Consolas,monospace}
.card.warn .dl{color:var(--warn)}
.card-t{font-size:12.5px;line-height:1.65;display:-webkit-box;-webkit-line-clamp:3;
  -webkit-box-orient:vertical;overflow:hidden;color:var(--tx)}
.card-f{margin-top:9px;font-size:10.5px;color:var(--tx3);display:flex;
  justify-content:space-between;align-items:flex-end;gap:8px}
.card-f .who{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card.done{opacity:.45;border-left-color:var(--tx3);background:transparent}
.card.done .card-t{text-decoration:line-through;text-decoration-color:var(--tx3);color:var(--tx2)}

.btn-done,.btn-reset{flex:none;border:1px solid var(--line);background:var(--bg3);color:var(--tx2);
  font:inherit;font-size:11px;line-height:1.4;padding:3px 9px;border-radius:7px;
  cursor:pointer;white-space:nowrap;transition:.15s}
.btn-done:hover{border-color:var(--ok);color:var(--ok);background:rgba(47,227,164,.1)}
.btn-done.on{background:rgba(47,227,164,.16);border-color:rgba(47,227,164,.5);color:var(--ok)}
.btn-done.on:hover{background:rgba(47,227,164,.24);color:var(--ok)}
.btn-reset{margin-left:auto}
.btn-reset:hover{border-color:var(--line2);color:var(--tx)}

/* ---- 群卡片（details）---- */
.grp{background:var(--bg2);border:1px solid var(--line);border-radius:12px;
  margin-bottom:10px;overflow:hidden;transition:.18s;animation:rise .32s ease both}
.grp:hover{border-color:var(--line2)}
.grp[open]{box-shadow:var(--shadow);border-color:var(--line2)}
.grp>summary{padding:11px 14px;cursor:pointer;display:flex;justify-content:space-between;
  gap:10px;align-items:baseline;list-style:none;font-weight:600;font-size:13px;transition:.15s}
.grp>summary::-webkit-details-marker{display:none}
.grp>summary:hover{background:var(--bg3)}
.grp[open]>summary{background:linear-gradient(180deg,rgba(22,227,165,.05),transparent)}
.gname{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gmeta{font-size:10.5px;color:var(--tx3);font-weight:400;white-space:nowrap;
  font-family:ui-monospace,Consolas,monospace}
/* 整个群都被「已完成」收起来之后的空壳：群还在，只是个提示 */
.grp.ghost{background:transparent;border-style:dashed;opacity:.75}
.grp.ghost>summary{font-weight:500;color:var(--tx3)}
.grp.ghost>summary:hover{background:var(--bg3)}
.ghost-body{padding:10px 14px;border-top:1px dashed var(--line);
  font-size:12px;color:var(--tx2);line-height:1.72}
.ghost-body b{color:var(--ok);font-weight:600}

/* ---- 消息行 ---- */
.msg{padding:10px 14px;border-top:1px solid var(--line);transition:.15s;position:relative}
.msg:hover{background:var(--bg3)}
.msg:hover::before{content:'';position:absolute;left:0;top:0;bottom:0;width:2px;
  background:var(--brand);opacity:.55}
.msg-h{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;
  font-size:11px;color:var(--tx3);margin-bottom:4px}
.msg-h b{color:var(--tx2);font-weight:600;font-size:11.5px}
.msg-h .btn-done{margin-left:auto}
.msg-x{font-size:12.5px;line-height:1.66;white-space:pre-wrap;word-break:break-word;color:var(--tx)}
/* 「先发正文、再单独 @所有人」的补全：正文是从上文捞回来的，得让人看得出来 */
.msg-c{margin-top:4px;padding:6px 10px;border-left:2px solid var(--line2);border-radius:0 6px 6px 0;
  background:var(--bg3);color:var(--tx2);font-size:12px;line-height:1.62;
  white-space:pre-wrap;word-break:break-word}
.msg-c span{display:block;margin-top:2px;color:var(--tx3);font-size:10.5px}
.msg-cf{margin-top:4px;color:var(--tx3);font-size:10.5px}
.msg.done{opacity:.4}
.msg.done .msg-x{text-decoration:line-through;text-decoration-color:var(--tx3)}
.tag{font-size:10px;padding:1.5px 7px;border-radius:5px;background:var(--bg4);color:var(--tx2);
  border:1px solid var(--line);letter-spacing:.2px}
.dtag{font-size:10px;padding:1.5px 7px;border-radius:5px;background:rgba(255,95,109,.12);
  color:var(--hot);font-weight:700;border:1px solid rgba(255,95,109,.28)}
mark{background:rgba(255,214,10,.28);color:inherit;padding:0 2px;border-radius:3px;
  box-shadow:0 0 0 1px rgba(255,214,10,.18)}
.empty{color:var(--tx3);padding:26px;text-align:center;background:var(--bg2);
  border:1px dashed var(--line);border-radius:12px;font-size:12.5px}
.warn{margin:14px 30px 0;padding:11px 14px;background:rgba(255,176,32,.08);
  border:1px solid rgba(255,176,32,.26);border-radius:10px;color:var(--warn);
  font-size:12.5px;line-height:1.75}
.warn b{color:var(--warn)}
.warn code{background:var(--hover);padding:1px 6px;border-radius:5px;
  font-family:ui-monospace,Consolas,monospace;font-size:11.5px}

/* ---- 取消关注 ---- */
.btn-mute{flex:none;border:1px solid transparent;background:transparent;color:var(--tx3);
  font:inherit;font-size:11px;line-height:1;padding:4px 7px;border-radius:6px;
  cursor:pointer;transition:.15s;opacity:.5}
.grp>summary:hover .btn-mute{opacity:1}
.btn-mute:hover{color:var(--hot);border-color:rgba(255,95,109,.3);background:rgba(255,95,109,.1)}
.mutedbox{margin-left:2px}
.mutedbox>summary{cursor:pointer;list-style:none;font-size:12px;color:var(--tx2);
  padding:5px 11px;border:1px solid var(--line);border-radius:999px;background:var(--bg3);
  white-space:nowrap;transition:.15s}
.mutedbox>summary::-webkit-details-marker{display:none}
.mutedbox>summary:hover{border-color:var(--line2);color:var(--tx)}
.mutedbox[open]>summary{border-color:rgba(22,227,165,.4);color:var(--brand)}
.mutedpanel{position:absolute;margin-top:6px;background:var(--bg2);border:1px solid var(--line2);
  border-radius:12px;box-shadow:var(--shadow);padding:8px;min-width:280px;
  max-height:320px;overflow:auto;z-index:30;max-width:calc(100vw - 60px)}
.mutedpanel .mrow{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;
  font-size:12.5px}
.mutedpanel .mrow:hover{background:var(--bg3)}
.mutedpanel .mrow span.mn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.mutedpanel .none{color:var(--tx3);font-size:12px;padding:9px 8px}
.btn-back{flex:none;border:1px solid var(--line);background:var(--bg3);color:var(--tx2);
  font:inherit;font-size:11px;padding:3px 9px;border-radius:7px;cursor:pointer;transition:.15s}
.btn-back:hover{border-color:var(--ok);color:var(--ok);background:rgba(47,227,164,.1)}
/* 页脚：服务在后台跑，这里给个低调的停止入口 */
.foot{max-width:1680px;margin:-8px auto 34px;padding:0 30px;display:none;
  align-items:center;gap:10px;flex-wrap:wrap;
  color:var(--tx3);font-size:12px;line-height:1.75}
.foot .sp{opacity:.5}
.foot a{color:var(--tx3);text-decoration:none;border-bottom:1px dashed currentColor;
  cursor:pointer;transition:color .15s}
.foot a:hover{color:var(--hot);border-bottom-color:var(--hot)}
.bye{max-width:560px;margin:120px auto;padding:0 24px;text-align:center;
  color:var(--tx);font-size:19px;font-weight:640;line-height:2.2}
.bye span{display:block;color:var(--tx2);font-size:13.5px;font-weight:400}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media(max-width:900px){
  .cols{grid-template-columns:1fr}
  main,header{padding-left:16px;padding-right:16px}
  .warn,.demobar,.updbar{padding-left:16px;padding-right:16px}
  input[type=search]{width:100%}
  .gmeta{display:none}
  .foot{padding-left:16px;padding-right:16px}
  .stats{gap:8px}
  .stat{min-width:calc(50% - 4px)}
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
      <button class="btn-act ic" id="btnTheme" title="切换深色 / 浅色"><span id="thIc">☾</span></button>
      <button class="btn-act primary" id="btnRefresh">↻ 刷新数据</button>
      <button class="btn-act" id="btnSrc">数据源</button>
      <button class="btn-act" id="btnDoctor">环境体检</button>
      <button class="btn-act" id="btnDemo">看示例</button>
    </div>
  </div>
  <div class="stats">
    <div class="stat"><b>%%TOTAL%%</b><span>@所有人 消息</span></div>
    <div class="stat"><b>%%GN%%</b><span>有喊话的群</span></div>
    <div class="stat click" id="stSoon" title="点一下：只看带截止时间的"><b style="color:var(--hot)">%%SOON%%</b><span>14 天内到期</span></div>
    <div class="stat click" id="stDone" title="点一下：显示 / 收起已完成的条目"><b id="doneN" style="color:var(--ok)">0</b><span id="doneLbl">已完成</span></div>
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
const doneLbl = document.getElementById('doneLbl');
const btnTheme = document.getElementById('btnTheme');
const thIc = document.getElementById('thIc');
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

/* ---------- 界面偏好：隐藏已完成 / 只看带日期的 ----------
   这两项是"设一次就一直这样"的东西，刷新、重开、换端口都得记住。
   cookie 走 path=/ 在整台机器上共享（预览面板换随机端口也不会丢），
   localStorage 有就用它的（更及时），两边都写。 */
const PKEY = 'qad_prefs_v1';
function readPrefs() {
  let o = {};
  try { o = JSON.parse(localStorage.getItem(PKEY) || '{}') || {}; } catch (e) {}
  const m = document.cookie.match(/(?:^|;\\s*)qadp=([^;]*)/);
  if (m) {
    try { o = Object.assign(JSON.parse(decodeURIComponent(m[1])) || {}, o); } catch (e) {}
  }
  return o;
}
function savePrefs() {
  const s = JSON.stringify({
    hideDone: hideDone.checked, onlyTodo: onlyTodo.checked, theme: themeNow()
  });
  try { localStorage.setItem(PKEY, s); } catch (e) {}
  try {
    document.cookie = 'qadp=' + encodeURIComponent(s) + ';path=/;max-age=63072000;SameSite=Lax';
  } catch (e) {}
}
const PREF = readPrefs();
/* 默认：隐藏已完成 = 勾上（标完就不想再看见它），只看带日期的 = 不勾 */
hideDone.checked = PREF.hideDone === undefined ? true : !!PREF.hideDone;
onlyTodo.checked = !!PREF.onlyTodo;

/* ---------- 主题：深色为默认，浅色存进同一份偏好 ----------
   <head> 里那段内联脚本已经把 data-theme 定好了（防首屏闪白），
   这里只负责按钮图标与状态同步。 */
function themeNow() {
  return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
}
function paintTheme() {
  const t = themeNow();
  thIc.textContent = t === 'light' ? '☀' : '☾';
  btnTheme.title = t === 'light' ? '当前浅色 —— 点一下换深色' : '当前深色 —— 点一下换浅色';
  btnTheme.setAttribute('aria-label', btnTheme.title);
}
function setTheme(t) {
  if (t === 'light') document.documentElement.setAttribute('data-theme', 'light');
  else document.documentElement.removeAttribute('data-theme');
  paintTheme();
}
/* 偏好里存了主题、但 <head> 没读到（极端情况：storage 被清了一半）→ 补一次 */
if (PREF.theme === 'light' || PREF.theme === 'dark') setTheme(PREF.theme);
paintTheme();
btnTheme.onclick = () => { setTheme(themeNow() === 'light' ? 'dark' : 'light'); savePrefs(); };

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
  // 「隐藏已完成」不再参与这里的判断 —— 它现在默认就是勾上的，
  // 算进来的话每张卡都会默认展开，页面长得没法看
  const open = (qs.length || onlyTodo.checked || gsel.value) ? ' open' : '';
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
  doneLbl.textContent = done.size ? (hd ? '已完成 · 已收起' : '已完成 · 显示中') : '已完成';
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
onlyTodo.addEventListener('change', () => { savePrefs(); render(); });
hideDone.addEventListener('change', () => { savePrefs(); render(); });

/* 顶上两张数字卡直接当开关用：点「已完成」收起 / 放开，点「14 天内到期」只看带日期的 */
document.getElementById('stDone').onclick = () => {
  hideDone.checked = !hideDone.checked; savePrefs(); render();
};
document.getElementById('stSoon').onclick = () => {
  onlyTodo.checked = !onlyTodo.checked; savePrefs(); render();
};
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

  // QQ 号（多个也默认选上最近用的那个，不再逼用户选）
  qqUin.textContent = '';
  const uins = d.qq.uin_list || [];
  if (!uins.length) qqUin.appendChild(opt('', '这台机器还没登录过 QQ'));
  uins.forEach(u => qqUin.appendChild(opt(u, u)));
  qqUin.value = d.qq.default || d.qq.uin || (uins[0] || '');

  // 微信开关
  wxOn.checked = !!d.wechat.enabled;
  srcSt('blkWX', wxOn, wxSt);

  // 微信账号
  wxDir.textContent = '';
  const cands = d.wechat.candidates || [];
  if (!cands.length) wxDir.appendChild(opt('', '没找到微信数据目录'));
  cands.forEach(c => wxDir.appendChild(opt(
    c.path,
    c.account + ' · ' + c.db_count + ' 个库 · ' + c.mtime_txt + (c.official ? '' : '（旧位置）')
  )));
  // 默认选上：配置里存的 > 自动探测到的那个。选了就存进 config.json，刷新不会再变
  wxDir.value = d.wechat.default || d.wechat.db_dir || d.wechat.current || '';
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

function srcBody() {
  return {
    qq: { enabled: qqOn.checked, uin: qqUin.value || '', exe: qqExe.value || '' },
    wechat: { enabled: wxOn.checked, db_dir: wxDir.value || '' }
  };
}

/* quiet=true 是"改一下就记住"：开关和账号下拉不等人点保存，
   省得下次刷新又变成别的号 —— 但只给一句轻提示，不打断操作 */
function srcSave(quiet) {
  if (!SRC) { srcMsgShow('还没读到当前设置，等一下再点保存。', 'err'); return; }
  const body = srcBody();
  if (!body.qq.uin && (SRC.qq.uin_list || []).length) {
    srcMsgShow('QQ 号没选上，重新点一下下拉框。', 'err');
    return;
  }
  const btn = document.getElementById('srcSave');
  if (!quiet) { btn.disabled = true; btn.textContent = '保存中…'; }
  fetch('/api/sources', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(r => r.json()).then(x => {
    if (!quiet) { btn.disabled = false; btn.textContent = '保存'; }
    if (!x.ok) { srcMsgShow(x.error || '没保存成功', 'err'); return; }
    srcRender(x.info);
    srcMsgShow(quiet
      ? '已记住。点「↻ 刷新数据」按新设置读一次。'
      : '已保存。点右上角「↻ 刷新数据」按新设置读一次，看板上就会跟着变。', 'ok');
  }).catch(() => {
    if (!quiet) { btn.disabled = false; btn.textContent = '保存'; }
    srcMsgShow('保存失败，服务可能已经停了。', 'err');
  });
}

if (document.getElementById('btnSrc')) {
  document.getElementById('btnSrc').onclick = () => srcOpen(true);
  document.getElementById('srcClose').onclick = () => srcOpen(false);
  document.getElementById('srcCancel').onclick = () => srcOpen(false);
  srcModal.onclick = e => { if (e.target === srcModal) srcOpen(false); };
  document.getElementById('srcSave').onclick = () => srcSave(false);
  document.getElementById('qqExePick').onclick = () => srcPickPath('exe');
  document.getElementById('wxDirPick').onclick = () => srcPickPath('dir');
  // 开关和账号：改一下就落盘，下次刷新还是这个
  qqOn.onchange = () => { srcSt('blkQQ', qqOn, qqSt); srcSave(true); };
  wxOn.onchange = () => { srcSt('blkWX', wxOn, wxSt); srcSave(true); };
  qqUin.onchange = () => srcSave(true);
  wxDir.onchange = () => { wxPathShow.value = wxDir.value || ''; srcSave(true); };

  // 列头「读取已关闭」小标签，点一下直接开面板
  const offQQ = document.getElementById('offQQ');
  const offWX = document.getElementById('offWX');
  if (SRCOFF.qq) { offQQ.style.display = ''; offQQ.onclick = () => srcOpen(true); }
  if (SRCOFF.wechat) { offWX.style.display = ''; offWX.onclick = () => srcOpen(true); }
  // 不再主动弹面板 —— 默认值已经选好了，天天弹等于天天问同一句话
}

/* ---------- 更新提示（每天一次心跳，结果由服务端缓存） ---------- */
const updbar = document.getElementById('updbar');
const updModal = document.getElementById('updModal');
const updList = document.getElementById('updList');
let UPD = null, updTimer = null;

function updModalShow(on) { updModal.classList.toggle('show', !!on); }

function updRender() {
  if (!UPD || !UPD.notes) return;
  const n = UPD.notes.length;
  const meta = '<div class="updmeta">' +
    '<b>v' + esc(UPD.version || '') + '</b>' +
    '<span>你的版本</span>' +
    '<i>→</i>' +
    '<b>' + esc(UPD.latest || '') + '</b>' +
    '<span>' + esc(UPD.latest_date || '远端最新') + '</span>' +
    (n ? '<em>' + n + ' 条改动</em>' : '') +
    '</div>';
  updList.innerHTML = meta + (UPD.notes.map(x =>
    '<div class="row"><div class="cd">' + esc(x.date || '') + '</div>' +
    '<div class="cm">' + esc(x.msg || '') + '</div></div>').join('')
    || '<div class="row"><div class="cm">这次没写提交说明 —— 可以直接更，' +
       '想先看改了什么就点下面「在 GitHub 上看」。</div></div>');
  document.getElementById('updGit').href = UPD.url || '#';
}

function updLoad(force) {
  if (!SERVED || DEMO) return;
  fetch('/api/update' + (force ? '?force=1' : ''))
    .then(r => r.json()).then(d => {
      UPD = d;
      if (d.has_update) {
        const n = (d.notes || []).length;
        document.getElementById('updSub').textContent =
          '远端 ' + d.latest + (d.latest_date ? ' · ' + d.latest_date : '') +
          (n ? '，' + n + ' 条改动' : '') +
          '（你这里是 ' + (d.version || '') + '）';
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

    # 数据源状态：哪一侧被关掉了（列头挂个「读取已关闭」标签）
    src_off = {"qq": False, "wechat": False}
    try:
        src_off["qq"] = not bool(settings.get("qq", "enabled", True))
        src_off["wechat"] = not bool(settings.get("wechat", "enabled", True))
    except Exception:
        pass

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
