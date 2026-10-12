#!/usr/bin/env python3
"""board-view.py — 掲示板の派生 view (read-only) + event 検証 + 投稿 template.

**ローカル read-only 実装** (投稿は board.py)。 v2 は session ID 宛ての依頼・提出・受領、
v1 は従来の peer-status。 手順は CONTRACT.md。 どの掲示板か = --root / --board / --all-boards / AGENT_BOARD_ROOT。

## 述語 (= thread の現在状態の導出)

- event の順序 = `git log --reverse --diff-filter=A -- events/` (= main 上の commit 順)。
  `created_at` は表示用で順序には使わない (DESIGN.md「Storage model」)。 掲示板が repo の
  subdirectory (`<project>/board/`) でも同じ (path は掲示板の directory からの相対に直す)。 未 commit の
  event file は末尾に「(uncommitted)」 marker 付きで加える。
- superseded = 全 event の `supersedes` の和集合。 superseded された event は live でない。
- v1 の状態 = live event の最後の kind。v2 は board_workflow.py が導出する。
- active claim = open thread 内の live `claim` で `lease_until` > now。 lease 切れは stale。
- open blocker = open thread 内の live `blocker`。
- unpromoted finding = open thread 内の live `finding` で `promoted_to` が空
  (= まだ provisional、 元 project へ昇格していない)。

## mode

- (既定)      全 thread の派生 view。
- --surface   active claim / stale claim / open blocker / unpromoted finding /
              uncommitted / invalid / locked のみ。 **該当 0 件なら silent** (dashboard 統合用)。
- --project K  project.key で filter。
- --all-boards workspace の全掲示板 (`board.py boards`) をまとめる。 --json では thread に `board` が付く。
- --validate  全 event を厳密な schema と protocol で検証 (外部 dependency 不要)
              + filename / path / restricted 整合。 不整合があれば exit 1。
- --template KIND --project K --thread T [--task ..] [--agent claude|codex] [--repo o/n]
              [--restricted] [--write]
              schema 準拠の event JSON を生成 (event_id / created_at / lease_until 自動)。
              --write で `events/<K>/<T>/<ts>--<id>.json` に書く (git 操作はしない)。
- --selftest  temp git repo に fixture を積んで述語を検証。

## 限界 (declared)

- 読取専用。 push / rebase / 競合解決はしない。
- source-classification gate は **人間 / 起票 agent の判断** (= --template は
  --restricted を渡された時だけ restricted 形にする。 gate を自動判定しない)。
- git-crypt locked (= board.json が \\x00GITCRYPT header) の時は「🔒 locked」 を出して終了。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from board_workflow import reduce_workflow, LABELS
from board_schema import check_schema, errors as schema_errors, history_errors
import board_config as bc
import board_transport as bt

ENGINE = Path(__file__).resolve().parent
SCHEMA_PATH = ENGINE / "schema" / "event.schema.json"
CLOSED = {"done", "abandoned"}
GITCRYPT_MAGIC = bc.GITCRYPT_MAGIC
RESTRICTED_SUMMARY = {
    "claim": "Work claimed.",
    "update": "Work updated.",
    "finding": "Finding recorded in source.",
    "blocker": "Work blocked.",
    "done": "Work completed.",
    "abandoned": "Work abandoned.",
    "note": "Status noted.",
}
FILENAME_RE = re.compile(r"^(\d{8}T\d{6}Z)--(.+)\.json$")


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso_z(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compact(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def is_locked(root: Path) -> bool:
    return bc.is_locked(root)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    ).stdout


COMMIT_META: dict[Path, dict] = {}   # path -> {sha, date, author} (transport info、 表示用)


def _head(root: Path) -> str:
    """The commit a view was read from: the checkout's HEAD, or the fetched commit of a materialised board."""
    try:
        return (root / bt.BASE_FILE).read_text(encoding="utf-8").strip()[:7]
    except OSError:
        return git(root, "rev-parse", "--short", "HEAD").strip()


def event_paths(root: Path) -> list[tuple[Path, bool]]:
    """(path, committed) を main 上の追加順で返す。 未 commit は末尾。"""
    order = root / bt.ORDER_FILE
    if order.is_file():
        # a board materialised by board_transport (a synchronized read): no .git here, the branch order and the
        # commit meta were computed from the fetched commit. A path the log added but the tree lacks is listed
        # and fails to open below = damaged history, as with a checkout.
        out: list[tuple[Path, bool]] = []
        for line in order.read_text(encoding="utf-8").splitlines():
            sha, date, author, rel = (line.split("|", 3) + ["", "", "", ""])[:4]
            if not rel: continue
            p = root / rel
            if sha: COMMIT_META[p] = {"sha": sha, "date": date, "author": author}
            out.append((p, True))
        return out
    seen: list[Path] = []
    # git prints paths from the repository top; a board in `<repo>/board/` strips that prefix
    prefix = git(root, "rev-parse", "--show-prefix").strip()
    # sentinel は平文 prefix (= \x1e/\x1f は str.strip() が空白扱いで削る。 path は必ず events/ で始まるので衝突しない)
    log = git(root, "log", "--reverse", "--diff-filter=A", "--name-only",
              "--format=COMMIT|%H|%cI|%an", "HEAD", "--", "events")
    cur: dict | None = None
    for line in log.splitlines():
        line = line.strip()
        if line.startswith("COMMIT|"):
            sha, date, author = (line.split("|", 3)[1:] + ["", ""])[:3]
            cur = {"sha": sha[:10], "date": date, "author": author}
            continue
        if not line.endswith(".json") or not line.startswith(prefix):
            continue
        p = root / line[len(prefix):]
        if p not in seen:
            seen.append(p)
            if cur:
                COMMIT_META[p] = cur
    # A deleted historical record is a damaged history, not an absent event.
    out = [(p, True) for p in seen]
    status = git(root, "status", "--porcelain", "--untracked-files=all", "--", "events")
    for line in status.splitlines():
        rel = line[3:].strip()
        if not rel.endswith(".json") or not rel.startswith(prefix):
            continue
        p = root / rel[len(prefix):]
        if p in seen:
            out = [(old, False if old == p else committed) for old, committed in out]
        elif p.is_file():
            out.append((p, False))
    return out


