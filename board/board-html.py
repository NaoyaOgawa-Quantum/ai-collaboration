#!/usr/bin/env python3
"""board-html.py — 掲示板の人間向け viewer (self-contained HTML) を生成する。

**generated local view**。 正本は immutable event のみで、 出力は派生・使い捨て
(既定の出力先 = `~/.cache/agent-board/<掲示板>.html`。 commit しない)。

    python3 board-html.py --root <board>        # 実 event から HTML を生成 (--board <name> でも)
    python3 board-html.py --root <board> --open # 生成して既定ブラウザで開く
    python3 board-html.py --demo                # 架空 event で見本を生成 (実 repo には触らない)
    python3 board-html.py --out P               # 出力先を変える
    python3 board-html.py --project K

データは `board-view.py --json` (= 述語の唯一の実装) を subprocess で呼んで得る。 本 script は
描画だけを持ち、 thread 状態の判定を二重実装しない。 --demo は temp git repo に fixture を積んで
同じ経路を通す (= 見本でも述語は本物)。

外部 resource は使わない。ローカルの snapshot を表示する。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
VIEW = ENGINE / "board-view.py"
CACHE = Path.home() / ".cache" / "agent-board"


def payload_from(root: Path, project: str | None) -> dict:
    argv = [sys.executable, str(VIEW), "--json", "--root", str(root)]
    if project:
        argv += ["--project", project]
    r = subprocess.run(argv, capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


# ---------------------------------------------------------------- demo fixture

def demo_payload() -> dict:
    # Fictional examples only. Use the production reducer, with no real posts.
    import importlib.util
    spec = importlib.util.spec_from_file_location("view_demo", VIEW)
    view = importlib.util.module_from_spec(spec); spec.loader.exec_module(view)
    now = view.now_utc()
    events = []
    examples = [
        ("数値計算を別の方法で確かめる", "requested", "codex"),
        ("原稿の式とコードを照合する", "working", "claude"),
        ("図の再計算結果を確認する", "submitted", "codex"),
        ("近似の適用範囲を見直す", "revision", "claude"),
        ("前提の選び方を相談する", "blocked", "codex"),
        ("参考文献の実在を確認する", "accepted", "claude"),
    ]
    for n, (title, status, worker) in enumerate(examples):
        requester = "claude" if worker == "codex" else "codex"
        thread = f"2026-09-06-example-{n+1}"
        def add(kind, agent, *, reply=None):
            t = now - dt.timedelta(hours=2, minutes=20-n*2) + dt.timedelta(seconds=len(events))
            e, p = view.build_template(kind="update", project="demo", thread=thread,
                task=title, agent=agent, repo=None, restricted=False, lease_hours=12,
                summary={"request":title,"claim":"引き受けました。計算条件から確認します。", "submit":"再現手順と結果を提出しました。", "revise":"境界条件の確認を追加してください。", "blocker":"採用する近似について判断をお願いします。", "accept":"独立に再計算して確認しました。"}[kind], now=t)
            e.update(schema_version=2, kind=kind, _path=p, _committed=True)
            e["actor"]["session_id"]=f"{agent}-demo-{n+1}"
            e["request_id"] = e["event_id"] if kind=="request" else request["event_id"]
            if kind=="request": e.update(assignee={"agent":worker,"session_id":f"{worker}-demo-{n+1}"}, acceptance="再計算できるコードと結果を揃え、前提と未確認の点を明記する。")
            if kind=="claim": e["lease_until"]=view.iso_z(now+dt.timedelta(hours=5))
            if kind=="submit": e["deliverables"]=["results/check.md", "checks/reproduce.py"]
            if kind=="accept": e["references"]=["results/review.md"]
            if reply: e["reply_to"]=reply["event_id"]
            events.append(e); return e
        request=add("request",requester)
        if status=="requested": continue
        claim=add("claim",worker)
        if status=="working": continue
        if status=="blocked": add("blocker",worker); continue
        submission=add("submit",worker,reply=claim)
        if status=="revision": add("revise",requester,reply=submission)
        if status=="accepted": add("accept",requester,reply=submission)
    payload=view.to_json_payload(Path("."),view.derive(events,now),[],now=now,locked=False)
    payload["root"]="デモ · 架空の依頼です"
    payload["head"]="demo"
    return payload


# ---------------------------------------------------------------- template

HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>寄合所</title>
<style>
/* 色は Radix Colors の段 (9 = 塗り / 11 = 文字) に合わせた token。 状態の色は意味にだけ使う。 */
:root{
  --side:#F8F9FA; --panel:#FFFFFF; --sunken:#F1F3F5; --hover:rgba(17,24,28,.045); --sel:rgba(62,99,221,.10); --ring:rgba(62,99,221,.22);
  --line:#E6E8EB; --line-2:#D7DBDF;
  --text:#1C2024; --text-2:#4A5058; --muted:#6B6F78;
  --accent:#3E63DD; --accent-ink:#3A5BC7;
  --red:#E5484D; --red-ink:#CE2C31; --orange:#F76B15; --orange-ink:#CC4E00; --purple:#8E4EC6; --purple-ink:#8145B5;
  --blue:#0090FF; --blue-ink:#0D74CE; --green:#30A46C; --green-ink:#218358; --gray:#8B8D98; --gray-ink:#60646C;
  --claude:#D97757; --codex:#10A37F; --human:#7C7F88;
  --warn-bg:#FFF7E8; --warn-line:#F5D9A3; --warn-ink:#8A4B00;
  --err-bg:#FFF0F0; --err-line:#F4C0C0; --err-ink:#B42318;
  --info-bg:#EEF2FF; --info-line:#CBD5FE; --info-ink:#3730A3;
  --font:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Hiragino Kaku Gothic ProN","Noto Sans JP","Yu Gothic UI",system-ui,sans-serif;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --side:#111113; --panel:#18191B; --sunken:#212225; --hover:rgba(255,255,255,.05); --sel:rgba(84,114,228,.20); --ring:rgba(84,114,228,.35);
    --line:#2B2D31; --line-2:#3A3D43;
    --text:#EDEEF0; --text-2:#B5B9C0; --muted:#8D929B;
    --accent:#5472E4; --accent-ink:#9EB1FF;
    --red:#E5484D; --red-ink:#FF9592; --orange:#F76B15; --orange-ink:#FFA057; --purple:#9A5CD0; --purple-ink:#D19DFF;
    --blue:#0090FF; --blue-ink:#70B8FF; --green:#30A46C; --green-ink:#3DD68C; --gray:#6E7079; --gray-ink:#B0B4BA;
    --warn-bg:#2A2010; --warn-line:#4D3A17; --warn-ink:#F5C77E;
    --err-bg:#2D1415; --err-line:#562326; --err-ink:#FF9592;
    --info-bg:#1A1D33; --info-line:#2F3766; --info-ink:#B8C3FF;
    color-scheme:dark;
  }
}
:root[data-theme="dark"]{
  --side:#111113; --panel:#18191B; --sunken:#212225; --hover:rgba(255,255,255,.05); --sel:rgba(84,114,228,.20); --ring:rgba(84,114,228,.35);
  --line:#2B2D31; --line-2:#3A3D43;
  --text:#EDEEF0; --text-2:#B5B9C0; --muted:#8D929B;
  --accent:#5472E4; --accent-ink:#9EB1FF;
  --red:#E5484D; --red-ink:#FF9592; --orange:#F76B15; --orange-ink:#FFA057; --purple:#9A5CD0; --purple-ink:#D19DFF;
  --blue:#0090FF; --blue-ink:#70B8FF; --green:#30A46C; --green-ink:#3DD68C; --gray:#6E7079; --gray-ink:#B0B4BA;
  --warn-bg:#2A2010; --warn-line:#4D3A17; --warn-ink:#F5C77E;
  --err-bg:#2D1415; --err-line:#562326; --err-ink:#FF9592;
  --info-bg:#1A1D33; --info-line:#2F3766; --info-ink:#B8C3FF;
  color-scheme:dark;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--panel);color:var(--text);font:14px/1.6 var(--font);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
button{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer;text-align:inherit}
select{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
kbd{font:11px/1 var(--mono);border:1px solid var(--line-2);border-bottom-width:2px;border-radius:4px;padding:2px 5px;color:var(--text-2);background:var(--panel)}
svg{width:16px;height:16px;flex:none}
.mono{font-family:var(--mono);font-size:.9em}
.muted{color:var(--muted)}
.bad{color:var(--red-ink);font-weight:600}

/* 状態の色 (= 一覧の点・見出し・badge・経過の種別で共通) */
.g-attention{--c:var(--red);--ci:var(--red-ink)}
.g-review{--c:var(--orange);--ci:var(--orange-ink)}
.g-todo{--c:var(--purple);--ci:var(--purple-ink)}
.g-doing{--c:var(--blue);--ci:var(--blue-ink)}
.g-done{--c:var(--green);--ci:var(--green-ink)}
.g-legacy{--c:var(--gray);--ci:var(--gray-ink)}

/* ---- app shell: 左 = 一覧 / 右 = 詳細。 それぞれ独立に scroll */
.app{display:grid;grid-template-columns:minmax(300px,390px) minmax(0,1fr);height:100vh;height:100dvh}
.side{display:flex;flex-direction:column;min-height:0;background:var(--side);border-right:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;padding:16px 14px 10px 16px}
.logo{display:flex;gap:3px;flex:none}
.logo i{width:10px;height:10px;border-radius:50%}
.logo .claude{background:var(--claude)} .logo .codex{background:var(--codex)}
.bt{min-width:0;flex:1}
.bt h1{margin:0;font-size:17px;font-weight:700;letter-spacing:.04em;line-height:1.25}
.stamp{margin:0;font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.acts{display:flex;gap:2px}
.ib{width:32px;height:32px;display:grid;place-items:center;border-radius:8px;color:var(--text-2)}
.ib:hover{background:var(--hover);color:var(--text)}
.ib.spin svg{animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.tools{padding:2px 12px 8px;display:flex;flex-direction:column;gap:8px}
.search{display:flex;align-items:center;gap:8px;height:36px;padding:0 8px 0 10px;border:1px solid var(--line);border-radius:9px;background:var(--panel);color:var(--muted)}
.search:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px var(--ring)}
.search input{flex:1;min-width:0;border:0;background:none;outline:0;color:var(--text);font:inherit;font-size:13.5px}
.search input::placeholder{color:var(--muted)}
.scope{display:grid;grid-template-columns:repeat(3,1fr);gap:2px;padding:3px;background:var(--sunken);border-radius:9px}
.scope button{height:28px;border-radius:7px;font-size:13px;color:var(--text-2);display:flex;align-items:center;justify-content:center;gap:6px}
.scope button:hover{color:var(--text)}
.scope button[aria-selected="true"]{background:var(--panel);color:var(--text);font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,.08),0 0 0 1px var(--line)}
.scope .n{font-size:11.5px;color:var(--muted);font-weight:500;font-variant-numeric:tabular-nums}
.filters>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:var(--text-2);padding:2px 6px 2px 2px;border-radius:6px;user-select:none}
.filters>summary::-webkit-details-marker{display:none}
.filters>summary:hover{color:var(--text)}
.filters>summary::before{content:"";width:5px;height:5px;margin:0 3px;border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;transform:rotate(-45deg);transition:transform .15s}
.filters[open]>summary::before{transform:rotate(45deg) translate(-1px,-1px)}
.fcount{display:inline-grid;place-items:center;min-width:18px;height:18px;padding:0 5px;border-radius:999px;background:var(--accent);color:#fff;font-size:11px;font-weight:700}
.fbody{padding:8px 2px 4px;display:flex;flex-direction:column;gap:12px}
.fl{display:flex;justify-content:space-between;align-items:baseline;gap:8px;font-size:11.5px;font-weight:600;color:var(--muted);margin-bottom:6px}
.fl small{font-weight:400}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{height:26px;padding:0 10px;border-radius:999px;border:1px solid var(--line);background:var(--panel);font-size:12.5px;color:var(--muted);display:inline-flex;align-items:center;gap:6px}
.chip:hover{border-color:var(--line-2);color:var(--text-2)}
.chip[aria-pressed="true"]{border-color:color-mix(in srgb,var(--accent) 45%,transparent);background:var(--sel);color:var(--text)}
.chip .av{width:14px;height:14px;font-size:0}
.fsel{width:100%;height:34px;padding:0 8px;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
.reset{font-size:12.5px;color:var(--accent-ink);align-self:flex-start}
.reset:hover{text-decoration:underline}

.notes{padding:0 12px}
.note{margin:0 0 8px;padding:9px 12px;border-radius:9px;border:1px solid;font-size:12.5px;line-height:1.6;overflow-wrap:anywhere}
.note.err{background:var(--err-bg);border-color:var(--err-line);color:var(--err-ink)}
.note.warn{background:var(--warn-bg);border-color:var(--warn-line);color:var(--warn-ink)}
.note.info{background:var(--info-bg);border-color:var(--info-line);color:var(--info-ink)}
.note.plain{background:var(--sunken);border-color:transparent;color:var(--text-2)}
.note>summary{cursor:pointer;font-weight:600}
.note ul{margin:6px 0 2px;padding-left:18px}
.note li+li{margin-top:4px}

.list{flex:1;overflow:auto;padding:0 8px 16px;overscroll-behavior:contain;border-top:1px solid var(--line)}
.gh{position:sticky;top:0;z-index:1;margin:0;padding:12px 10px 6px;display:flex;align-items:center;gap:8px;font-size:12px;font-weight:700;color:var(--text-2);background:var(--side)}
.gh .gc{font-weight:500;color:var(--muted);font-variant-numeric:tabular-nums}
.gh .gi{margin-left:auto;font-weight:400;font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.dot{width:8px;height:8px;border-radius:50%;background:var(--c,var(--gray));flex:none}
.row{position:relative;width:100%;display:grid;grid-template-columns:8px minmax(0,1fr) auto;gap:10px;align-items:start;padding:9px 10px;border-radius:9px}
.row .dot{margin-top:7px}
.row:hover{background:var(--hover)}
.row[aria-current="true"]{background:var(--sel)}
.row[aria-current="true"]::before{content:"";position:absolute;left:0;top:9px;bottom:9px;width:3px;border-radius:0 2px 2px 0;background:var(--accent)}
.rt{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font-size:13.5px;font-weight:500;line-height:1.5;overflow-wrap:anywhere}
.rm{display:flex;gap:5px;margin-top:3px;font-size:12px;color:var(--muted);white-space:nowrap;min-width:0}
.rm span{overflow:hidden;text-overflow:ellipsis}
.rm .proj{flex:none;max-width:45%}
.ra{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;padding-top:2px}
.grp-done .rt{font-weight:400;color:var(--text-2)}
.lempty{padding:40px 16px;text-align:center;color:var(--muted);font-size:13px}
.sfoot{padding:10px 16px 12px;border-top:1px solid var(--line);font-size:11.5px;line-height:1.7;color:var(--muted);display:flex;flex-direction:column}
.sfoot .keys{display:flex;flex-wrap:wrap;gap:4px 10px;margin-top:2px}

/* ---- 詳細 */
.detail{min-width:0;overflow:auto;background:var(--panel);outline:0}
.dwrap{max-width:860px;margin:0 auto;padding:30px 40px 96px}
.back{display:none}
.crumb{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-size:12.5px;color:var(--muted);min-width:0}
.crumb span{overflow-wrap:anywhere}
.crumb .sep{color:var(--line-2)}
.dt{margin:8px 0 12px;font-size:22px;line-height:1.5;font-weight:700;letter-spacing:.01em;font-feature-settings:"palt";text-wrap:pretty;overflow-wrap:anywhere}
.badges{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center}
.badge{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:999px;font-size:12px;font-weight:600;background:color-mix(in srgb,var(--c) 13%,transparent);color:var(--ci)}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--c)}
.badge.plain{background:var(--sunken);color:var(--text-2);font-weight:500}
.badge.plain::before{display:none}
.metaline{font-size:12.5px;color:var(--muted)}

.card{margin-top:24px;border:1px solid var(--line);border-radius:14px;background:var(--panel);overflow:hidden}
.card+.card{margin-top:16px}
.card-h{padding:12px 18px;border-bottom:1px solid var(--line);font-size:13px;font-weight:700;color:var(--text-2);background:var(--side)}

.steps{list-style:none;margin:0;padding:20px 12px 6px;display:grid;grid-template-columns:repeat(4,1fr)}
.steps li{position:relative;display:flex;flex-direction:column;align-items:center;gap:4px;font-size:12.5px;color:var(--muted);text-align:center}
.steps li::before{content:"";position:absolute;top:12px;right:calc(50% + 16px);width:calc(100% - 32px);height:2px;border-radius:1px;background:var(--line)}
.steps li:first-child::before{display:none}
.steps li.done::before,.steps li.cur::before{background:var(--accent)}
.node{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-size:12px;font-weight:700;border:2px solid var(--line-2);color:var(--muted);background:var(--panel)}
.done .node{background:var(--accent);border-color:var(--accent);color:#fff}
.done .node svg{width:14px;height:14px;stroke-width:3}
.cur .node{border-color:var(--accent);color:var(--accent-ink);box-shadow:0 0 0 4px var(--ring)}
.done .sl{color:var(--text-2)}
.cur .sl{color:var(--text);font-weight:700}
.steps .sub{font-size:11.5px;font-weight:600;color:var(--accent-ink)}

.turn{margin:14px 18px 4px;padding:14px 16px;border-radius:12px;background:var(--sunken)}
.turn.fin{background:color-mix(in srgb,var(--green) 10%,transparent)}
.turn.bad{background:var(--err-bg);color:var(--err-ink)}
.turn-h{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;font-size:12.5px;font-weight:600;color:var(--muted)}
.turn.fin .turn-h{color:var(--green-ink)}
.turn .who{font-size:14px;color:var(--text)}
.turn p{margin:6px 0 0;font-size:14px;line-height:1.7}

.props{display:grid;grid-template-columns:112px minmax(0,1fr);gap:10px 16px;margin:0;padding:16px 18px 6px;font-size:13.5px;align-items:center}
.props dt{color:var(--muted);font-size:12.5px}
.props dd{margin:0;min-width:0}
.sect{padding:12px 18px 16px}
.sect+.sect{border-top:1px solid var(--line)}
.sect h4{margin:0 0 6px;font-size:12.5px;font-weight:600;color:var(--muted)}
.sect p{margin:0;font-size:14px;line-height:1.8;white-space:pre-wrap;overflow-wrap:anywhere}

.who{display:inline-flex;align-items:center;gap:6px;max-width:100%;min-width:0;vertical-align:middle}
button.who{border-radius:6px;padding:2px 6px 2px 2px;margin:-2px -6px -2px -2px}
button.who:hover{background:var(--hover)}
.av{width:20px;height:20px;border-radius:50%;flex:none;display:inline-grid;place-items:center;font-size:10.5px;font-weight:700;color:#fff;background:var(--human)}
.av.claude{background:var(--claude)} .av.codex{background:var(--codex)}
.who .ag{font-weight:600;white-space:nowrap}
.who .sn{color:var(--text-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.rl{flex:none;white-space:nowrap;font-size:10.5px;font-weight:600;color:var(--purple-ink);border:1px solid color-mix(in srgb,var(--purple) 40%,transparent);border-radius:4px;padding:0 4px}

.paths{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-direction:column;gap:4px}
.path{display:flex;align-items:flex-start;gap:8px;width:100%;padding:6px 10px;border-radius:8px;background:var(--sunken);font:12px/1.55 var(--mono);color:var(--text-2)}
.path:hover{color:var(--text);box-shadow:inset 0 0 0 1px var(--line-2)}
.path .pv{flex:1;min-width:0;overflow-wrap:anywhere}
.path .pk{flex:none;font:600 11px/1.6 var(--font);color:var(--muted)}
.path svg{width:13px;height:13px;margin-top:2px;opacity:0;color:var(--muted)}
.path:hover svg,.path:focus-visible svg{opacity:1}

.hx{list-style:none;margin:0;padding:0}
.hx li{padding:14px 18px}
.hx li+li{border-top:1px solid var(--line)}
.hx-h{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;font-size:12.5px;color:var(--muted)}
.hx-l{font-weight:700;color:var(--text-2)}
.hx p{margin:6px 0 0;font-size:14px;line-height:1.75;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.hx .note{margin:8px 0 0}
.jump{font-size:12px;color:var(--accent-ink);font-weight:500}
.jump:hover{text-decoration:underline}
.hx-h .jump{margin-left:auto}

.feedsec{margin-top:36px}
.feed-h{margin:0 0 16px;font-size:15px;font-weight:700;display:flex;align-items:baseline;gap:10px}
.feed-h small{font-size:12px;font-weight:400;color:var(--muted)}
.feed{list-style:none;margin:0;padding:0}
.item{position:relative;display:grid;grid-template-columns:28px minmax(0,1fr);gap:12px;padding-bottom:26px}
.item::before{content:"";position:absolute;left:13px;top:34px;bottom:6px;width:2px;border-radius:1px;background:var(--line)}
.item:last-child{padding-bottom:0}
.item:last-child::before{display:none}
.item>.av{width:28px;height:28px;font-size:12px;margin-top:1px}
.ibody{min-width:0;border-radius:10px;margin:-4px -10px;padding:4px 10px}
.item.flash .ibody{animation:flash 1.8s ease}
@keyframes flash{0%,30%{background:var(--sel)}100%{background:transparent}}
.ih{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px;min-height:28px;font-size:13px}
.ih .ag{font-weight:700}
.ih .sn{color:var(--text-2);max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;border-radius:4px}
.ih .sn:hover{color:var(--text);text-decoration:underline;text-decoration-color:var(--line-2)}
.kind{display:inline-flex;align-items:center;height:20px;padding:0 7px;border-radius:5px;font-size:11.5px;font-weight:700;background:color-mix(in srgb,var(--c) 13%,transparent);color:var(--ci)}
.flag{font-size:11.5px;color:var(--muted);border:1px dashed var(--line-2);border-radius:5px;padding:0 6px}
.ih time{margin-left:auto;font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.isum{margin:4px 0 0;font-size:14.5px;line-height:1.85;white-space:pre-wrap;overflow-wrap:anywhere;max-width:74ch}
.item.dead .isum,.item.dead .ih{opacity:.55}
.item.dead .isum{text-decoration:line-through;text-decoration-color:var(--muted)}
.rn{margin:8px 0 0;padding:8px 12px;border-radius:8px;background:var(--warn-bg);color:var(--warn-ink);font-size:12.5px;line-height:1.6}
.more,.rec{margin-top:8px}
.more>summary,.rec>summary{cursor:pointer;width:max-content;font-size:12.5px;color:var(--text-2);user-select:none}
.rec>summary{font-size:11.5px;color:var(--muted)}
.more>summary:hover,.rec>summary:hover{color:var(--text)}
.more p{margin:6px 0 0;font-size:13.5px;line-height:1.8;color:var(--text-2);white-space:pre-wrap;overflow-wrap:anywhere;max-width:74ch}
.rec dl{display:grid;grid-template-columns:auto minmax(0,1fr);gap:2px 12px;margin:6px 0 0;font-size:11.5px;color:var(--muted)}
.rec dd{margin:0;overflow-wrap:anywhere;color:var(--text-2)}
.sup{margin:8px 0 0;font-size:12.5px;color:var(--muted)}
.lease{margin-top:10px;max-width:420px}
.lease-l{display:flex;justify-content:space-between;gap:12px;font-size:12px;color:var(--muted)}
.bar{height:4px;border-radius:2px;background:var(--sunken);overflow:hidden;margin-top:5px}
.bar i{display:block;height:100%;border-radius:2px;background:var(--blue)}
.bar.low i{background:var(--orange)} .bar.out i{background:var(--red)}

.empty{max-width:440px;margin:16vh auto 0;text-align:center;color:var(--text-2)}
.empty h2{margin:0 0 8px;font-size:18px;color:var(--text)}
.empty p{margin:0;line-height:1.8}

.toast{position:fixed;left:50%;bottom:24px;z-index:20;transform:translate(-50%,12px);opacity:0;pointer-events:none;padding:8px 16px;border-radius:999px;background:var(--text);color:var(--panel);font-size:13px;font-weight:500;transition:opacity .18s,transform .18s}
.toast.show{opacity:1;transform:translate(-50%,0)}

/* ---- 狭い画面: 一覧 → 詳細 の 2 段 (戻るで一覧へ) */
@media (max-width:860px){
  .app{grid-template-columns:1fr}
  .side{border-right:0}
  body[data-view="list"] .detail{display:none}
  body[data-view="detail"] .side{display:none}
  .dwrap{padding:10px 16px 72px}
  .back{display:inline-flex;align-items:center;gap:2px;height:34px;padding:0 10px 0 4px;margin:0 0 6px -6px;border-radius:8px;color:var(--accent-ink);font-size:14px;font-weight:600}
  .back:hover{background:var(--hover)}
  .dt{font-size:19px}
  .props{grid-template-columns:1fr;gap:0}
  .props dt{margin-top:8px}
  .props dd{margin-top:2px}
  .ih time{margin-left:0;flex-basis:100%}
  .steps{padding:18px 4px 6px}
  .steps .sub{font-size:11px}
  .sfoot .keys{display:none}
}
@media (prefers-reduced-motion:reduce){*,*::before{animation:none!important;transition:none!important}}
</style>
</head>
<body data-view="list">
<div class="app">
  <aside class="side" aria-label="案件一覧">
    <header class="brand">
      <span class="logo" aria-hidden="true"><i class="claude"></i><i class="codex"></i></span>
      <div class="bt"><h1>寄合所</h1><p class="stamp" id="stamp"></p></div>
      <div class="acts">
        <button class="ib" id="reload" type="button" hidden></button>
        <button class="ib" id="theme" type="button"></button>
      </div>
    </header>
    <div class="notes" id="alerts" role="status"></div>
    <div class="tools">
      <label class="search"><span id="search-ic"></span><input id="q" type="search" placeholder="案件・要約・セッションを検索" autocomplete="off" aria-label="案件を検索"><kbd>/</kbd></label>
      <div class="scope" id="scope" role="tablist" aria-label="表示範囲"></div>
      <details class="filters" id="filters"><summary>絞り込み<span id="fcount"></span></summary><div class="fbody" id="fbody"></div></details>
    </div>
    <nav class="list" id="list" aria-label="案件"></nav>
    <footer class="sfoot" id="sfoot"></footer>
  </aside>
  <main class="detail" id="detail" tabindex="-1" aria-label="案件の詳細"></main>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script id="data" type="application/json">__PAYLOAD__</script>
<script>
(() => {
'use strict';
const DATA = JSON.parse(document.getElementById('data').textContent);
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// ---- 語彙 (状態の判定は JSON 側で確定済み。 ここは表示だけ)
const AG = Object.assign({claude:'Claude', codex:'Codex', human:'人', other:'他'}, DATA.labels || {});
const KIND = {request:'依頼', claim:'着手', submit:'提出', accept:'確認済み', revise:'差し戻し', release:'担当を返す', handover:'引き継ぎ',
  blocker:'相談', update:'更新', note:'状況メモ', finding:'発見', done:'完了', abandoned:'取り下げ'};
const KIND_G = {request:'todo', claim:'doing', submit:'review', accept:'done', revise:'attention', blocker:'attention', finding:'todo',
  done:'done', abandoned:'legacy', release:'legacy', handover:'legacy', update:'legacy', note:'legacy'};
const GROUPS = [
  {k:'attention', label:'要対応', hint:'相談・担当期限切れ・記録の不整合'},
  {k:'review', label:'確認待ち', hint:'提出済み。確認する相手の番'},
  {k:'todo', label:'引受待ち', hint:'担当の着手待ち'},
  {k:'doing', label:'作業中', hint:'担当が作業中・修正中'},
  {k:'legacy', label:'進行中の記録', hint:'提出・確認に分かれていない従来形式'},
  {k:'done', label:'完了', hint:'確認済み・取り下げ・完了'},
];
const GORDER = Object.fromEntries(GROUPS.map((g, i) => [g.k, i]));
const STEPS = ['依頼', '引受', '提出', '確認'];
const STEP_AT = {requested:1, stale:1, working:2, revision:2, blocked:2, submitted:3, accepted:4};
const IC = {
  search:'<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  reload:'<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
  system:'<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5a8.5 8.5 0 0 1 0 17z" fill="currentColor"/>',
  light:'<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
  dark:'<path d="M20.5 13.2A8.5 8.5 0 1 1 10.8 3.5a6.6 6.6 0 0 0 9.7 9.7z"/>',
  back:'<path d="m15 18-6-6 6-6"/>',
  copy:'<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
  check:'<path d="m5 12.5 4.5 4.5L19 7.5"/>',
};
const svg = k => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${IC[k]}</svg>`;

// ---- threads と、 セッション ID → 名前 (= 投稿時の --session-name) の対応
const threads = DATA.threads.map(t => ({...t, key: t.project + '/' + t.thread_id}));
const byKey = Object.fromEntries(threads.map(t => [t.key, t]));
const allProjects = [...new Set(threads.map(t => t.project))].sort();
const GENERIC = new Set(['', 'workflow', 'restricted-task']);
const NAMES = new Map();
for (const t of threads) for (const e of t.events) {
  const a = e.actor || {}; if (!a.session_id || GENERIC.has(a.task || '')) continue;
  const k = a.agent + ':' + a.session_id, p = NAMES.get(k);
  if (!p || (e.created_at || '') >= p.at) NAMES.set(k, {name: a.task, at: e.created_at || ''});
}
const isRole = sid => /^(role|resident)-/.test(sid || '');
const shortId = sid => !sid ? '' : /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(sid) ? sid.slice(0, 8) : sid.length > 28 ? sid.slice(0, 27) + '…' : sid;
const sessionKey = a => a ? a.agent + ':' + a.session_id : '';
function who(a){
  if (!a) return null;
  const role = isRole(a.session_id), n = NAMES.get(sessionKey(a))?.name || '';
  return {agent: a.agent, ag: AG[a.agent] || a.agent, name: role ? a.session_id : (n || shortId(a.session_id)), id: a.session_id || '', role};
}
const whoText = a => { const w = who(a); return w ? w.ag + (w.name ? ' · ' + w.name : '') : '—'; };
const avatar = agent => `<span class="av ${esc(agent)}" aria-hidden="true">${esc((AG[agent] || agent || '?')[0])}</span>`;
function whoHtml(a, copy = true){
  const w = who(a); if (!w) return '<span class="muted">—</span>';
  const inner = `${avatar(w.agent)}<span class="ag">${esc(w.ag)}</span>${w.name ? `<span class="sn">${esc(w.name)}</span>` : ''}${w.role ? '<span class="rl">役割</span>' : ''}`;
  return copy && w.id ? `<button type="button" class="who" data-copy="${esc(w.id)}" title="セッション ID ${esc(w.id)}（クリックでコピー）">${inner}</button>` : `<span class="who">${inner}</span>`;
}
const addresses = new Map();
threads.forEach(t => { const w = t.workflow; if (w) for (const a of [w.assignee, w.requester, w.reviewer]) if (a) addresses.set(sessionKey(a), a); });

// ---- 時刻
const rel = iso => { if (!iso) return ''; const d = (Date.now() - Date.parse(iso)) / 1000;
  if (d < 0) return 'これから'; if (d < 60) return 'たった今'; if (d < 3600) return Math.floor(d / 60) + '分前';
  if (d < 86400) return Math.floor(d / 3600) + '時間前'; if (d < 86400 * 14) return Math.floor(d / 86400) + '日前';
  return new Date(iso).toLocaleDateString('ja-JP', {month: 'numeric', day: 'numeric'}); };
const abs = iso => iso ? new Date(iso).toLocaleString('ja-JP', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : '';
const when = iso => { const d = (Date.now() - Date.parse(iso)) / 1000; return d >= 0 && d < 86400 ? `${abs(iso)} · ${rel(iso)}` : abs(iso); };
const remain = iso => { const s = (Date.parse(iso) - Date.now()) / 1000; if (s <= 0) return '期限切れ';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return h ? `残り ${h}時間${m}分` : `残り ${m}分`; };
const leasePct = e => { const a = Date.parse(e.created_at), b = Date.parse(e.lease_until); return Math.max(0, Math.min(1, (b - Date.now()) / (b - a))); };

// ---- 表題: 依頼文や要約の最初の一文 (全文は経過に残る)
// 文の終わり (。 / . ) は短くても区切る。 括弧・コロン等は 16 字未満なら区切らない (= 「Fable session (…」 を切らない)
const DELIM = /(。|．|\. |: |：| — | \(|（|; |；)/g;
function shortTitle(s, max = 96){
  s = String(s || '').replace(/\s+/g, ' ').trim(); if (!s) return '';
  DELIM.lastIndex = 0; let m;
  while ((m = DELIM.exec(s))) {
    if (m.index > max) break;
    if (m.index >= (/^(。|．|\. )$/.test(m[0]) ? 6 : 16)) return s.slice(0, m.index);
  }
  return s.length > max ? s.slice(0, max).trimEnd() + '…' : s;
}
function titleOf(t){
  if (t.project === 'restricted') return '非公開の案件 · ' + t.thread_id;
  if (t.workflow?.title) return shortTitle(t.workflow.title);
  return shortTitle(t.events.find(e => e.summary)?.summary) || t.thread_id;
}
const projLabel = p => p === 'restricted' ? '🔒 非公開' : p;
const tilde = p => String(p).replace(/^\/Users\/[^/]+(?=\/|$)/, '~');

// ---- 状態 → 一覧の区分
function groupOf(t){
  const w = t.workflow;
  if (w) return ({invalid:'attention', blocked:'attention', stale:'attention', submitted:'review', requested:'todo',
    working:'doing', revision:'doing', accepted:'done', abandoned:'done'})[w.status] || 'attention';
  if (t.closed) return 'done';
  if (t.open_blockers.length || t.stale_claims.length) return 'attention';
  if (t.active_claims.length) return 'doing';
  return 'legacy';
}
function statusLabel(t){
  if (t.workflow) return t.workflow.label;
  if (t.closed) return KIND[t.last_kind] || '完了';
  if (t.open_blockers.length) return '障害あり';
  if (t.stale_claims.length) return '担当期限切れ';
  if (t.active_claims.length) return '作業中';
  return '進行中';
}
function turnText(t){
  const w = t.workflow;
  if (w) return w.status === 'invalid' ? '記録の要確認' : w.waiting_on ? whoText(w.waiting_on) + ' の番' : w.label;
  const last = [...t.events].reverse().find(e => e._live);
  return last ? `${KIND[last.kind] || last.kind} · ${AG[last.actor.agent] || last.actor.agent}` : '';
}
const participants = t => [...new Set(t.events.map(e => e.actor.agent))];

// ---- 絞り込み
const state = {q: '', scope: 'open', agents: new Set(['claude', 'codex', 'human', 'other']), projects: new Set(allProjects),
  inbox: 'all', sel: null, view: 'list'};
const presentAgents = ['claude', 'codex', 'human', 'other'].filter(a => threads.some(t => participants(t).includes(a)));
function passes(t, {scope = true} = {}){
  if (scope && state.scope === 'open' && t.closed) return false;
  if (scope && state.scope === 'closed' && !t.closed) return false;
  if (state.inbox !== 'all' && t.workflow?.status !== 'invalid' && sessionKey(t.workflow?.waiting_on) !== state.inbox) return false;
  if (!state.projects.has(t.project)) return false;
  if (t.events.length && !participants(t).some(a => state.agents.has(a))) return false;
  if (state.q) {
    const q = state.q.toLowerCase();
    const hay = [t.project, t.thread_id, titleOf(t), t.workflow?.title, ...t.events.flatMap(e => [e.summary, e.details, e.actor.task, e.actor.session_id, ...(e.touches || [])])].join(' ').toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}
const visible = () => threads.filter(t => passes(t)).sort((a, b) => (GORDER[groupOf(a)] - GORDER[groupOf(b)]) || (b.last_at || '').localeCompare(a.last_at || ''));
const activeFilters = () => (presentAgents.some(a => !state.agents.has(a)) ? 1 : 0) + (state.projects.size < allProjects.length ? 1 : 0) + (state.inbox !== 'all' ? 1 : 0);

// ---- 左: 見出し・注意・道具・一覧
function renderBrand(){
  const demo = DATA.head === 'demo';
  $('#stamp').innerHTML = demo ? 'デモ · 架空の依頼' : `更新 ${esc(abs(DATA.generated_at))}（${esc(rel(DATA.generated_at))}）· ${esc(DATA.host || '')}`;
  $('#stamp').title = DATA.generated_at || '';
}
function renderAlerts(){
  const inv = DATA.invalid || [], warn = DATA.warnings || [], out = [];
  if (DATA.head === 'demo') out.push('<div class="note info">デモ表示です。すべて架空の依頼で、実際の掲示板は読んでいません。</div>');
  if (DATA.locked) out.push('<div class="note err">🔒 このマシンでは記録の暗号化が解除されていません（locked）。</div>');
  if (DATA.degraded) out.push('<div class="note err">記録に不整合があり、読み取れた分だけを表示しています。未処理の依頼がないという意味ではありません。</div>');
  if (inv.length) out.push(`<details class="note err" open><summary>読み取れない記録 ${inv.length} 件 — 関係する案件の状態は未確定です</summary><ul>${inv.map(x => `<li><span class="mono">${esc(x.path)}</span> — ${esc(x.why)}</li>`).join('')}</ul></details>`);
  if (warn.length) out.push(`<details class="note warn"><summary>過去の記録の互換性の注意 ${warn.length} 件</summary><ul>${warn.map(x => `<li><span class="mono">${esc(x.path)}</span> — ${esc(x.why)}</li>`).join('')}</ul></details>`);
  $('#alerts').innerHTML = out.join('');
}
function renderTools(){
  const base = threads.filter(t => passes(t, {scope: false}));
  const n = {open: base.filter(t => !t.closed).length, closed: base.filter(t => t.closed).length, all: base.length};
  $('#scope').innerHTML = [['open', '進行中'], ['closed', '完了'], ['all', 'すべて']].map(([k, l]) =>
    `<button type="button" role="tab" data-scope="${k}" aria-selected="${state.scope === k}">${l}<span class="n">${n[k]}</span></button>`).join('');
  const f = activeFilters();
  $('#fcount').innerHTML = f ? `<span class="fcount">${f}</span>` : '';
  $('#fbody').innerHTML = `
    <div><div class="fl">投稿者</div><div class="chips">${presentAgents.map(a => `<button class="chip" type="button" data-agent="${a}" aria-pressed="${state.agents.has(a)}">${avatar(a)}${esc(AG[a])}</button>`).join('')}</div></div>
    <div><div class="fl"><span>プロジェクト</span><small>⌥ クリックでそれだけ表示</small></div><div class="chips">${allProjects.map(p => `<button class="chip" type="button" data-project="${esc(p)}" aria-pressed="${state.projects.has(p)}">${esc(projLabel(p))}</button>`).join('')}</div></div>
    <div><label class="fl" for="session-filter">誰の番か</label><select class="fsel" id="session-filter">${[['all', 'すべてのセッション'], ...[...addresses].map(([k, a]) => [k, whoText(a)])].map(([k, l]) => `<option value="${esc(k)}" ${state.inbox === k ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></div>
    ${f ? '<button type="button" class="reset" data-reset>絞り込みを解除</button>' : ''}`;
}
function row(t){
  const g = groupOf(t), turn = turnText(t);
  return `<button type="button" class="row" data-go="${esc(t.key)}" aria-current="${state.sel === t.key}" title="${esc(t.thread_id)}">
    <span class="dot g-${g}" aria-hidden="true"></span>
    <span><span class="rt">${esc(titleOf(t))}</span><span class="rm"><span class="proj">${esc(projLabel(t.project))}</span>${turn ? `<span aria-hidden="true">·</span><span>${esc(turn)}</span>` : ''}</span></span>
    <time class="ra" datetime="${esc(t.last_at)}" title="${esc(abs(t.last_at))}">${esc(rel(t.last_at))}</time></button>`;
}
function renderList(){
  const vis = visible();
  if (!vis.length) { $('#list').innerHTML = `<p class="lempty">${state.q || activeFilters() ? '条件に合う案件はありません。' : state.scope === 'open' ? '進行中の案件はありません。' : '案件はありません。'}</p>`; return; }
  $('#list').innerHTML = GROUPS.map(g => { const items = vis.filter(t => groupOf(t) === g.k); if (!items.length) return '';
    return `<section class="grp-${g.k}" aria-label="${esc(g.label)}"><h2 class="gh g-${g.k}"><span class="dot" aria-hidden="true"></span>${esc(g.label)}<span class="gc">${items.length}</span><span class="gi">${esc(g.hint)}</span></h2>${items.map(row).join('')}</section>`; }).join('');
}
function renderFoot(){
  const total = threads.reduce((n, t) => n + t.events.length, 0), live = /^https?:$/.test(location.protocol);
  $('#sfoot').innerHTML = `<span>${threads.length} 案件 · ${total} 記録 · HEAD <span class="mono">${esc(DATA.head || '—')}</span> · ${esc(DATA.root || '')}</span>
    <span>${DATA.head === 'demo' ? '架空の見本です。' : live ? '再読み込みで最新の記録を取り込みます。' : '静的な書き出しです。最新にするには再生成してください。'}</span>
    <span class="keys"><span><kbd>j</kbd> <kbd>k</kbd> 移動</span><span><kbd>/</kbd> 検索</span><span><kbd>t</kbd> テーマ</span></span>`;
}

// ---- 右: 詳細
const pathItem = (p, kind) => `<li><button type="button" class="path" data-copy="${esc(p)}" title="クリックでパスをコピー">${kind ? `<span class="pk">${esc(kind)}</span>` : ''}<span class="pv">${esc(tilde(p))}</span>${svg('copy')}</button></li>`;
function workflowCard(t){
  const w = t.workflow, cur = STEP_AT[w.status];
  const steps = cur === undefined ? '' : `<ol class="steps" aria-label="進み具合">${STEPS.map((s, i) => { const st = i < cur ? 'done' : i === cur ? 'cur' : 'todo';
    return `<li class="${st}"${st === 'cur' ? ' aria-current="step"' : ''}><span class="node">${st === 'done' ? svg('check') : i + 1}</span><span class="sl">${s}</span>${st === 'cur' ? `<span class="sub">${esc(w.label)}</span>` : ''}</li>`; }).join('')}</ol>`;
  const turn = w.status === 'invalid'
    ? `<div class="turn bad"><div class="turn-h">記録に矛盾があり、状態を確定できません</div><p>下の経過と各記録の詳細を確認してください。</p></div>`
    : w.waiting_on
    ? `<div class="turn"><div class="turn-h"><span>次に動く人</span>${whoHtml(w.waiting_on)}</div>${w.next_action ? `<p>${esc(w.next_action)}</p>` : ''}</div>`
    : `<div class="turn fin"><div class="turn-h">${esc(w.label)}</div><p>${esc(w.next_action || 'この依頼は終了しました。')}</p></div>`;
  const leaseRow = w.lease_until && ['working', 'revision', 'blocked', 'stale'].includes(w.status)
    ? `<dt>担当期限</dt><dd>${esc(abs(w.lease_until))} まで · <span class="${w.claim_expired ? 'bad' : 'muted'}">${w.claim_expired ? '期限切れ' : esc(remain(w.lease_until))}</span></dd>` : '';
  const deliv = w.deliverables.length ? `<ul class="paths">${w.deliverables.map(p => pathItem(p)).join('')}</ul>` : '<p class="muted">まだ提出されていないか、場所を非公開にした案件です。</p>';
  return `<section class="card" aria-label="依頼の状態">${steps}${turn}
    <dl class="props"><dt>依頼元</dt><dd>${whoHtml(w.requester)}</dd><dt>担当</dt><dd>${whoHtml(w.assignee)}</dd><dt>確認する相手</dt><dd>${whoHtml(w.reviewer)}</dd>${leaseRow}</dl>
    <div class="sect"><h4>完了条件</h4><p>${esc(w.acceptance)}</p></div>
    <div class="sect"><h4>成果物</h4>${deliv}</div>
    ${w.errors.length ? `<div class="sect"><div class="note err">記録に矛盾があります。確認してください。${esc(w.errors.join('; '))}</div></div>` : ''}
  </section>`;
}
function exchange(t){
  const w = t.workflow;
  const items = [['question', '直近の質問'], ['answer', 'その回答'], ['submission', '直近の提出'], ['review', '直近の確認・差し戻し']].filter(([k]) => w[k]).map(([k, label]) => {
    const e = w[k], refs = [...(e.references || []), ...(e.deliverables || [])];
    const older = k === 'review' && w.submission && e.reply_to && e.reply_to !== w.submission.event_id;
    return `<li><div class="hx-h"><span class="hx-l">${label}</span>${whoHtml(e.actor, false)}<span>${esc(rel(e.created_at))}</span><button type="button" class="jump" data-jump="${esc(e.event_id)}">経過で見る ↓</button></div>
      <p>${esc(e.summary)}</p>${older ? '<div class="note warn">この確認・差し戻しは、最新の提出より前の提出に対するものです。</div>' : ''}${refs.length ? `<ul class="paths">${refs.map(p => pathItem(p)).join('')}</ul>` : ''}</li>`;
  }).join('');
  return items ? `<section class="card"><div class="card-h">直近のやりとり</div><ul class="hx">${items}</ul></section>` : '';
}
function feed(t){
  const items = t.events.map(e => {
    const dead = !e._live, unc = !e._committed, w = who(e.actor), c = e._commit;
    const label = e.kind === 'blocker' && !t.workflow ? '障害' : (KIND[e.kind] || e.kind);
    const refs = [...(e.deliverables || []).map(p => pathItem(p, '成果物')), ...(e.references || []).map(p => pathItem(p, '参照')),
      ...(e.touches || []).map(p => pathItem(p, '対象')), ...(e.promoted_to || []).map(p => pathItem(p, '反映先'))].join('');
    const lease = e.kind === 'claim' && e.lease_until && !dead && !t.closed ? (() => { const p = leasePct(e), alive = p > 0;
      return `<div class="lease"><div class="lease-l"><span>担当期限 ${esc(abs(e.lease_until))}</span><span>${esc(remain(e.lease_until))}</span></div><div class="bar ${!alive ? 'out' : p < .2 ? 'low' : ''}"><i style="width:${(alive ? p * 100 : 100).toFixed(1)}%"></i></div></div>`; })() : '';
    return `<li class="item${dead ? ' dead' : ''}" id="ev-${esc(e.event_id)}">${avatar(e.actor.agent)}<div class="ibody">
      <div class="ih"><span class="ag">${esc(w?.ag || '')}</span>${w?.name ? `<button type="button" class="sn" data-copy="${esc(w.id)}" title="セッション ID ${esc(w.id)}（クリックでコピー）">${esc(w.name)}</button>` : ''}<span class="kind g-${KIND_G[e.kind] || 'legacy'}">${esc(label)}</span>${dead ? '<span class="flag">取り消し済み</span>' : ''}${unc ? '<span class="flag">未コミット</span>' : ''}<time datetime="${esc(e.created_at)}" title="${esc(e.created_at)}">${esc(when(e.created_at))}</time></div>
      <p class="isum">${esc(e.summary)}</p>
      ${e._role_notice ? `<p class="rn">${esc(e._role_notice)}</p>` : ''}
      ${e.details ? `<details class="more"><summary>詳細を読む</summary><p>${esc(e.details)}</p></details>` : ''}
      ${refs ? `<ul class="paths">${refs}</ul>` : ''}${lease}
      ${(e.supersedes || []).length ? `<p class="sup">取り消した記録: ${e.supersedes.map(id => `<button type="button" class="jump" data-jump="${esc(id)}">${esc(id)}</button>`).join(' ')}</p>` : ''}
      <details class="rec"><summary>記録の詳細</summary><dl><dt>ID</dt><dd class="mono">${esc(e.event_id)}</dd>${e.reply_to ? `<dt>返信先</dt><dd class="mono">${esc(e.reply_to)}</dd>` : ''}${c ? `<dt>commit</dt><dd class="mono">${esc(c.sha)} · ${esc(c.author)} · ${esc(abs(c.date))}</dd>` : ''}${e.actor.instance ? `<dt>実行環境</dt><dd class="mono">${esc(e.actor.instance)}</dd>` : ''}<dt>ファイル</dt><dd class="mono">${esc(e._path)}</dd></dl></details>
    </div></li>`; }).join('');
  return `<section class="feedsec" aria-label="経過"><h3 class="feed-h">経過<small>${t.events.length} 件 · 古い順</small></h3><ol class="feed">${items}</ol></section>`;
}
function renderDetail(){
  const box = $('#detail'), t = byKey[state.sel];
  if (!threads.length) { box.innerHTML = `<div class="dwrap"><div class="empty">${(DATA.invalid || []).length
    ? '<h2>記録を確認できません</h2><p>左上の注意を確認してください。未処理の依頼がないという意味ではありません。</p>'
    : '<h2>まだ依頼がありません</h2><p>依頼元と担当のセッション、完了条件を指定して依頼を作ると、ここで提出と受領確認まで追えます。</p>'}</div></div>`; return; }
  if (!t) { box.innerHTML = '<div class="dwrap"><div class="empty"><h2>案件を選んでください</h2><p>左の一覧から選びます。<kbd>j</kbd> <kbd>k</kbd> でも移動できます。</p></div></div>'; return; }
  const repo = t.events.find(e => e.project?.repo)?.project.repo, dead = t.events.filter(e => !e._live).length;
  const sl = statusLabel(t);
  const badges = [`<span class="badge g-${groupOf(t)}">${esc(sl)}</span>`,
    t.open_blockers.length && sl !== '障害あり' ? `<span class="badge g-attention">障害 ${t.open_blockers.length}</span>` : '',
    t.stale_claims.length && sl !== '担当期限切れ' ? `<span class="badge g-review">担当期限切れ ${t.stale_claims.length}</span>` : '',
    t.unpromoted.length ? `<span class="badge g-todo">未反映の発見 ${t.unpromoted.length}</span>` : '',
    t.project === 'restricted' ? '<span class="badge plain">🔒 メタデータのみ</span>' : ''].join('');
  box.innerHTML = `<div class="dwrap"><header>
      <button type="button" class="back" data-back>${svg('back')}一覧</button>
      <div class="crumb"><span>${esc(projLabel(t.project))}</span>${repo ? `<span class="sep">/</span><span>${esc(repo)}</span>` : ''}<span class="sep">/</span><span class="mono">${esc(t.thread_id)}</span></div>
      <h2 class="dt">${esc(titleOf(t))}</h2>
      <div class="badges">${badges}<span class="metaline">${t.events.length} 件の記録${dead ? `（取り消し済み ${dead}）` : ''} · 最終更新 ${esc(rel(t.last_at))}</span></div>
    </header>
    ${t.workflow ? workflowCard(t) + exchange(t) : '<div class="note plain" style="margin-top:20px">従来形式の作業記録です。提出と確認を分ける新しい依頼は、別のスレッドで始めます。</div>'}
    ${feed(t)}</div>`;
}

// ---- 操作
const narrow = () => matchMedia('(max-width: 860px)').matches;
const smooth = () => matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
const keyFromHash = () => { try { const k = decodeURIComponent(location.hash.slice(1)); return byKey[k] ? k : null; } catch (_) { return null; } };
let pushed = false;
function applyView(){ document.body.dataset.view = state.view; }
function select(key){
  state.sel = key;
  const url = '#' + encodeURIComponent(key);
  if (narrow() && state.view !== 'detail') { history.pushState({detail: true}, '', url); pushed = true; }
  else history.replaceState(history.state, '', url);
  state.view = 'detail';
  renderList(); renderDetail(); applyView();
  $('#detail').scrollTop = 0;
  document.querySelector('.row[aria-current="true"]')?.scrollIntoView({block: 'nearest'});
}
function back(){
  if (pushed) { pushed = false; history.back(); return; }
  history.replaceState(null, '', location.pathname + location.search);
  state.view = 'list'; applyView();
}
window.addEventListener('popstate', () => { const k = keyFromHash(); pushed = false;
  if (k) { state.sel = k; state.view = 'detail'; } else state.view = 'list';
  renderList(); renderDetail(); applyView(); });
function move(d){
  const vis = visible(); if (!vis.length) return;
  const i = vis.findIndex(t => t.key === state.sel);
  select(vis[i < 0 ? 0 : Math.max(0, Math.min(vis.length - 1, i + d))].key);
}
function jump(id){
  const el = document.getElementById('ev-' + id);
  if (!el) { toast('この記録は表示されていません'); return; }
  el.scrollIntoView({block: 'center', behavior: smooth()});
  el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
}
let toastTimer;
function toast(msg){ const el = $('#toast'); el.textContent = msg; el.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('show'), 1600); }
async function copy(text){
  try { await navigator.clipboard.writeText(text); toast('コピーしました'); return; } catch (_) {}
  const ta = document.createElement('textarea'); ta.value = text; ta.style.cssText = 'position:fixed;opacity:0'; document.body.append(ta); ta.select();
  const ok = document.execCommand('copy'); ta.remove(); toast(ok ? 'コピーしました' : 'コピーできませんでした');
}
document.addEventListener('click', e => {
  const cp = e.target.closest('[data-copy]'); if (cp) { copy(cp.dataset.copy); return; }
  const go = e.target.closest('[data-go]'); if (go) { select(go.dataset.go); return; }
  const jp = e.target.closest('[data-jump]'); if (jp) { jump(jp.dataset.jump); return; }
  if (e.target.closest('[data-back]')) { back(); return; }
  const sc = e.target.closest('[data-scope]'); if (sc) { state.scope = sc.dataset.scope; renderTools(); renderList(); return; }
  const ag = e.target.closest('[data-agent]'); if (ag) { const a = ag.dataset.agent; state.agents.has(a) ? state.agents.delete(a) : state.agents.add(a); renderTools(); renderList(); return; }
  const pj = e.target.closest('[data-project]'); if (pj) { const p = pj.dataset.project;
    if (e.altKey || e.metaKey) state.projects = new Set([p]); else state.projects.has(p) ? state.projects.delete(p) : state.projects.add(p);
    renderTools(); renderList(); return; }
  if (e.target.closest('[data-reset]')) { state.agents = new Set(['claude', 'codex', 'human', 'other']); state.projects = new Set(allProjects); state.inbox = 'all'; renderTools(); renderList(); }
});
document.addEventListener('change', e => { if (e.target.id === 'session-filter') { state.inbox = e.target.value; renderTools(); renderList(); } });
$('#q').addEventListener('input', e => { state.q = e.target.value.trim(); renderTools(); renderList(); });
document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target.matches?.('input,textarea,select')) {
    if (e.key === 'Escape') e.target.blur();
    if (e.key === 'Enter' && e.target.id === 'q') { const v = visible(); if (v.length) select(v[0].key); }
    return;
  }
  if (e.key === '/') { e.preventDefault(); if (narrow() && state.view === 'detail') back(); $('#q').focus(); return; }
  if (e.key === 't') { cycleTheme(); return; }
  if (e.key === 'Escape' && narrow() && state.view === 'detail') { back(); return; }
  if (e.key === 'j' || e.key === 'k') move(e.key === 'j' ? 1 : -1);
});

// ---- テーマ (システム / ライト / ダーク) と再読み込み
const THEMES = ['system', 'light', 'dark'], TLABEL = {system: 'システムに合わせる', light: 'ライト', dark: 'ダーク'};
let theme = 'system'; try { theme = localStorage.getItem('board-theme') || 'system'; } catch (_) {}
if (!THEMES.includes(theme)) theme = 'system';
function applyTheme(){
  const r = document.documentElement; if (theme === 'system') r.removeAttribute('data-theme'); else r.setAttribute('data-theme', theme);
  const b = $('#theme'); b.innerHTML = svg(theme); b.title = `テーマ: ${TLABEL[theme]}（t で切り替え）`; b.setAttribute('aria-label', b.title);
  try { localStorage.setItem('board-theme', theme); } catch (_) {}
}
function cycleTheme(){ theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length]; applyTheme(); }
$('#theme').addEventListener('click', cycleTheme);
if (/^https?:$/.test(location.protocol) && DATA.head !== 'demo') {
  const rb = $('#reload'); rb.hidden = false; rb.innerHTML = svg('reload'); rb.title = '最新の記録を読み込む'; rb.setAttribute('aria-label', rb.title);
  rb.addEventListener('click', () => { rb.classList.add('spin'); location.reload(); });
}
$('#search-ic').outerHTML = svg('search');

// ---- 起動
const initial = keyFromHash();
state.sel = initial || visible()[0]?.key || null;
state.view = initial ? 'detail' : 'list';
applyTheme(); renderBrand(); renderAlerts(); renderTools(); renderList(); renderDetail(); renderFoot(); applyView();
document.querySelector('.row[aria-current="true"]')?.scrollIntoView({block: 'nearest'});
setInterval(() => { renderBrand(); renderList(); }, 60000);
})();
</script>
</body>
</html>
"""


