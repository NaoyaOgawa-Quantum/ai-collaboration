#!/usr/bin/env python3
"""board-post-reach-check.py — Stop hook: when a board post in this turn printed the next actor's direct address, was it
messaged? (CONTRACT.md#post-then-push: a post is a record, not a push; the rule said "send one line", the session forgot.)

What it reads: the session transcript named in the hook payload (`transcript_path`), only the current turn (after the
last real user message). A reach line is what `board.py` prints after a post whose next actor is alive on this machine:
`SendMessage の to = uds:…` (a Claude session, any config dir) or a `codex queue --thread …` command (a Codex thread).
Satisfied when, later in the same turn, a `SendMessage` / `send_message` tool call was made (for a Claude address) or a
Bash call containing `codex queue` (for a Codex thread). The sender's `to` is not matched against the printed address:
a peer in the same config dir may be messaged by name, and one message after the post is the whole obligation.

The other direction (kind `ack`): a cross-session message in this turn's user text that names a board request id hands
this session a turn (a revise, an answer, a request). Satisfied when the turn sends any message or posts on the board
(`board.py <posting command>` in a Bash call). Measured: a worker fixed a revise within two minutes and said so only in
its chat, which the requester cannot see; the requester saw an idle session for seven minutes and sent twice more.

Modes (environment BOARD_POST_REACH_STOP; the owner's wiring sets it): `observe` (default) records each miss as one JSON
line under `<config dir>/state/board-post-reach/observe.jsonl` and never blocks; `block` returns
`{"decision": "block", "reason": …}` once per turn (loop guard: `stop_hook_active`); `off` does nothing. Raise to
`block` only after the observe log shows no false positives (same two-step as the first-reply stamp check).

Fail-open: an unreadable payload or transcript, or any exception, prints nothing and exits 0. The wiring (a Stop entry
in the owner's settings calling this script) belongs to the owner's layer; this file is the shared kernel.

    board-post-reach-check.py            # hook payload on stdin
    board-post-reach-check.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

REACH_CLAUDE = re.compile(r'SendMessage の to = (uds:\S+)')
REACH_CODEX = re.compile(r'\bcodex queue\b')
SEND_TOOLS = {'SendMessage', 'mcp__ccd_session_mgmt__send_message'}
# The other direction: a cross-session message that names a board request hands this session a turn (a revise, an
# answer, a request). Answered when the turn sends a message or posts on the board; the sender sees neither chat text.
INBOUND = re.compile(r'<cross-session-message\b')
INBOUND_FROM = re.compile(r'<cross-session-message[^>]*\bfrom="([^"]+)"')
REQUEST_ID = re.compile(r'\b\d{8}T\d{6}Z-(?:claude|codex|human|other)-[0-9a-f]{6}\b')
BOARD_POST = re.compile(r'\bboard\.py\s+(?:request|claim|submit|accept|revise|release|blocker|update|handover|abandoned|note|touch|untouch|retry)\b')


def load_entries(path):
    out = []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            try: e = json.loads(line)
            except Exception: continue
            if isinstance(e, dict): out.append(e)
    return out


def blocks(e):
    c = (e.get('message') or {}).get('content')
    return c if isinstance(c, list) else []


def is_real_user(e):
    if e.get('type') != 'user': return False
    c = (e.get('message') or {}).get('content')
    if isinstance(c, str): return True
    return not any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in c or [])


def current_turn(entries):
    start = 0
    for i, e in enumerate(entries):
        if is_real_user(e): start = i
    return entries[start:]


def text_of(block):
    c = block.get('content')
    if isinstance(c, str): return c
    if isinstance(c, list): return '\n'.join(str(x.get('text', '')) for x in c if isinstance(x, dict))
    return ''


def steps(turn):
    """The turn as a flat list of ('result', text) and ('call', tool name, input) in order."""
    out = []
    for e in turn:
        for b in blocks(e):
            if not isinstance(b, dict): continue
            if e.get('type') == 'user' and b.get('type') == 'tool_result': out.append(('result', text_of(b), None))
            elif e.get('type') == 'assistant' and b.get('type') == 'tool_use': out.append(('call', b.get('name') or '', b.get('input') or {}))
    return out


def handovers(turn):
    """Senders of cross-session messages in this turn's user text that name a board request: ['uds:…', …]."""
    out = []
    for e in turn:
        if not is_real_user(e): continue
        c = (e.get('message') or {}).get('content')
        text = c if isinstance(c, str) else '\n'.join(str(b.get('text', '')) for b in c if isinstance(b, dict)) if isinstance(c, list) else ''
        if INBOUND.search(text) and REQUEST_ID.search(text):
            m = INBOUND_FROM.search(text); out.append(m.group(1) if m else '(sender unknown)')
    return list(dict.fromkeys(out))