class EventIssue:
    """A rejected record, with conservative routing for fault isolation."""
    def __init__(self, root, path, why, data=None):
        self.path, self.why = path, why
        self.keys = set()
        self.event_ids = set()
        filename = FILENAME_RE.fullmatch(path.name)
        if filename:
            self.event_ids.add(filename.group(2))
        if isinstance(data, dict) and isinstance(data.get('event_id'), str):
            self.event_ids.add(data['event_id'])
        parts = path.relative_to(root / 'events').parts
        self.unscoped = not (len(parts) == 3
            and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,63}', parts[0])
            and re.fullmatch(r'[a-z0-9][a-z0-9-]{2,95}', parts[1]))
        if not self.unscoped:
            self.keys.add((parts[0], parts[1]))
        # On a path/body mismatch, neither claimed thread may be called healthy.
        if isinstance(data, dict) and isinstance(data.get('project'), dict):
            pk, tid = data['project'].get('key'), data.get('thread_id')
            if (isinstance(pk, str) and isinstance(tid, str)
                    and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,63}', pk)
                    and re.fullmatch(r'[a-z0-9][a-z0-9-]{2,95}', tid)):
                self.keys.add((pk, tid))

    def __iter__(self):
        return iter((self.path, self.why))


class EventList(list):
    """Carry rejected-record diagnostics through all state derivation paths."""
    def __init__(self):
        super().__init__()
        self.invalid = []


def history_warnings(events):
    return [(e['_path'], w) for e in events for w in e.get('_warnings', [])]


def path_errors(root, p, data):
    problems = []
    m = FILENAME_RE.fullmatch(p.name)
    if not m or m.group(2) != data['event_id']:
        problems.append('filename must match <UTC-compact>--<event-id>.json')
    parts = p.relative_to(root / 'events').parts
    if len(parts) != 3 or tuple(parts[:2]) != (data['project']['key'], data['thread_id']):
        problems.append('event path does not match project.key/thread_id')
    return problems


def load_events(root: Path):
    events = EventList()
    invalid = events.invalid
    schema = json.loads(SCHEMA_PATH.read_text())
    check_schema(schema)
    for p, committed in event_paths(root):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            # Do not echo unvalidated payload fragments into logs or the UI.
            invalid.append(EventIssue(root, p, f"JSON parse error ({type(e).__name__})"))
            continue
        if not isinstance(data, dict):
            invalid.append(EventIssue(root, p, "top-level is not an object"))
            continue
        errors, warnings = history_errors(data, schema, committed=committed)
        if errors:
            invalid.append(EventIssue(root, p, "schema: " + "; ".join(errors), data))
            continue
        errors = path_errors(root, p, data)
        if errors:
            invalid.append(EventIssue(root, p, "; ".join(errors), data))
            continue
        data["_path"] = p
        data["_committed"] = committed
        data["_commit"] = COMMIT_META.get(p)
        data["_warnings"] = warnings
        events.append(data)
    by_id = {}
    for e in events:
        by_id.setdefault(e['event_id'], []).append(e)
    duplicates = {eid for eid, records in by_id.items() if len(records) > 1}
    for e in events:
        if e['event_id'] in duplicates:
            invalid.append(EventIssue(root, e['_path'], 'duplicate event_id', e))
    events[:] = [e for e in events if e['event_id'] not in duplicates]
    return events, invalid


def to_json_payload(root: Path, threads: dict, invalid, *, now: dt.datetime, locked: bool, warnings=(), board=None) -> dict:
    """--json: viewer (board-html.py) 向けの派生 state。 述語は derive() と同一 (= 二重実装しない)。"""
    def pub(ev: dict) -> dict:
        body = {k: v for k, v in ev.items() if not k.startswith("_")}
        body["_path"] = str(ev["_path"].relative_to(root))
        body["_committed"] = ev.get("_committed", True)
        body["_commit"] = ev.get("_commit")
        body["_role_notice"] = role_claim_notice(ev)
        return body

    out_threads = []
    for (pk, tid), t in sorted(threads.items()):
        live_ids = {e.get("event_id") for e in t["live"]}
        out_threads.append({
            **({"board": board} if board else {}),
            "project": pk,
            "thread_id": tid,
            "closed": t["closed"],
            "workflow": t.get("workflow"),
            "last_kind": (t["last"] or {}).get("kind"),
            "last_at": (t["last"] or {}).get("created_at"),
            "active_claims": [e.get("event_id") for e in t["active_claims"]],
            "stale_claims": [e.get("event_id") for e in t["stale_claims"]],
            "open_blockers": [e.get("event_id") for e in t["open_blockers"]],
            "unpromoted": [e.get("event_id") for e in t["unpromoted"]],
            "events": [dict(pub(e), _live=e.get("event_id") in live_ids) for e in t["events"]],
        })
    return {
        **({"board": board} if board else {}),
        "generated_at": iso_z(now),
        "host": socket.gethostname().split(".")[0],
        "root": str(root).replace(str(Path.home()), "~"),
        "head": _head(root),
        "locked": locked,
        "invalid": [{"path": str(p.relative_to(root)), "why": why} for p, why in invalid],
        "warnings": [{"path": str(p.relative_to(root)), "why": why} for p, why in warnings],
        "degraded": bool(invalid) or any((t.get('workflow') or {}).get('errors') for t in threads.values()),
        "threads": out_threads,
    }


def event_brief(event):
    """Only event-owned content, without filesystem/private loader metadata."""
    if event is None:
        return None
    return {key: event.get(key) for key in
            ('event_id', 'kind', 'created_at', 'actor', 'summary', 'references', 'deliverables', 'reply_to')}