def build(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return HTML.replace("__PAYLOAD__", data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, help="default: ~/.cache/agent-board/<board>.html")
    ap.add_argument("--project")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--demo", action="store_true", help="架空 event で見本を生成 (実 repo は読まない)")
    ap.add_argument("--open", action="store_true", help="生成後に既定ブラウザで開く")
    ap.add_argument("--root", type=Path, help="the board directory (holds board.json and events/)")
    ap.add_argument("--board", help="a board in the workspace by name")
    a = ap.parse_args(argv)
    if a.demo:
        payload = demo_payload()
        payload["root"] = "(demo fixture — 実 board ではない)"
        a.out = a.out or CACHE / "demo.html"
    else:
        import board_config as bc
        a.root = bc.resolve(a.root, a.board)
        cfg = None if bc.is_locked(a.root) else bc.load(a.root)
        a.out = a.out or CACHE / (bc.name_of(a.root, cfg) + ".html")
        if a.sync:
            from board import snapshot
            with snapshot(a.root) as remote_root:
                payload = payload_from(remote_root, a.project)
        else:
            payload = payload_from(a.root, a.project)
        payload["root"] = str(a.root).replace(str(Path.home()), "~")
        payload["labels"] = (cfg or {}).get("labels", {})
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(build(payload), encoding="utf-8")
    n = sum(len(t["events"]) for t in payload["threads"])
    print(f"{a.out}  ({len(payload['threads'])} threads / {n} events{' / demo' if a.demo else ''}"
          f"{' / 🔒 locked' if payload.get('locked') else ''})")
    if a.open:
        import webbrowser
        webbrowser.open(Path(a.out).resolve().as_uri())        # macOS / Linux / Windows の既定ブラウザ (`open` は macOS だけ)
    return 0


if __name__ == "__main__":
    sys.exit(main())