def misses(turn):
    """Reach lines of this turn that no later direct message or codex queue answered, and board hand-over messages
    this turn neither answered nor acted on with a post: [(kind, address)] with kind claude / codex / ack."""
    flat = steps(turn); out = []
    senders = handovers(turn)
    if senders:
        replied = any(s[0] == 'call' and s[1] in SEND_TOOLS for s in flat)
        posted = any(s[0] == 'call' and s[1] == 'Bash' and BOARD_POST.search(str((s[2] or {}).get('command', ''))) for s in flat)
        if not replied and not posted: out.extend(('ack', a) for a in senders)
    for i, step in enumerate(flat):
        if step[0] != 'result': continue
        later = flat[i + 1:]
        for addr in REACH_CLAUDE.findall(step[1]):
            if not any(s[0] == 'call' and s[1] in SEND_TOOLS for s in later): out.append(('claude', addr))
        if REACH_CODEX.search(step[1]) and 'inbox --agent codex' in step[1]:
            if not any(s[0] == 'call' and s[1] == 'Bash' and REACH_CODEX.search(str((s[2] or {}).get('command', ''))) for s in later):
                out.append(('codex', 'codex queue'))
    seen, uniq = set(), []
    for m in out:
        if m not in seen: seen.add(m); uniq.append(m)
    return uniq


def state_dir():
    base = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    d = base / 'state' / 'board-post-reach'; d.mkdir(parents=True, exist_ok=True); return d


def decide(payload, mode):
    if mode == 'off' or payload.get('stop_hook_active'): return None
    path = payload.get('transcript_path')
    if not path or not os.path.isfile(path): return None
    found = misses(current_turn(load_entries(path)))
    if not found: return None
    lines = [f'{k}: {a}' for k, a in found]
    if mode == 'block':
        acks = [a for k, a in found if k == 'ack']; posts = [f'{k}: {a}' for k, a in found if k != 'ack']
        parts = []
        if posts:
            parts.append('📮 掲示板に投稿し、 次に動く session がこの機械で生きている (' + '; '.join(posts) + ') のに、 この turn に SendMessage '
                         '(Codex なら codex queue) が無い。 投稿は push でない (CONTRACT#post-then-push): 今 1 行送る (thread id + 書いた中身)')
        if acks:
            parts.append('📨 掲示板の番を渡す message を受けた (' + '; '.join(acks) + ') のに、 この turn に返事 (SendMessage) も掲示板への投稿も無い。 '
                         '相手に見えるのは投稿と message だけ (chat は見えない): 「受け取った・何をする・いつ submit する」 の 1 行を SendMessage で返す')
        return {'decision': 'block', 'reason': ' / '.join(parts) + '。 送らない理由 (相手が自分 / 既に別の経路で伝えた / 知らせだけで番が動かない) なら短い追記に 1 行。 返事の本文は出し直さない'}
    try:
        with (state_dir() / 'observe.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'session': (payload.get('session_id') or '')[:8],
                                'misses': lines, 'transcript': str(path)}, ensure_ascii=False) + '\n')
    except OSError: pass
    return None


def main():
    if '--selftest' in sys.argv: return _selftest()
    try:
        payload = json.loads(sys.stdin.read() or '{}')
        if not isinstance(payload, dict): return 0
        out = decide(payload, os.environ.get('BOARD_POST_REACH_STOP', 'observe').strip().lower() or 'observe')
        if out: print(json.dumps(out, ensure_ascii=False))
    except Exception:
        return 0
    return 0