def workflow_row(project, thread, state, *, reader=None):
    """One read contract shared by inbox and explicit request inspection."""
    w = state.get('workflow') or {}
    row = {key: w.get(key) for key in ('status', 'label', 'waiting_on', 'next_action',
        'request_id', 'reply_to', 'title', 'acceptance', 'requester', 'reviewer', 'assignee',
        'submission', 'question', 'answer', 'review', 'claim_expired', 'lease_until')}
    row.update(project=project, thread=thread,
        actionable=bool(reader and w.get('waiting_on') == reader and not w.get('errors')),
        deliverables=w.get('deliverables') or [],
        references=(w.get('request') or {}).get('references') or [],
        errors=w.get('errors') or [])
    return row


def participated(state, reader):
    """Exact session participation, including previous handover owners."""
    for event in state['events']:
        for role in ('actor', 'assignee', 'target'):
            value = event.get(role) or {}
            if {key: value.get(key) for key in ('agent', 'session_id')} == reader:
                return True
    return False


def handoff_lines(w):
    """Render the actual handoff messages, not just the original request title."""
    lines = []
    if w.get('acceptance'):
        lines.append('    完了条件: ' + w['acceptance'])
    for reference in (w.get('request') or {}).get('references') or []:
        lines.append('    依頼資料: ' + reference)
    for key, label in [('question', '直近の質問'), ('answer', 'その回答'),
                       ('submission', '直近の提出'), ('review', '直近の確認・差し戻し')]:
        e = w.get(key)
        if e:
            actor = e.get('actor') or {}
            lines.append(f"    {label} [{e['event_id']}; reply_to={e.get('reply_to') or '—'}] {actor.get('agent')} / {actor.get('session_id')}: {e.get('summary') or ''}")
            for reference in (e.get('references') or []) + (e.get('deliverables') or []):
                lines.append('      → ' + reference)
    return lines


