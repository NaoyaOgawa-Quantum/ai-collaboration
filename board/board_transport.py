#!/usr/bin/env python3
"""board_transport.py — read and write a board through its repository's object store: one fetch, one push, no
temporary clone, and only the events this machine has not decrypted yet are decrypted.

Why (measured on a board of ~1000 events): the old writer cloned the remote into a temporary directory
and ran `git-crypt unlock` over every tracked file (24 ms per file, one process each), twice per post — a minute per
post, growing by a second a day. The event files, their paths and the commit shape are unchanged here, so a board
written by this transport is read by the old engine and vice versa; `AGENT_BOARD_TRANSPORT=clone` selects the old
transport for one release.

Reading (`Repo.materialize`): `git fetch origin <branch>`, then the tree of `refs/remotes/origin/<branch>` is
listed and each event blob is written as a plain file into a temporary board directory. A blob is decrypted with
`git cat-file --filters` (the checkout's own git-crypt filter; the checkout must be unlocked) once per machine and
kept by its blob id under `~/.cache/agent-board/<board>/blobs/` (`AGENT_BOARD_CACHE_DIR` moves it): ciphertext is
content-addressed, so the cache never goes stale. The materialised directory has no `.git`; `.order` carries the
events' commit order on the branch (what board-view's `event_paths` derives from `git log`) and `.base` the commit
read.

Writing (`Repo.write_blob` / `commit` / `push`): the new event is hashed with `git hash-object --path` (the path's
clean filter encrypts it), added to the fetched tree through a temporary index file, committed on top of the fetched
commit with the neutral subject, and pushed as `<sha>:refs/heads/<branch>`. A rejected push means another writer won:
the caller fetches again and revalidates (board.py, three attempts). The caller's index and working tree are not used.

After a push on an owner board that is a whole repository, `fast_forward` moves the local checkout to the pushed
commit when its HEAD is an ancestor of it and its tracked files are clean, under a lock in the Git directory — so
other sessions on this machine read the post from the checkout without `--sync`. A board inside a shared project is
never fast-forwarded (the collaborators' checkouts are theirs). `notify` writes the pushed commit to
`<cache>/last-push`, which `board.py watch` polls between remote checks (same-machine wake in seconds).

Codex: inside its sandbox there is no network, so fetch and push fail there exactly as the clone did.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
try:
    import fcntl                      # POSIX; Windows has no fcntl (board_items.py guards it the same way)
except ImportError:                   # the fast-forward lock below is then skipped
    fcntl = None
from pathlib import Path

import board_config as bc

GIT_TIMEOUT = int(os.environ.get('AGENT_BOARD_GIT_TIMEOUT', '120'))
ORDER_FILE = '.order'   # materialised board: `<commit sha10>|<date>|<author>|<path>` per committed event, branch order
BASE_FILE = '.base'     # materialised board: the commit it was read from
NOTIFY_FILE = 'last-push'
LAST_FETCH_FILE = 'last-fetch'   # when this machine last fetched or pushed the branch (any process): an optimistic post trusts a young one
TREE_DIR = 'tree'                # <cache>/tree/<commit>/ = the board at that commit, built once from hard links to the blob cache
TREE_KEEP = 5                    # materialised commits kept per board (older ones are removed after TREE_TTL_S)
TREE_TTL_S = 600
NOTIFY_HOSTS_ENV = 'AGENT_BOARD_NOTIFY_HOSTS'   # comma-separated ssh hosts whose watch is woken after a push (owner wiring)
NOTIFY_SSH_ENV = 'AGENT_BOARD_NOTIFY_SSH'       # the ssh executable (tests point it at a stub)
FETCH_RETRIES = 4       # two sessions fetching one repository at once: one loses the ref lock (layer-1 #concurrent-fetch-ref-lock)


def git(root, *args, check=True, env=None, input=None):
    e = dict(os.environ)
    if env: e.update(env)
    p = subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=False, timeout=GIT_TIMEOUT, env=e, input=input)
    if check and p.returncode:
        raise ValueError(f'git {args[0]} failed: ' + p.stderr.decode(errors='replace')[:1200])
    return p


def transport() -> str:
    """`fetch` (this module) unless AGENT_BOARD_TRANSPORT=clone (the temporary-clone transport kept for one release)."""
    return 'clone' if os.environ.get('AGENT_BOARD_TRANSPORT', '').strip().lower() == 'clone' else 'fetch'


def parse_log(text: str, prefix: str):
    """(path below the board, sha10, date, author) for each event file in the order its adding commit entered the branch."""
    cur = None
    out, seen = [], set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('COMMIT|'):
            sha, date, author = (line.split('|', 3)[1:] + ['', ''])[:3]
            cur = (sha[:10], date, author)
            continue
        if not line.endswith('.json') or not line.startswith(prefix): continue
        rel = line[len(prefix):]
        if rel in seen or not rel.startswith('events/'): continue
        seen.add(rel); out.append((rel, *(cur or ('', '', ''))))
    return out


class Repo:
    """One board's repository: paths, remote, branch, cache."""

    def __init__(self, root):
        self.root = Path(root)
        top = git(self.root, 'rev-parse', '--show-toplevel').stdout.decode().strip()
        self.top = Path(top)
        self.common = Path(git(self.root, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.decode().strip())
        self.sub = bc.subdir(self.root)
        self.remote = git(self.root, 'remote', 'get-url', 'origin').stdout.decode().strip()
        try: self.branch = bc.load(self.root)['branch']
        except ValueError: self.branch = 'main'

    @property
    def prefix(self) -> str:
        return self.sub + '/' if self.sub else ''

    @property
    def tracking(self) -> str:
        return f'refs/remotes/origin/{self.branch}'

    def cache_dir(self) -> Path:
        key = hashlib.sha1((self.remote + '\n' + self.sub).encode()).hexdigest()[:16]
        base = Path(os.environ.get('AGENT_BOARD_CACHE_DIR') or Path.home() / '.cache' / 'agent-board')
        d = base / key
        (d / 'blobs').mkdir(parents=True, exist_ok=True)
        try: os.chmod(base, 0o700); os.chmod(d, 0o700)
        except OSError: pass
        return d

    # ---------------------------------------------------------------- remote

    def fetch(self) -> str:
        """Fetch the board's branch; returns the commit now at origin/<branch>."""
        for i in range(FETCH_RETRIES):
            p = git(self.top, 'fetch', '--quiet', 'origin', f'+refs/heads/{self.branch}:{self.tracking}', check=False)
            if p.returncode == 0: break
            err = p.stderr.decode(errors='replace')
            if 'lock' in err and i < FETCH_RETRIES - 1:
                time.sleep(1 + i); continue
            raise ValueError('git fetch failed: ' + err[:1200])
        self._stamp(LAST_FETCH_FILE)
        return self.remote_sha()

    def _stamp(self, name):
        try: (self.cache_dir() / name).write_text(str(time.time()) + '\n')
        except OSError: pass

    def fetched_age(self) -> float:
        """Seconds since this machine last fetched or pushed the branch (any process); inf when never."""
        try: return max(0.0, time.time() - float((self.cache_dir() / LAST_FETCH_FILE).read_text().strip()))
        except (OSError, ValueError): return float('inf')

    def remote_sha(self) -> str:
        p = git(self.top, 'rev-parse', '--verify', '-q', self.tracking, check=False)
        return p.stdout.decode().strip() if p.returncode == 0 else ''

    def ls_remote(self) -> str:
        """The branch's commit on the remote, read without transfer (watch: one round trip, no fetch when unchanged)."""
        out = git(self.top, 'ls-remote', '--heads', 'origin', self.branch).stdout.decode().split()
        return out[0] if out else ''

    # ---------------------------------------------------------------- read

    def tree(self, sha: str) -> dict:
        """{path below the board: blob} for board.json and every events/**/*.json at commit `sha`."""
        pre = self.prefix
        out = git(self.top, 'ls-tree', '-r', '-z', '--full-tree', sha, '--', pre + 'events', pre + 'board.json').stdout.decode()
        rows = {}
        for rec in out.split('\0'):
            if not rec: continue
            meta, path = rec.split('\t', 1)
            mode, typ, blob = meta.split()
            if typ != 'blob' or not path.startswith(pre): continue
            rel = path[len(pre):]
            if rel == 'board.json' or (rel.startswith('events/') and rel.endswith('.json')): rows[rel] = blob
        return rows

    def order(self, sha: str):
        log = git(self.top, 'log', '--reverse', '--diff-filter=A', '--name-only', '--format=COMMIT|%H|%cI|%an',
                  sha, '--', self.prefix + 'events').stdout.decode(errors='replace')
        return parse_log(log, self.prefix)

    def blob(self, sha: str, rel: str, blob: str) -> bytes:
        """The blob's content as the checkout's filters produce it (git-crypt: decrypted), cached by blob id."""
        c = self.cache_dir() / 'blobs' / blob
        try: return c.read_bytes()
        except OSError: pass
        data = git(self.top, 'cat-file', '--filters', f'{sha}:{self.prefix}{rel}').stdout
        if data.startswith(bc.GITCRYPT_MAGIC): raise ValueError('board is encrypted and locked (unlock the repository first)')
        tmp = c.with_name(c.name + f'.{os.getpid()}.tmp')
        tmp.write_bytes(data); os.replace(tmp, c)
        return data

    def materialize(self, sha: str, dest: Path) -> Path:
        """Write the board at commit `sha` as plain files into `dest` (board.json, events/, .order, .base)."""
        rows = self.tree(sha)
        dest.mkdir(parents=True, exist_ok=True)
        if 'board.json' in rows: (dest / 'board.json').write_bytes(self.blob(sha, 'board.json', rows['board.json']))
        lines, seen = [], set()
        for rel, c10, date, author in self.order(sha):
            # a path the log added but the tree lacks is listed too: board-view reports it as damaged history
            seen.add(rel); lines.append(f'{c10}|{date}|{author}|{rel}')
        for rel in sorted(rows):
            if rel.startswith('events/') and rel not in seen: lines.append(f'|||{rel}')
        for rel, blob in rows.items():
            if not rel.startswith('events/'): continue
            p = dest / rel; p.parent.mkdir(parents=True, exist_ok=True)
            data = self.blob(sha, rel, blob)
            try: os.link(self.cache_dir() / 'blobs' / blob, p)       # no copy: the blob cache already holds the plaintext
            except OSError: p.write_bytes(data)
        (dest / ORDER_FILE).write_text('\n'.join(lines) + ('\n' if lines else ''))
        (dest / BASE_FILE).write_text(sha + '\n')
        return dest

    def materialized(self, sha: str) -> Path:
        """The board at commit `sha` as a persistent read-only directory under the cache, built once per machine
        (hard links to the blob cache, so a thousand events cost a few milliseconds) and shared by every process:
        a commit's tree never changes, so the directory is immutable once its `.base` exists. Built into a private
        temporary name and renamed into place; older commits are pruned after TREE_TTL_S, keeping TREE_KEEP."""
        tree = self.cache_dir() / TREE_DIR; final = tree / sha; board = final / 'board'   # `board/` like a checkout's board dir
        if (board / BASE_FILE).is_file(): return board
        tree.mkdir(parents=True, exist_ok=True)
        tmp = tree / f'.tmp-{os.getpid()}-{sha}'
        if tmp.exists(): shutil.rmtree(tmp, ignore_errors=True)
        self.materialize(sha, tmp / 'board')
        try: os.rename(tmp, final)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)        # another process won the race; its copy is identical
        try:
            dirs = sorted((d for d in tree.iterdir() if d.is_dir() and not d.name.startswith('.tmp-')), key=lambda d: d.stat().st_mtime, reverse=True)
            for d in dirs[TREE_KEEP:]:
                if d != final and time.time() - d.stat().st_mtime > TREE_TTL_S: shutil.rmtree(d, ignore_errors=True)
        except OSError: pass
        return board

    # ---------------------------------------------------------------- write

    def write_blob(self, rel: str, text: bytes) -> str:
        """Store the event as a blob through the path's clean filter (git-crypt encrypts); returns the blob id."""
        return git(self.top, 'hash-object', '-w', '--path', self.prefix + rel, '--stdin', input=text).stdout.decode().strip()

    def blob_bytes(self, blob: str) -> bytes:
        return git(self.top, 'cat-file', 'blob', blob).stdout

    def commit(self, base: str, rel: str, blob: str, message: str = 'Add event') -> str:
        """A commit on top of `base` whose tree is base's tree plus the event; built in a temporary index."""
        fd, idx = tempfile.mkstemp(prefix='board-index-'); os.close(fd); os.unlink(idx)
        env = {'GIT_INDEX_FILE': idx}
        try:
            git(self.top, 'read-tree', base, env=env)
            git(self.top, 'update-index', '--add', '--cacheinfo', f'100644,{blob},{self.prefix + rel}', env=env)
            tree = git(self.top, 'write-tree', env=env).stdout.decode().strip()
            return git(self.top, 'commit-tree', tree, '-p', base, '-m', message).stdout.decode().strip()
        finally:
            try: os.unlink(idx)
            except OSError: pass

    def push(self, sha: str) -> bool:
        """Push the commit to the branch; False when the remote moved (the caller fetches and revalidates)."""
        p = git(self.top, 'push', '--quiet', 'origin', f'{sha}:refs/heads/{self.branch}', check=False)
        if p.returncode:
            err = p.stderr.decode(errors='replace')
            if 'rejected' in err or 'fetch first' in err or 'non-fast-forward' in err: return False
            raise ValueError('git push failed: ' + err[:1200])
        git(self.top, 'update-ref', self.tracking, sha, check=False)
        self._stamp(LAST_FETCH_FILE)
        return True

    def notify_local(self, sha: str) -> None:
        """Leave the pushed commit where this machine's watches look (every two seconds)."""
        try: (self.cache_dir() / NOTIFY_FILE).write_text(sha + '\n')
        except OSError: pass

    def notify(self, sha: str) -> None:
        """notify_local here, and on each host named in AGENT_BOARD_NOTIFY_HOSTS (ssh, background, fail-silent): the
        host runs this file's `--touch-notify` for the same board path under its own home, so its watches wake in
        seconds instead of at their next remote check. Paths must sit under the home directory on both machines."""
        self.notify_local(sha)
        hosts = [h.strip() for h in (os.environ.get(NOTIFY_HOSTS_ENV) or '').split(',') if h.strip()]
        if not hosts: return
        home = Path.home().resolve()
        def portable(p):   # the same path under the other machine's home when it is under ours, else as it is
            p = Path(p).resolve()
            try: return '~/' + p.relative_to(home).as_posix()
            except ValueError: return p.as_posix()
        engine, root = portable(__file__), portable(self.root)
        ssh = os.environ.get(NOTIFY_SSH_ENV) or 'ssh'
        for h in hosts:
            try:
                subprocess.Popen([ssh, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', h,
                                  f'/usr/bin/python3 {engine} --touch-notify {root} {sha}'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError: pass

    def notified(self) -> str:
        try: return (self.cache_dir() / NOTIFY_FILE).read_text().strip()
        except OSError: return ''

    def fast_forward(self, new: str, audience: str) -> bool:
        """Owner board that is a whole repository: move the checkout to `new` when HEAD is its ancestor and the tracked
        files are clean, under a lock (the check and the merge are one step; a parallel session's edits in between
        cannot be caught otherwise). Never for a board inside a shared project."""
        if self.sub or audience != 'owner': return False
        try:
            with open(self.common / 'board-ff.lock', 'w') as fh:
                if fcntl:
                    try: fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError: return False
                head = git(self.top, 'rev-parse', '--verify', '-q', 'HEAD', check=False).stdout.decode().strip()
                branch = git(self.top, 'symbolic-ref', '--short', '-q', 'HEAD', check=False).stdout.decode().strip()
                if not head or branch != self.branch: return False
                if git(self.top, 'merge-base', '--is-ancestor', head, new, check=False).returncode: return False
                if git(self.top, 'status', '--porcelain', '--untracked-files=no', check=False).stdout.strip(): return False
                return git(self.top, 'merge', '--ff-only', '--quiet', new, check=False).returncode == 0
        except (OSError, subprocess.SubprocessError, ValueError):
            return False


def _selftest() -> int:
    import json, sys
    with tempfile.TemporaryDirectory(prefix='board-transport-') as td:
        base = Path(td); remote = base / 'remote.git'; root = base / 'source'; ws = base / 'cache'
        os.environ['AGENT_BOARD_CACHE_DIR'] = str(ws)
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True)
        for k, v in [('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid')]: git(root, 'config', k, v)
        (root / 'board.json').write_text(json.dumps({'board_format': 1, 'audience': 'owner', 'encryption': 'none'}))
        git(root, 'add', 'board.json'); git(root, 'commit', '-q', '-m', 'Initialize')
        git(root, 'remote', 'add', 'origin', str(remote)); git(root, 'push', '-q', '-u', 'origin', 'main')
        r = Repo(root)
        assert r.sub == '' and r.prefix == '' and r.branch == 'main'
        sha0 = r.fetch(); assert sha0 == git(root, 'rev-parse', 'HEAD').stdout.decode().strip()
        assert r.ls_remote() == sha0
        # write two events through the object store only; the working tree and index stay untouched
        ev = lambda i: json.dumps({'event_id': f'e{i}', 'i': i}).encode()
        rel1 = 'events/p/t/20260101T000000Z--e1.json'; rel2 = 'events/p/t/20260101T000001Z--e2.json'
        b1 = r.write_blob(rel1, ev(1)); c1 = r.commit(sha0, rel1, b1); assert r.push(c1)
        assert git(root, 'status', '--porcelain').stdout == b''                       # nothing staged, nothing in the tree
        assert r.remote_sha() == c1 and r.ls_remote() == c1
        b2 = r.write_blob(rel2, ev(2)); c2 = r.commit(c1, rel2, b2); assert r.push(c2)
        stale = r.commit(c1, 'events/p/t/20260101T000002Z--e3.json', r.write_blob('x', ev(3)))
        assert not r.push(stale)                                                       # the remote moved: rejected, not raised
        rows = r.tree(c2); assert set(rows) == {'board.json', rel1, rel2}, rows
        assert [x[0] for x in r.order(c2)] == [rel1, rel2]
        d = r.materialize(c2, base / 'm')
        assert json.loads((d / rel2).read_text())['i'] == 2 and (d / BASE_FILE).read_text().strip() == c2
        lines = (d / ORDER_FILE).read_text().splitlines(); assert [l.split('|')[3] for l in lines] == [rel1, rel2], lines
        assert (ws / hashlib.sha1((r.remote + '\n').encode()).hexdigest()[:16] / 'blobs' / b1).is_file()  # cached once
        # fast-forward: HEAD is an ancestor and the tree is clean → the checkout moves; dirty tracked file → it stays
        assert r.fast_forward(c2, 'owner') and git(root, 'rev-parse', 'HEAD').stdout.decode().strip() == c2
        assert (root / rel2).is_file()
        b3 = r.write_blob('events/p/t/20260101T000003Z--e4.json', ev(4)); c3 = r.commit(c2, 'events/p/t/20260101T000003Z--e4.json', b3); assert r.push(c3)
        (root / 'board.json').write_text('{}')
        assert not r.fast_forward(c3, 'owner') and git(root, 'rev-parse', 'HEAD').stdout.decode().strip() == c2
        git(root, 'checkout', '-q', '--', 'board.json')
        assert not r.fast_forward(c3, 'collaborators')                                  # never for a collaborators board
        r.notify(c3); assert r.notified() == c3
        assert r.fetched_age() < 60                                                          # push stamped the machine
        # persistent materialisation: built once, hard-linked, reused by path; a second commit gets its own directory
        d1 = r.materialized(c3); assert (d1 / BASE_FILE).read_text().strip() == c3 and d1 == r.materialized(c3)
        f = next((d1 / 'events').rglob('*.json')); assert f.stat().st_nlink >= 2, f                # a link, not a copy
        assert d1.name == 'board' and d1.parent.parent.name == TREE_DIR and r.materialized(c2) != d1
        # remote notify: the ssh stub records what it was asked to run
        stub = base / 'ssh'; stub.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$STUB_SSH_OUT"\n'); stub.chmod(0o755)
        os.environ['STUB_SSH_OUT'] = str(base / 'ssh.out'); os.environ[NOTIFY_SSH_ENV] = str(stub); os.environ[NOTIFY_HOSTS_ENV] = 'host-a'
        saved_home = os.environ.get('HOME'); os.environ['HOME'] = str(base)      # the fixture's root is then "under home" → ~/source
        try:
            r.notify(c3)
            for _ in range(40):                                                       # the stub runs in the background
                if (base / 'ssh.out').is_file() and (base / 'ssh.out').read_text().count('\n') >= 6: break
                time.sleep(0.1)
            time.sleep(0.2)
        finally:
            os.environ['HOME'] = saved_home
        got = (base / 'ssh.out').read_text().splitlines()
        assert got[0] == '-o' and got[4] == 'host-a' and got[5].startswith('/usr/bin/python3 ') and got[5].endswith(f'--touch-notify ~/source {c3}'), got
        os.environ.pop(NOTIFY_HOSTS_ENV); os.environ.pop(NOTIFY_SSH_ENV)
        # --touch-notify = the remote end of that call
        subprocess.run([sys.executable, __file__, '--touch-notify', str(root), 'cafe0000'], check=True, env=dict(os.environ))
        assert r.notified() == 'cafe0000'
        # a board that is a subdirectory of a project: prefix in paths, project checkout never fast-forwarded
        proj = base / 'proj'; subprocess.run(['git', 'init', '-q', '-b', 'main', str(proj)], check=True)
        for k, v in [('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid')]: git(proj, 'config', k, v)
        (proj / 'board').mkdir(); (proj / 'board' / 'board.json').write_text(json.dumps({'board_format': 1, 'audience': 'collaborators', 'encryption': 'none', 'sources': ['proj']}))
        (proj / 'paper.tex').write_text('x'); git(proj, 'add', '.'); git(proj, 'commit', '-q', '-m', 'Initialize')
        remote2 = base / 'proj.git'; subprocess.run(['git', 'init', '--bare', '-q', str(remote2)], check=True)
        git(proj, 'remote', 'add', 'origin', str(remote2)); git(proj, 'push', '-q', '-u', 'origin', 'main')
        rp = Repo(proj / 'board'); assert rp.sub == 'board' and rp.prefix == 'board/'
        s0 = rp.fetch(); bp = rp.write_blob(rel1, ev(1)); cp = rp.commit(s0, rel1, bp); assert rp.push(cp)
        assert set(rp.tree(cp)) == {'board.json', rel1}
        assert git(proj, 'rev-parse', 'HEAD').stdout.decode().strip() == s0 and not (proj / 'board' / rel1).exists()
        assert not rp.fast_forward(cp, 'collaborators')
        assert parse_log('COMMIT|abc|d|a\nboard/events/p/t/x.json\nboard/other.json\n', 'board/') == [('events/p/t/x.json', 'abc', 'd', 'a')]
        assert transport() in ('fetch', 'clone')
    print('board_transport selftest: ok')
    return 0


if __name__ == '__main__':
    import sys
    if '--selftest' in sys.argv: sys.exit(_selftest())
    if len(sys.argv) == 4 and sys.argv[1] == '--touch-notify':
        Repo(os.path.expanduser(sys.argv[2])).notify_local(sys.argv[3]); sys.exit(0)
    sys.exit(0)
