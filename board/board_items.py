#!/usr/bin/env python3
"""board_items.py — claim a work item (not a file) among parallel sessions on one machine. See CONTRACT.md#item-claims.

Why: several sessions opened in one workspace receive the same list of pending items (a mail thread, a ledger entry, a
request) from their start-of-session surfaces and end-of-turn checks. Each is told "handle this" separately, so two of
them read the same mail and file two records for it (measured). A claim lets the first session to start on an item say
so; the others show "<session> is on it" instead of asking their own model to handle it.

What it is:
- An item is named by a key the caller chooses (`mail:<id>`, `todo:<id>`, `board:<project>/<thread>`). One item may
  have several aliases (a reply and its ledger entry): `hold` takes them together or not at all.
- The ledger is local state of the board checkout, shared by every session on this machine that uses the checkout:
  `<board>/.local/items.json` for a whole-repository board with an ignored `.local/`, else
  `<git common dir>/board-items/<board subdir>.json` (the same rule as the post outbox). It never enters history: a
  claim lives for minutes, and history is for durable events. Readers need no network and no sync.
- A claim has a lease (default 90 minutes; `hold` again renews it). It stops holding when the lease ends, when the
  holder releases it, or when the caller's liveness test says the holder session has ended. Nothing is silently
  dropped: an expired or dead claim simply makes the item pending again everywhere.
- First holder wins. A `hold` of an item another live session holds takes nothing and returns that holder.

Not here (deliberately): mirroring claims to other machines. A board post costs a fresh checkout of the whole board,
so posting every claim would grow history and slow every post. Other machines learn of the work when its record (the
inbox entry, the ledger entry) is committed and pushed. The coupling to the rest of the engine is the ledger location.

CLI (exit 0 = taken / nobody else holds it; 2 = another live session holds it; 1 = error):
    python3 board_items.py hold    --root <board> --key mail:<id> [--key todo:<id>] [--label '<short>'] [--lease-min 90]
    python3 board_items.py held    --root <board> --key mail:<id>        # who else holds it
    python3 board_items.py release --root <board> [--key …]              # no key = everything this session holds
    python3 board_items.py list    --root <board> [--json]
    python3 board_items.py --selftest
The session is `--session`, else CLAUDE_CODE_SESSION_ID / CODEX_THREAD_ID (one address per native session).
env: BOARD_ITEMS_LEDGER = ledger path (tests and callers outside a board checkout).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no advisory lock; writes stay atomic (os.replace), concurrent holds are best effort
    fcntl = None

DEFAULT_LEASE_MIN = 90
MAX_KEY = 300
AGENTS = ('claude', 'codex', 'human', 'other')
BASE_FIELDS = ('agent', 'session', 'name', 'label', 'since', 'lease_until')


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse(s):
    try:
        return dt.datetime.strptime(str(s), '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def ledger_path(root=None):
    """Where this board checkout keeps its item claims (never inside a tracked tree)."""
    env = os.environ.get('BOARD_ITEMS_LEDGER')
    if env:
        return Path(os.path.expanduser(env))
    root = Path(root or '.').expanduser().resolve()
    sub = ''
    with contextlib.suppress(Exception):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import board_config as bc
        sub = bc.subdir(root) or ''
    if not sub and (root / '.local').is_dir():
        return root / '.local' / 'items.json'
    common = subprocess.run(['git', '-C', str(root), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                            capture_output=True, text=True)
    if common.returncode:
        raise ValueError(f'{root}: not a board checkout (no .local/ and not a git repository); set BOARD_ITEMS_LEDGER')
    return Path(common.stdout.strip()) / 'board-items' / ((sub.replace('/', '_') or 'root') + '.json')


def check_key(k):
    if not isinstance(k, str) or not k.strip() or len(k) > MAX_KEY or '\n' in k:
        raise ValueError(f'item key must be one line of 1–{MAX_KEY} characters: {k!r}')
    return k.strip()


def _load(p):
    try:
        d = json.loads(Path(p).read_text(encoding='utf-8'))
        if isinstance(d, dict) and isinstance(d.get('items'), dict):
            return d
    except (OSError, ValueError):
        pass
    return {'version': 1, 'items': {}}


def _save(p, d):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.items-', dir=str(p.parent))
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


@contextlib.contextmanager
def _locked(p):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(str(p) + '.lock', 'a') as fh:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _live(rec, now, alive):
    """A claim holds while its lease runs and alive(claim) has not reported its session ended (False; None = unknown)."""
    lease = parse(rec.get('lease_until'))
    if lease is None or lease <= now:
        return False
    if alive is not None:
        with contextlib.suppress(Exception):
            if alive(rec) is False:
                return False
    return True


def _mine(rec, me):
    return me is not None and (rec.get('agent'), rec.get('session')) == tuple(me)


def holders(root, keys, me=None, *, now=None, alive=None, path=None):
    """{key: claim} for the keys another live session holds (me = (agent, session) is left out)."""
    now = now or now_utc()
    items = _load(path or ledger_path(root))['items']
    out = {}
    for k in keys:
        rec = items.get(k)
        if rec and _live(rec, now, alive) and not _mine(rec, me):
            out[k] = rec
    return out


def hold(root, groups, me, *, name='', label='', lease_min=DEFAULT_LEASE_MIN, now=None, alive=None, path=None,
         extra=None, labels=None):
    """Claim each group of alias keys for me = (agent, session), all or nothing per group.

    Returns one result per group: {'taken': bool, 'holder': claim or None}. A group whose alias another live session
    holds is not taken (first holder wins); a group this session already holds is renewed. One lock covers the batch.
    extra = small caller fields kept on the claim (str / int / bool), e.g. what the caller's liveness test reads.
    labels = one label per group (overrides label).
    """
    agent, session = me
    if agent not in AGENTS or not session:
        raise ValueError('hold needs an agent and a session id')
    groups = [[check_key(k) for k in g] for g in groups]
    now = now or now_utc()
    lease = iso(now + dt.timedelta(minutes=max(1, int(lease_min))))
    p = Path(path or ledger_path(root))
    results = []
    with _locked(p):
        d = _load(p)
        items = d['items']
        for k in [k for k, r in items.items() if not _live(r, now, alive)]:
            del items[k]  # ended claims leave the ledger on the next write
        for i, g in enumerate(groups):
            lab = labels[i] if labels and i < len(labels) and labels[i] else label
            other = next((items[k] for k in g if k in items and not _mine(items[k], me)), None)
            if other:
                results.append({'taken': False, 'holder': other})
                continue
            for k in g:
                old = items.get(k) or {}
                items[k] = {**{x: v for x, v in (extra or {}).items()
                               if isinstance(v, (str, int, bool)) and x not in BASE_FIELDS},
                            'agent': agent, 'session': session, 'name': (name or session[:8])[:80],
                            'label': (lab or old.get('label') or '')[:120],
                            'since': old.get('since') or iso(now), 'lease_until': lease}
            results.append({'taken': True, 'holder': None})
        _save(p, d)
    return results


def release(root, me, keys=None, *, path=None):
    """Drop me's claims on keys (all of me's claims when keys is None). Returns the released keys."""
    p = Path(path or ledger_path(root))
    with _locked(p):
        d = _load(p)
        gone = [k for k, r in d['items'].items() if _mine(r, me) and (keys is None or k in keys)]
        for k in gone:
            del d['items'][k]
        if gone:
            _save(p, d)
    return gone


def listing(root, *, now=None, alive=None, path=None):
    now = now or now_utc()
    return {k: r for k, r in _load(path or ledger_path(root))['items'].items() if _live(r, now, alive)}


def describe(rec):
    """One line for a surface: who holds it, since when (UTC) and until when."""
    who = rec.get('name') or (rec.get('session') or '')[:8]
    sid = (rec.get('session') or '')[:8]
    tail = f" ({rec.get('agent')}/{sid})" if who != sid else f" ({rec.get('agent')})"
    return (f"👥 {who}{tail} が {str(rec.get('since', ''))[11:16]}Z から対応中"
            f" (lease {str(rec.get('lease_until', ''))[11:16]}Z まで)")


def _selftest():
    fails = []

    def check(cond, label):
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        if not cond:
            fails.append(label)

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / 'items.json'
        t0 = dt.datetime(2026, 1, 5, 9, 0, tzinfo=dt.timezone.utc)
        A, B = ('claude', 'sess-a'), ('claude', 'sess-b')
        r = hold(None, [['mail:m1', 'todo:t1']], A, now=t0, path=p)
        check(r == [{'taken': True, 'holder': None}], 'A takes an item with two aliases')
        hold(None, [['mail:mx']], A, now=t0, path=p, extra={'registered': True, 'session': 'spoof', 'obj': [1]})
        rx = _load(p)['items']['mail:mx']
        check(rx.get('registered') is True and rx['session'] == 'sess-a' and 'obj' not in rx,
              'extra fields are kept, but cannot overwrite the claim fields and must be scalars')
        r = hold(None, [['todo:t1'], ['mail:m2']], B, now=t0 + dt.timedelta(minutes=1), path=p)
        check(not r[0]['taken'] and r[0]['holder']['session'] == 'sess-a' and r[1]['taken'],
              'B is refused the alias A holds (first holder wins) and takes an unrelated item in the same batch')
        check(set(holders(None, ['mail:m1', 'mail:m2'], B, now=t0, path=p)) == {'mail:m1'},
              'holders() names what another session holds and leaves out my own')
        check(holders(None, ['mail:m1'], A, now=t0, path=p) == {}, 'my own claim is not reported as held')
        later = t0 + dt.timedelta(minutes=DEFAULT_LEASE_MIN + 1)
        check(holders(None, ['mail:m1'], B, now=later, path=p) == {},
              'the item comes back when the lease ends (nothing silently dropped)')
        r = hold(None, [['mail:m1']], B, now=later, path=p)
        check(r[0]['taken'] and 'todo:t1' not in _load(p)['items'], 'an expired claim is taken over; ended claims are pruned')
        hold(None, [['mail:m3']], A, now=later, path=p)
        dead = lambda rec: rec.get('session') != 'sess-a'  # noqa: E731
        check(holders(None, ['mail:m3'], B, now=later, alive=dead, path=p) == {},
              'a claim of a session reported ended does not hold')
        check(holders(None, ['mail:m3'], B, now=later, alive=lambda rec: None, path=p) != {},
              'an unknown liveness (None) keeps the claim until its lease')
        s0 = _load(p)['items']['mail:m1']['since']
        hold(None, [['mail:m1'], ['mail:m2']], B, now=later + dt.timedelta(minutes=30), path=p)
        rec = _load(p)['items']['mail:m1']
        check(rec['since'] == s0 and rec['lease_until'] > iso(later + dt.timedelta(minutes=60)),
              'holding again renews the lease and keeps the start time')
        check(release(None, B, ['mail:m1'], path=p) == ['mail:m1'] and 'mail:m2' in _load(p)['items'],
              'release of one key leaves the others')
        check(sorted(release(None, B, path=p)) == ['mail:m2'], 'release without keys drops everything the session holds')
        try:
            hold(None, [['bad\nkey']], A, path=p)
            check(False, 'a multi-line key is refused')
        except ValueError:
            check(True, 'a multi-line key is refused')
        if fcntl:  # eight sessions claiming one item at the same moment: exactly one takes it
            env = dict(os.environ, BOARD_ITEMS_LEDGER=str(Path(td) / 'race.json'))
            procs = [subprocess.Popen([sys.executable, __file__, 'hold', '--key', 'mail:race', '--session', f'race-{i}'],
                                      env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(8)]
            codes = [pr.wait() for pr in procs]
            check(codes.count(0) == 1 and codes.count(2) == 7, f'concurrent holds: one winner, seven refused ({codes})')
        (Path(td) / 'broken.json').write_text('{not json')
        check(holders(None, ['x'], A, path=Path(td) / 'broken.json') == {}, 'a broken ledger reads as empty')
        check(hold(None, [['x']], A, path=Path(td) / 'broken.json')[0]['taken'], 'and is rewritten on the next hold')
        board = Path(td) / 'board'
        (board / '.local').mkdir(parents=True)
        os.environ.pop('BOARD_ITEMS_LEDGER', None)
        check(ledger_path(board) == board.resolve() / '.local' / 'items.json',
              'a whole-repository board with .local/ keeps the ledger there')
        line = describe({'agent': 'claude', 'session': 'abcdef1234', 'name': 'abcdef12',
                         'since': '2026-01-05T09:00:00Z', 'lease_until': '2026-01-05T10:30:00Z'})
        check('abcdef12' in line and '09:00Z' in line and '10:30Z' in line, 'describe() names the holder and the times')
    print(f"selftest: {'ALL PASS' if not fails else 'FAIL ' + str(len(fails))}")
    return 1 if fails else 0


def main():
    if '--selftest' in sys.argv[1:]:
        return _selftest()
    ap = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    ap.add_argument('command', choices=['hold', 'held', 'release', 'list'])
    ap.add_argument('--root', default='.', help='the board directory')
    ap.add_argument('--key', action='append', default=[], help='an item key (repeatable; hold = aliases of one item)')
    ap.add_argument('--agent', choices=AGENTS, default='claude')
    ap.add_argument('--session', help='default: CLAUDE_CODE_SESSION_ID / CODEX_THREAD_ID')
    ap.add_argument('--session-name', default='')
    ap.add_argument('--label', default='', help='hold: a short description shown to other sessions')
    ap.add_argument('--lease-min', type=int, default=DEFAULT_LEASE_MIN)
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    session = a.session or os.environ.get('CLAUDE_CODE_SESSION_ID') or os.environ.get('CODEX_THREAD_ID') or ''
    me = (a.agent, session) if session else None
    try:
        if a.command == 'list':
            got = listing(a.root)
            if a.json:
                print(json.dumps(got, ensure_ascii=False, indent=1))
            for k, r in sorted(got.items()) if not a.json else ():
                print(f'{k}  {describe(r)}')
            return 0
        if not a.key and a.command != 'release':
            ap.error(f'{a.command} needs --key')
        if a.command == 'held':
            got = holders(a.root, a.key, me)
            for k, r in got.items():
                print(f'🔴 {k} — {describe(r)}')
            return 2 if got else 0
        if not me:
            ap.error(f'{a.command} needs --session (or CLAUDE_CODE_SESSION_ID / CODEX_THREAD_ID)')
        if a.command == 'release':
            gone = release(a.root, me, a.key or None)
            print('手放した: ' + (', '.join(gone) or '(なし)'))
            return 0
        r = hold(a.root, [a.key], me, name=a.session_name, label=a.label, lease_min=a.lease_min)[0]
        if r['taken']:
            print(f"🟢 {', '.join(a.key)} — この session が対応する (lease {a.lease_min} 分)")
            return 0
        print(f"🔴 {', '.join(a.key)} — {describe(r['holder'])}。 二重にやらない: 相手に聞くか、 lease が切れるのを待つ")
        return 2
    except (ValueError, OSError) as ex:
        print('ERROR: ' + str(ex), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