def stats_text(events, now: dt.datetime, name: str = "") -> str:
    """依頼の滞留の表 (純関数): 閉じた依頼の request→claim / claim→submit / submit→accept の時間の分布と、 開いている依頼を
    今の状態での経過が長い順に。 止まっている依頼 (役割 id 宛てで誰も開かない・検収役が居ない) が一目で分かるための read-only の view。"""
    from board_workflow import reduce_workflow, LABELS
    threads: dict = {}
    for e in events:
        threads.setdefault((e["project"]["key"], e["thread_id"]), []).append(e)
    spans = {"request→claim": [], "claim→submit": [], "submit→accept": [], "request→closed": []}
    open_rows = []
    for key, th in threads.items():
        req = next((e for e in th if e.get("kind") == "request"), None)
        if not req: continue
        w = reduce_workflow(th, now)
        t0 = parse_ts(req["created_at"])
        claims = [e for e in th if e.get("kind") == "claim"]; subs = [e for e in th if e.get("kind") == "submit"]
        closes = [e for e in th if e.get("kind") in ("accept", "abandoned")]
        h = lambda a, b: (parse_ts(b["created_at"]) - parse_ts(a["created_at"])).total_seconds() / 3600
        if claims: spans["request→claim"].append(h(req, claims[0]))
        if claims and subs: spans["claim→submit"].append(h(claims[0], subs[-1]))
        if subs and closes: spans["submit→accept"].append(h(subs[-1], closes[-1]))
        if closes: spans["request→closed"].append(h(req, closes[-1]))
        if w.get("status") not in ("accepted", "abandoned"):
            who = w.get("waiting_on") or {}
            age = (now - parse_ts(th[-1]["created_at"])).total_seconds() / 86400
            open_rows.append((age, f"{key[0]}/{key[1]}", LABELS.get(w.get("status"), w.get("status")), f"{who.get('agent')}/{who.get('session_id')}"))
    out = [f"📊 {name or 'board'}: 依頼 {sum(1 for th in threads.values() if any(e.get('kind') == 'request' for e in th))} 件 (閉じた {len(spans['request→closed'])} / 開いている {len(open_rows)})"]
    out.append("  区間            n   中央値    p90     最大  (時間)")
    for k, vals in spans.items():
        if not vals: out.append(f"  {k:14s}  0"); continue
        s = sorted(vals); med = s[len(s) // 2]; p90 = s[min(len(s) - 1, int(len(s) * 0.9))]
        out.append(f"  {k:14s} {len(s):3d} {med:8.1f} {p90:7.1f} {s[-1]:8.1f}")
    for age, key, label, who in sorted(open_rows, key=lambda r: -r[0]):
        out.append(f"  ⏳ {age:5.1f} 日 [{label}] {key} → {who}")
    return "\n".join(out)


def derive(events: list[dict], now: dt.datetime) -> dict:
    """thread key -> state dict。"""
    threads: dict[tuple[str, str], dict] = {}
    for ev in events:
        key = (str((ev.get("project") or {}).get("key")), str(ev.get("thread_id")))
        t = threads.setdefault(key, {"events": [], "live": []})
        t["events"].append(ev)
    for key, t in threads.items():
        superseded = {eid for ev in t['events'] for eid in ev.get('supersedes', [])}
        t['live'] = [ev for ev in t['events'] if ev['event_id'] not in superseded]
        if any(e.get("schema_version") == 2 for e in t["events"]):
            w = reduce_workflow(t["events"], now)
            if w["request"] is None and not w["errors"]:
                # v2 notes only (no request yet): no workflow state; render as an open thread below
                t["live"] = [e for e in t["events"] if e.get("event_id") in w["accepted_ids"]]
            else:
                t["live"] = [e for e in t["events"] if e.get("event_id") in w["accepted_ids"]]
                t["last"] = t["live"][-1] if t["live"] else None
                t["closed"] = w["status"] in {"accepted", "abandoned"}
                t["active_claims"], t["stale_claims"] = [], []
                if w["claim"] and w["status"] in {"working", "revision", "blocked", "stale"}:
                    t["stale_claims" if w['claim_expired'] else "active_claims"].append(w["claim"])
                t["open_blockers"] = [w["blocker"]] if w["blocker"] and not t["closed"] else []
                t["unpromoted"] = [e for e in t["live"] if e["kind"] == "finding" and not e.get("promoted_to")] if not t["closed"] else []
                t["workflow"] = {k: w[k] for k in ("status", "waiting_on", "next_action", "errors")}
                t["workflow"].update(label=LABELS[w["status"]], request_id=(w["request"] or {}).get("event_id"),
                    title=(w["request"] or {}).get("summary"), acceptance=(w["request"] or {}).get("acceptance"),
                    assignee=w["assigned_to"],
                    requester={k:(w["request"] or {}).get("actor", {}).get(k) for k in ("agent", "session_id")},
                    reviewer=w["reviewer"],
                    claim_expired=w['claim_expired'], lease_until=(w['claim'] or {}).get('lease_until'),
                    deliverables=(w["submission"] or {}).get("deliverables", []),
                    request=event_brief(w['request']), submission=event_brief(w['submission']),
                    question=event_brief(w['question']), answer=event_brief(w['answer']), review=event_brief(w['review']),
                    reply_to=(w["submission"] if w["status"] == "submitted" else w["blocker"] if w["status"] == "blocked" else w["claim"] or w["request"] or {}).get("event_id"))
                if t['closed']:
                    t['workflow']['reply_to'] = None
                continue
        live = t["live"]
        last = live[-1] if live else None
        t["last"] = last
        t["closed"] = bool(last) and last.get("kind") in CLOSED
        t["active_claims"], t["stale_claims"] = [], []
        t["open_blockers"], t["unpromoted"] = [], []
        if t["closed"]:
            continue
        for ev in live:
            k = ev.get("kind")
            if k == "claim":
                try:
                    ok = parse_ts(ev.get("lease_until") or "") > now
                except ValueError:
                    ok = False
                (t["active_claims"] if ok else t["stale_claims"]).append(ev)
            elif k == "blocker":
                t["open_blockers"].append(ev)
            elif k == "finding" and not (ev.get("promoted_to") or []):
                t["unpromoted"].append(ev)
    damaged = {}
    for issue in getattr(events, 'invalid', []):
        keys = set(threads) | issue.keys if issue.unscoped else issue.keys
        for key in keys:
            damaged.setdefault(key, []).append(f'{issue.path.name}: {issue.why}')
    for key, t in threads.items():
        if (t.get('workflow') or {}).get('errors'):
            damaged.setdefault(key, []).extend(t['workflow']['errors'])
    for key, problems in damaged.items():
        t = threads.setdefault(key, {'events': [], 'live': [], 'last': None})
        t.update(closed=False, active_claims=[], stale_claims=[], open_blockers=[], unpromoted=[])
        w = t.setdefault('workflow', {})
        w.update(status='invalid', label=LABELS['invalid'], waiting_on=None, reply_to=None,
                 next_action='記録の不整合を確認する。状態の確定・投稿は保留。', errors=problems)
        for name in ('request_id', 'title', 'acceptance', 'assignee', 'requester', 'reviewer', 'reply_to'):
            w.setdefault(name, None)
        w.setdefault('deliverables', [])
    return threads


def assert_writable(events, project, thread, event_id=None):
    """Block damaged target threads; unknown-scope damage blocks every write."""
    if any(issue.unscoped for issue in getattr(events, 'invalid', [])):
        raise ValueError('unscoped invalid history: cannot safely select a writable thread')
    if any(event_id in issue.event_ids for issue in getattr(events, 'invalid', [])):
        raise ValueError('event_id already occurs in invalid history')
    state = derive(events, now_utc()).get((project, thread), {})
    problems = (state.get('workflow') or {}).get('errors')
    if problems:
        raise ValueError('target thread has invalid history: ' + '; '.join(problems[:3]))


def actor_str(ev: dict) -> str:
    a = ev.get("actor") or {}
    inst = a.get("instance")
    return inst or str(a.get("agent"))


# A status event that describes who owns / reviews / must not touch something is that session's own
# statement, not an owner assignment (CLAUDE.md#role-claim-is-not-assignment). Reading such a note is
# where the claim gets copied into project records as an agreement, so the project view says so there.
# Calibrated on all history: fires only on role/scope self-claims, their copies and their correction.
ROLE_CLAIM_RE = re.compile(
    r"窓口|担当範囲|を担当する|のみを担当|編集しない|触らない|触る前に"
    r"|\b(?:review window|owns|in charge of|responsible for)\b"
    r"|\b(?:won't|will not|do not|don't) (?:edit|touch)\b", re.I)
ROLE_CLAIM_NOTICE = ("↳ 役割・担当の自己申告は、その session の発言で owner の割り当てではない "
                     "(CLAUDE.md#role-claim-is-not-assignment)")


def role_claim_notice(ev: dict) -> str | None:
    if ev.get("kind") in {"note", "update"} and ROLE_CLAIM_RE.search(ev.get("summary") or ""):
        return ROLE_CLAIM_NOTICE
    return None


def fmt_event(ev: dict, now: dt.datetime) -> str:
    age = ""
    try:
        d = now - parse_ts(ev["created_at"])
        age = f"{int(d.total_seconds() // 3600)}h" if d.days < 2 else f"{d.days}d"
    except (KeyError, ValueError):
        pass
    unc = "" if ev.get("_committed", True) else " (uncommitted)"
    return f"[{ev.get('kind')}] {actor_str(ev)} {age}: {ev.get('summary')}{unc}"


def render(threads: dict, invalid, *, surface: bool, project: str | None,
           now: dt.datetime, locked: bool, warnings=(), board: str | None = None) -> str:
    lines: list[str] = []
    name = board or "board"
    if locked:
        lines.append(f"🔒 {name} is git-crypt locked on this machine (unlock its repository; see its README)")
        return "\n".join(lines)
    for p, why in invalid:
        lines.append(f"🔴 invalid event file {p.name}: {why}")
    for p, why in warnings:
        lines.append(f"⚠️ history {p.name}: {why}")
    keys = sorted(k for k in threads if not project or k[0] == project)
    if surface:
        for key in keys:
            t = threads[key]
            tag = f"{key[0]}/{key[1]}"
            w = t.get("workflow")
            if w:
                for err in w["errors"]:
                    lines.append(f"🔴 protocol {tag}: {err}")
                if w["waiting_on"]:
                    lines.append(f"📨 {w['waiting_on']['agent']} / {w['waiting_on']['session_id']} 対応待ち [{w['label']}] {tag} — {w['next_action']}")
                    lines.append(f"    {w['title']} / request={w['request_id']} / reply_to={w['reply_to']}")
                    lines.extend(handoff_lines(w))
            for ev in t["active_claims"]:
                lines.append(f"🟢 claim  {tag} — {actor_str(ev)} lease→{ev.get('lease_until')} "
                             f"({(ev.get('actor') or {}).get('task')})")
            for ev in t["stale_claims"]:
                hint = '担当期限切れ。次の対応は上記の宛先へ' if w else 'supersede or close'
                lines.append(f"🟡 stale  {tag} — {actor_str(ev)} lease expired "
                             f"{ev.get('lease_until')} ({hint})")
            for ev in t["open_blockers"]:
                lines.append(f"🔴 block  {tag} — {actor_str(ev)}: {ev.get('summary')}")
            for ev in t["unpromoted"]:
                lines.append(f"🟠 find   {tag} — provisional, promoted_to empty: {ev.get('summary')}")
            for ev in t["events"]:
                if not ev.get("_committed", True):
                    lines.append(f"⚠️ uncommitted event in {tag}: {ev['_path'].name}")
        if lines:
            lines.insert(0, f"📋 {name} (Claude / Codex 寄合所) — 要注意 thread:")
        return "\n".join(lines)
    if not keys:
        lines.append(f"📋 {name}: no events" + (f" for project {project}" if project else ""))
        return "\n".join(lines)
    cur = None
    for key in keys:
        t = threads[key]
        if key[0] != cur:
            cur = key[0]
            lines.append(f"\n## {cur}")
        state = t["workflow"]["label"] + " / " + str((t["workflow"]["waiting_on"] or {}).get("session_id", "—")) if t.get("workflow") else "closed" if t["closed"] else "open"
        flags = []
        if t["active_claims"]:
            flags.append(f"claimed by {', '.join(actor_str(e) for e in t['active_claims'])}")
        if t["stale_claims"]:
            flags.append("stale claim")
        if t["open_blockers"]:
            flags.append("BLOCKED")
        if t["unpromoted"]:
            flags.append(f"{len(t['unpromoted'])} provisional finding")
        lines.append(f"- {key[1]} [{state}{'; ' + '; '.join(flags) if flags else ''}] "
                     f"({len(t['events'])} events, {len(t['live'])} live)")
        for ev in t["live"][-3:]:
            lines.append(f"    {fmt_event(ev, now)}")
            notice = role_claim_notice(ev)
            if notice:
                lines.append(f"      {notice}")
    return "\n".join(lines).lstrip("\n")


# ---------------------------------------------------------------- validate

def validate(root: Path, events: list[dict], invalid) -> list[str]:
    problems = [f"{p}: {why}" for p, why in invalid]
    schema = json.loads(SCHEMA_PATH.read_text())
    check_schema(schema)
    for ev in events:
        p: Path = ev["_path"]
        body = {k: v for k, v in ev.items() if not k.startswith("_")}
        for err in history_errors(body, schema, committed=ev.get('_committed', False))[0]:
            problems.append(f"{p.relative_to(root)}: schema: {err}")
        for err in path_errors(root, p, body):
            problems.append(f"{p.relative_to(root)}: {err}")
    for key, thread in derive(events, now_utc()).items():
        for err in (thread.get("workflow") or {}).get("errors", []):
            problems.append(f"{key}: protocol: {err}")
    return problems


# ---------------------------------------------------------------- template

def build_template(*, kind: str, project: str, thread: str, task: str, agent: str,
                   repo: str | None, restricted: bool, lease_hours: float,
                   summary: str | None, now: dt.datetime) -> tuple[dict, Path]:
    if restricted:
        project, repo, task = "restricted", None, "restricted-task"
        summary = RESTRICTED_SUMMARY[kind]
    ev = {
        "schema_version": 1,
        "event_id": f"{compact(now)}-{agent}-{secrets.token_hex(3)}",
        "thread_id": thread,
        "project": {"key": project, "repo": repo},
        "actor": {"agent": agent, "task": task,
                  "instance": f"{agent}@{socket.gethostname().split('.')[0]}"},
        "kind": kind,
        "source_policy": "encrypted-metadata-only" if restricted else "ordinary",
        "created_at": iso_z(now),
        "lease_until": iso_z(now + dt.timedelta(hours=lease_hours)) if kind == "claim" else None,
        "summary": summary or f"TODO: one-line {kind} summary",
        "touches": [],
        "references": [],
        "promoted_to": [],
        "supersedes": [],
    }
    if not restricted:
        ev["details"] = ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,95}", project) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,95}", thread):
        raise ValueError("invalid project/thread identifier")
    path = Path("events") / project / thread / f"{compact(now)}--{ev['event_id']}.json"  # relative to the board
    return ev, path