def _selftest():
    import tempfile
    reach = 'event_id=x\nposted x\n→ 次に動く claude/56706b91 はこの機械で生きている: SendMessage の to = uds:/tmp/cc-socks/1.sock で thread id と書いた中身を 1 行\n'
    codex = 'posted y\n→ 次に動く codex/01a00000 に知らせる (生きている): codex queue --thread 01a0 --message \'掲示板 p/t: 書き込みあり。 inbox --agent codex --session 01a0 --sync で読む\'\n'
    inbound = ('Another Claude session sent a message:\n<cross-session-message from="uds:/tmp/cc-socks/7.sock" from-session="local_x" from-name="n" from-mode="prompting">\n'
               '依頼 20260101T000000Z-claude-abcdef に revise を出しました。 直したら submit してください。\n</cross-session-message>')
    def u(text): return {'type': 'user', 'message': {'content': text}}
    def res(text): return {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': text}]}}
    def call(name, inp=None): return {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': name, 'input': inp or {}}]}}
    def text(t): return {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': t}]}}
    cases = {
        'post then no message → miss': ([u('go'), call('Bash'), res(reach), text('done')], [('claude', 'uds:/tmp/cc-socks/1.sock')]),
        'post then SendMessage → ok': ([u('go'), call('Bash'), res(reach), call('SendMessage', {'to': 'x', 'message': 'm'}), text('done')], []),
        'post then desktop send_message → ok': ([u('go'), call('Bash'), res(reach), call('mcp__ccd_session_mgmt__send_message', {'session_id': 'x', 'message': 'm'})], []),
        'message before the post does not count': ([u('go'), call('SendMessage', {'to': 'x', 'message': 'm'}), call('Bash'), res(reach)], [('claude', 'uds:/tmp/cc-socks/1.sock')]),
        'reach in an earlier turn only → ok': ([u('go'), call('Bash'), res(reach), u('next'), call('Bash'), res('posted z\n')], []),
        'codex thread then codex queue via Bash → ok': ([u('go'), call('Bash'), res(codex), call('Bash', {'command': "codex queue --thread 01a0 --message 'x'"})], []),
        'codex thread, no queue → miss': ([u('go'), call('Bash'), res(codex)], [('codex', 'codex queue')]),
        'no post → ok': ([u('go'), call('Bash'), res('ok\n'), text('done')], []),
        'two posts, one message → ok': ([u('go'), call('Bash'), res(reach), call('Bash'), res(reach), call('SendMessage', {'to': 'x'})], []),
        # the other direction: a hand-over message answered by a reply, by a board post, or not at all
        'hand-over message, no reply, no post → ack miss': ([u(inbound), call('Bash', {'command': 'git status'}), text('直します')], [('ack', 'uds:/tmp/cc-socks/7.sock')]),
        'hand-over message + SendMessage → ok': ([u(inbound), call('SendMessage', {'to': 'x', 'message': '受け取った'}), text('ok')], []),
        'hand-over message + board post → ok': ([u(inbound), call('Bash', {'command': 'python3 scripts/board.py submit --root b --request x'}), text('ok')], []),
        'cross-session chat without a request id → ok': ([u('<cross-session-message from="uds:/tmp/cc-socks/7.sock">お疲れさま</cross-session-message>'), text('ok')], []),
        'request id outside a cross-session message → ok': ([u('request 20260101T000000Z-claude-abcdef を見て'), text('ok')], []),
        'hand-over message in an earlier turn only → ok': ([u(inbound), call('SendMessage', {'to': 'x'}), u('next'), call('Bash'), text('ok')], []),
    }
    with tempfile.TemporaryDirectory() as td:
        os.environ['CLAUDE_CONFIG_DIR'] = td
        for name, (entries, want) in cases.items():
            p = Path(td) / 'tr.jsonl'
            p.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            got = misses(current_turn(load_entries(p)))
            assert got == want, (name, got, want)
            out = decide({'transcript_path': str(p), 'session_id': 'abcdef12'}, 'block')
            assert bool(out) == bool(want), (name, out)
            assert decide({'transcript_path': str(p), 'stop_hook_active': True}, 'block') is None, name
            assert decide({'transcript_path': str(p)}, 'off') is None, name
            assert decide({'transcript_path': str(p), 'session_id': 'abcdef12'}, 'observe') is None, name
        log = (Path(td) / 'state' / 'board-post-reach' / 'observe.jsonl').read_text().splitlines()
        assert len(log) == sum(1 for _, (e, w) in cases.items() if w), len(log)
        assert all('uds:' in l or 'codex' in l for l in log)
        assert sum('"ack: ' in l for l in log) == 1, log
        blocked = decide({'transcript_path': str(p), 'session_id': 'abcdef12'}, 'block')   # p = the last case written (ok) → None
        assert blocked is None
        p.write_text('\n'.join(json.dumps(e) for e in cases['hand-over message, no reply, no post → ack miss'][0]) + '\n')
        r = decide({'transcript_path': str(p), 'session_id': 'abcdef12'}, 'block')['reason']
        assert '📨' in r and 'uds:/tmp/cc-socks/7.sock' in r and '📮' not in r, r
        assert decide({'transcript_path': str(Path(td) / 'missing.jsonl')}, 'block') is None   # fail-open
    print('board-post-reach-check selftest: ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