# ---------------------------------------------------------------- selftest

def _stats_selftest():
    base = now_utc() - dt.timedelta(hours=10)
    def ev(kind, h, thread, agent="claude", extra=None):
        e = {"schema_version": 2, "kind": kind, "event_id": f"{compact(base + dt.timedelta(hours=h))}-{agent}-{abs(hash((kind, h, thread))) % 0xffffff:06x}",
             "created_at": iso_z(base + dt.timedelta(hours=h)), "project": {"key": "p", "repo": None}, "thread_id": thread, "source_policy": "ordinary",
             "actor": {"agent": agent, "session_id": f"{agent}-1", "instance": "x", "task": "t"}, "summary": "s", "references": []}
        e.update(extra or {}); return e
    r1 = ev("request", 0, "a", extra={"assignee": {"agent": "codex", "session_id": "codex-1"}, "acceptance": "ok"}); r1["request_id"] = r1["event_id"]
    c1 = ev("claim", 1, "a", "codex", {"lease_until": iso_z(base + dt.timedelta(hours=30))}); c1["request_id"] = r1["event_id"]
    s1 = ev("submit", 3, "a", "codex", {"deliverables": ["d"]}); s1.update(request_id=r1["event_id"], reply_to=c1["event_id"])
    a1 = ev("accept", 4, "a", extra={"references": ["x"]}); a1.update(request_id=r1["event_id"], reply_to=s1["event_id"])
    r2 = ev("request", 2, "b", extra={"assignee": {"agent": "claude", "session_id": "role-p-x"}, "acceptance": "ok"}); r2["request_id"] = r2["event_id"]
    text = stats_text([r1, c1, s1, a1, r2], now_utc(), "demo")
    assert "依頼 2 件 (閉じた 1 / 開いている 1)" in text, text
    lines = {l.split()[0]: l.split()[1:] for l in text.splitlines() if l.strip().startswith(("request→", "claim→", "submit→"))}
    assert lines["request→claim"] == ["1", "1.0", "1.0", "1.0"] and lines["claim→submit"][:2] == ["1", "2.0"] and lines["submit→accept"][:2] == ["1", "1.0"], lines
    assert "[引受待ち] p/b → claude/role-p-x" in text and "⏳   0.3 日" in text, text


def _selftest() -> int:
    _stats_selftest()
    now = now_utc()
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        root = repo / "board"   # a board inside a project repository: paths come back relative to it
        root.mkdir()
        (root / bc.CONFIG).write_text(json.dumps({"board_format": 1, "audience": "owner", "encryption": "none"}))
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)

        def ev(kind, project, thread, agent, offset_h, **kw):
            t = now + dt.timedelta(hours=offset_h)
            e = {
                "schema_version": 1,
                "event_id": f"{compact(t)}-{agent}-{secrets.token_hex(3)}",
                "thread_id": thread,
                "project": {"key": project, "repo": kw.get("repo", f"o/{project}")},
                "actor": {"agent": agent, "task": kw.get("task", "task")},
                "kind": kind,
                "source_policy": kw.get("policy", "ordinary"),
                "created_at": iso_z(t),
                "lease_until": kw.get("lease"),
                "summary": kw.get("summary", f"{kind} on {thread}"),
                "touches": [], "references": [],
                "promoted_to": kw.get("promoted", []),
                "supersedes": kw.get("supersedes", []),
            }
            if "details" in kw:
                e["details"] = kw["details"]
            d = root / "events" / project / thread
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{compact(t)}--{e['event_id']}.json"
            p.write_text(json.dumps(e, indent=2))
            if kw.get("commit", True):
                subprocess.run(["git", "-C", str(root), "add", str(p)], check=True)
                subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "Add event"], check=True)
            return e

        # A: live claim (lease future)
        ev("claim", "alpha", "2026-09-02-a", "claude", -1, lease=iso_z(now + dt.timedelta(hours=11)))
        # B: stale claim (lease past)
        ev("claim", "alpha", "2026-09-02-b", "codex", -30, lease=iso_z(now - dt.timedelta(hours=18)))
        # C: finding unpromoted then done -> closed, nothing surfaced
        ev("finding", "beta", "2026-09-01-c", "codex", -5)
        ev("done", "beta", "2026-09-01-c", "codex", -4, promoted=["beta/DESIGN.md"])
        # D: blocker open
        ev("claim", "beta", "2026-09-02-d", "claude", -3, lease=iso_z(now + dt.timedelta(hours=9)))
        ev("blocker", "beta", "2026-09-02-d", "claude", -2, summary="needs owner decision")
        # E: superseded claim -> only the new one counts
        old = ev("claim", "gamma", "2026-09-02-e", "codex", -6, lease=iso_z(now + dt.timedelta(hours=6)))
        ev("claim", "gamma", "2026-09-02-e", "claude", -1, lease=iso_z(now + dt.timedelta(hours=11)),
           supersedes=[old["event_id"]])
        # F: unpromoted finding in open thread
        ev("finding", "gamma", "2026-09-02-f", "claude", -1, summary="provisional X")
        # G: uncommitted event
        ev("update", "gamma", "2026-09-02-f", "codex", 0, commit=False)
        # H: status events — a role self-claim gets the reminder in the project view, plain status does not
        ev("update", "delta", "2026-09-02-h", "codex", -2,
           summary="This session is the review window for the chapter; check scope before editing.")
        ev("update", "delta", "2026-09-02-h", "claude", -1, summary="Checks rerun; all pass.")
        # R: restricted, valid
        ev("claim", "restricted", "r-7f3a", "codex", -1, repo=None, task="restricted-task",
           policy="encrypted-metadata-only", summary="Work claimed.",
           lease=iso_z(now + dt.timedelta(hours=12)))

        events, invalid = load_events(root)
        assert not invalid, invalid
        th = derive(events, now)
        assert len(th["alpha", "2026-09-02-a"]["active_claims"]) == 1
        assert len(th["alpha", "2026-09-02-b"]["stale_claims"]) == 1
        assert th["beta", "2026-09-01-c"]["closed"] and not th["beta", "2026-09-01-c"]["unpromoted"]
        assert len(th["beta", "2026-09-02-d"]["open_blockers"]) == 1
        e = th["gamma", "2026-09-02-e"]
        assert len(e["active_claims"]) == 1 and e["active_claims"][0]["actor"]["agent"] == "claude"
        assert len(th["gamma", "2026-09-02-f"]["unpromoted"]) == 1
        assert any(not x.get("_committed") for x in th["gamma", "2026-09-02-f"]["events"])
        out = render(th, invalid, surface=True, project=None, now=now, locked=False)
        for needle in ("🟢 claim  alpha/2026-09-02-a", "🟡 stale  alpha/2026-09-02-b",
                       "🔴 block  beta/2026-09-02-d", "🟠 find   gamma/2026-09-02-f",
                       "⚠️ uncommitted event"):
            assert needle in out, (needle, out)
        assert "2026-09-01-c" not in out
        # --project filter
        out_b = render(th, invalid, surface=True, project="beta", now=now, locked=False)
        assert "alpha" not in out_b and "beta/2026-09-02-d" in out_b
        # silent when nothing
        assert render({}, [], surface=True, project=None, now=now, locked=False) == ""
        # role self-claim reminder: once, under the claiming status event only; never in the surface view
        out_d = render(th, invalid, surface=False, project="delta", now=now, locked=False)
        assert out_d.count(ROLE_CLAIM_NOTICE) == 1, out_d
        ls_d = out_d.splitlines()
        i = next(k for k, l in enumerate(ls_d) if "review window" in l)
        assert ROLE_CLAIM_NOTICE in ls_d[i + 1] and "Checks rerun" not in ls_d[i + 1], out_d
        assert ROLE_CLAIM_NOTICE not in out
        assert role_claim_notice({"kind": "note", "summary": "導入の編集窓口は X、私は編集しない"})
        assert not role_claim_notice({"kind": "request", "summary": "report only, do not edit paper.tex"})
        assert not role_claim_notice({"kind": "note", "summary": "Codex should post its own finding"})
        # validate: all fixtures pass
        probs = validate(root, events, invalid)
        assert not probs, probs
        # validate: restricted with details / outside restricted dir must fail
        bad = ev("update", "alpha", "2026-09-02-a", "codex", 0, policy="encrypted-metadata-only",
                 details="leak", summary="Work updated.", commit=False)
        events2, inv2 = load_events(root)
        probs2 = validate(root, events2, inv2)
        assert any("schema" in p for p in probs2), probs2
        try:
            import jsonschema  # noqa: F401
            assert any("schema" in p for p in probs2), probs2
        except ImportError:
            pass
        assert bad["event_id"] in "".join(probs2) or probs2
        # --json payload: 述語の結果が運ばれる + commit meta が付く
        payload = to_json_payload(root, th, invalid, now=now, locked=False)
        by = {(t["project"], t["thread_id"]): t for t in payload["threads"]}
        # the HTML viewer shows the same role-claim reminder from the payload (one predicate, two surfaces)
        assert [bool(e["_role_notice"]) for e in by["delta", "2026-09-02-h"]["events"]] == [True, False]
        assert by["alpha", "2026-09-02-a"]["active_claims"] and not by["beta", "2026-09-01-c"]["unpromoted"]
        assert by["beta", "2026-09-01-c"]["closed"] is True
        committed = [e for e in by["alpha", "2026-09-02-a"]["events"] if e["_committed"]]
        assert committed and committed[0]["_commit"] and committed[0]["_commit"]["sha"]
        assert any(not e["_live"] for e in by["gamma", "2026-09-02-e"]["events"])
        json.dumps(payload)  # serialisable
        # locked detection
        assert all(e["_path"] == "events/" + e["project"]["key"] + "/" + e["thread_id"] + "/" + Path(e["_path"]).name
                   for t in payload["threads"] for e in t["events"])  # relative to the board, not the repository
        (root / bc.CONFIG).write_bytes(GITCRYPT_MAGIC + b"\x00junk")
        assert is_locked(root)
        assert "🔒" in render({}, [], surface=True, project=None, now=now, locked=True)
        # template
        t, path = build_template(kind="claim", project="alpha", thread="2026-09-02-z", task="x",
                                 agent="claude", repo="o/alpha", restricted=False,
                                 lease_hours=12, summary=None, now=now)
        assert t["lease_until"] and path.parts[-3:-1] == ("alpha", "2026-09-02-z")
        tr, pr = build_template(kind="done", project="whatever", thread="r-1", task="x",
                                agent="codex", repo="o/x", restricted=True,
                                lease_hours=12, summary=None, now=now)
        assert tr["project"] == {"key": "restricted", "repo": None}
        assert tr["summary"] == "Work completed." and "details" not in tr
        assert pr.parts[-3] == "restricted"
    print("board-view selftest: PASS")
    return 0


# ---------------------------------------------------------------- main

def _filter(threads, a):
    if a.inbox:
        threads = {k: v for k, v in threads.items() if (v.get("workflow") or {}).get("errors") or ((v.get("workflow") or {}).get("waiting_on") or {}).get("session_id") == a.inbox}
    if a.project:
        threads = {k: v for k, v in threads.items() if k[0] == a.project}
    return threads


def one_board(root: Path, a, name: str, now: dt.datetime):
    """(payload for --json, text for the terminal) of one board, read from `root` as it stands."""
    if is_locked(root):
        payload = to_json_payload(root, {}, [], now=now, locked=True, board=name)
        return payload, render({}, [], surface=a.surface, project=a.project, now=now, locked=True, board=name)
    events, invalid = load_events(root)
    warnings = history_warnings(events)
    threads = _filter(derive(events, now), a)
    payload = to_json_payload(root, threads, invalid, now=now, locked=False, warnings=warnings, board=name)
    return payload, render(threads, invalid, surface=a.surface, project=a.project, now=now, locked=False,
                           warnings=warnings, board=name)


def all_boards(a, now: dt.datetime) -> int:
    """Every board in the workspace: the owner's dashboards read collaborator boards next to the owner board."""
    payloads, texts = [], []
    from board import snapshot
    for b in bc.discover(a.workspace):
        if b["error"]:
            payloads.append({"board": b["name"], "error": b["error"], "threads": [], "locked": False, "invalid": [], "warnings": [], "degraded": True})
            texts.append(f"🔴 {b['name']}: {b['error']}")
            continue
        try:
            if a.sync and not b["locked"]:
                with snapshot(b["root"]) as remote_root:
                    p, t = one_board(remote_root, a, b["name"], now)
            else:
                p, t = one_board(b["root"], a, b["name"], now)
        except (ValueError, OSError, subprocess.SubprocessError) as ex:
            p, t = {"board": b["name"], "error": str(ex), "threads": [], "locked": False, "invalid": [], "warnings": [], "degraded": True}, f"🔴 {b['name']}: {ex}"
        p["root"] = str(b["root"]).replace(str(Path.home()), "~")
        if b["config"]:
            p["audience"] = b["config"]["audience"]
        payloads.append(p)
        if t:
            texts.append(t)
    if a.json:
        merged = {
            "generated_at": iso_z(now),
            "host": socket.gethostname().split(".")[0],
            "boards": [{k: p.get(k) for k in ("board", "root", "audience", "head", "locked", "degraded", "error")} for p in payloads],
            "locked": any(p.get("locked") for p in payloads),
            "invalid": [dict(x, path=f"{p['board']}:{x['path']}") for p in payloads for x in p.get("invalid") or []],
            "warnings": [dict(x, path=f"{p['board']}:{x['path']}") for p in payloads for x in p.get("warnings") or []],
            "degraded": any(p.get("degraded") for p in payloads),
            "threads": [t for p in payloads for t in p.get("threads") or []],
        }
        print(json.dumps(merged, ensure_ascii=False, indent=1))
    elif texts:
        print("\n\n".join(texts))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--surface", action="store_true")
    ap.add_argument("--sync", action="store_true", help="read remote in an isolated temporary checkout")
    ap.add_argument("--project")
    ap.add_argument("--inbox", help="session ID of the receiver")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--stats", action="store_true", help="依頼の滞留: 遷移ごとの時間 (中央値 / p90 / 最大) と、 開いている依頼を古い順に")
    ap.add_argument("--json", action="store_true", help="派生 state を JSON で出力 (board-html.py 用)")
    ap.add_argument("--template", choices=sorted(RESTRICTED_SUMMARY))
    ap.add_argument("--thread")
    ap.add_argument("--task", default="")
    ap.add_argument("--agent", choices=["claude", "codex", "human", "other"], default="claude")
    ap.add_argument("--repo")
    ap.add_argument("--summary")
    ap.add_argument("--lease-hours", type=float, default=12.0)
    ap.add_argument("--restricted", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--root", type=Path, help="the board directory (holds board.json and events/)")
    ap.add_argument("--board", help="a board in the workspace by name (board.py boards lists them)")
    ap.add_argument("--all-boards", action="store_true", help="every board in the workspace (dashboards)")
    ap.add_argument("--workspace", type=Path, help="--all-boards: the directory holding the checkouts")
    ap.add_argument("--board-name", help=argparse.SUPPRESS)  # carries the name across the --sync re-entry
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    now = now_utc()
    if a.all_boards:
        if a.write or a.template or a.validate: ap.error("--all-boards is read-only and does not validate")
        return all_boards(a, now)
    root = bc.resolve(a.root, a.board)
    name = a.board_name or bc.name_of(root, None if is_locked(root) else bc.load(root))
    if a.sync:
        if a.write or a.template: ap.error("--sync is read-only")
        from board import snapshot
        with snapshot(root) as remote_root:
            args = [x for x in (argv if argv is not None else sys.argv[1:]) if x != "--sync"]
            # Re-enter with an explicit temporary root and no network flag.
            return main(args + ["--root", str(remote_root), "--board-name", name])

    if a.template:
        if not a.thread or (not a.restricted and not a.project):
            ap.error("--template needs --thread and (--project or --restricted)")
        if not a.restricted and not a.task:
            ap.error("--template needs --task unless --restricted")
        ev, path = build_template(kind=a.template, project=a.project or "restricted",
                                  thread=a.thread, task=a.task, agent=a.agent, repo=a.repo,
                                  restricted=a.restricted, lease_hours=a.lease_hours,
                                  summary=a.summary, now=now)
        path = root / path
        text = json.dumps(ev, indent=2, ensure_ascii=False) + "\n"
        if a.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(path.relative_to(root))
            print("→ edit summary/touches/references, then: "
                  f"git add {path.relative_to(root)} && git commit -m 'Add event' && git push",
                  file=sys.stderr)
        else:
            print(text, end="")
            print(f"# suggested path: {path.relative_to(root)}", file=sys.stderr)
        return 0

    if a.stats:
        if is_locked(root):
            print(render({}, [], surface=False, project=None, now=now, locked=True, board=name)); return 0
        events, _ = load_events(root)
        print(stats_text(events, now, name))
        return 0
    if a.validate:
        if is_locked(root):
            print(render({}, [], surface=False, project=None, now=now, locked=True, board=name))
            return 0
        events, invalid = load_events(root)
        probs = validate(root, events, invalid)
        for p, why in history_warnings(events):
            print(f"⚠️ history {p.name}: {why}")
        for p in probs:
            print(f"🔴 {p}")
        print(f"validated {len(events)} event(s): {'FAIL' if probs else 'OK'}")
        return 1 if probs else 0
    payload, text = one_board(root, a, name, now)
    if a.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    elif text:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as ex:
        print(f"🔴 board unavailable: {ex}", file=sys.stderr)
        sys.exit(1)
    except BrokenPipeError:
        os._exit(0)
