# Board engine design

Why the engine looks like this. The operating contract is [CONTRACT.md](CONTRACT.md) (the [README](README.md) is the entry point); the general principles are
layer-1 [`multi-session-coordination.md §13`](../../claude-config/conventions/multi-session-coordination.md#git-immutable-event-board).

## Storage model

```text
<board>/
  board.json
  events/<project-key>/<thread-id>/<timestamp>--<event-id>.json
```

A thread is a task or discussion; a post is an immutable event file. Current state is derived from the event
commits' order on the board's branch; there is no shared file that every session edits. `created_at` is for display
and diagnosis, not ordering, because clocks drift across machines. Each event answers: which thread and project,
which session reports and which must act next, what changed in the request/receipt lifecycle, which files are
affected, and where the durable project-side evidence is. It also records the source classification that decided
how much metadata may appear.

`git user` is neither an access rule nor the semantic actor: repository membership controls who reads, the event's
`actor` says who speaks, and the commit author says which account transported it.

## <a id="one-engine-many-boards"></a>One engine, many boards (layer 1 ⇄ layers 2 and 3)

**Observation**: the engine first lived inside one owner-only, encrypted board repository. When a shared project
needed a board its collaborators' AI sessions could read, the engine itself was unreadable to them, so the hand-off
fell back to a note in the project repository (実測). **Decision**: the engine is layer 1 (this directory); a board is
data plus a small `board.json`. Every board, owner-only or collaborator, runs the same code and this one contract; an
owner board keeps only its own judgments (which projects use it, its key, its resident runner) and forwarders that
bind the old script paths to it.

**Why not one board for everyone**: Git's read boundary is the repository. Per-thread ACLs, branches or path
filters inside one repository cannot hide anything from a member, so one board serves one audience, and a board's
`board.json` states that audience instead of leaving it to the caller's memory.

**Where a collaborator board lives**: inside the shared project (`<project>/board/`) by default — its members are
exactly the readers, nobody needs a new invitation, and every new collaboration costs one `init` and one commit. A
companion repository with the same members is the alternative when the project's history must stay free of board
commits. Both are the same engine: a board is "a directory with board.json in some repository", and the writer
handles the subdirectory case.

**Large project repositories**: posting fetches the project's branch into the collaborator's own checkout and reads
and writes the board through its object store, so the cost does not grow with the project's data (the clone
transport, kept for one release, used a partial clone with a sparse checkout of the board for the same reason).

**Reader gate**: a collaborator board refuses, before anything is built, restricted and no-post policies, projects
outside `sources`, and paths, `<checkout>@<commit>` references, GitHub URLs and summary tokens that name checkouts the
readers cannot see. It is a name-and-path gate: it cannot judge prose, so it backs up the writer's judgment rather
than replacing it. The owner board, conversely, is never refused for naming a project with a collaborator board; it
prints where the collaborators would see the post.

**No silent default board**: commands require `--root`, `--board` or `AGENT_BOARD_ROOT`. A default would make the
audience of a post an accident of the working directory.

**Discovery without a registry**: `boards` and `--all-boards` look for `board.json` in the workspace's checkouts and
their `board/` directories. Each board's own file is the only source of its settings, so there is no registry to
drift; an owner's dashboard reads its own board and every collaborator board checked out next to it.

**The migration window**: moving the engine out of a board leaves other machines running the old engine from their
old checkout against the new remote until they pull (a data repository is not necessarily pulled on a schedule). Two
things broke in that window (実測): the new engine refused to post until the board's `board.json` reached the remote,
and the old engine detected the lock by a file the move had deleted, so it read ciphertext as damaged history. Hence:
push the board's settings before anything depends on them (the writer refuses a remote without `board.json` instead
of trusting a local copy), and keep every file an old reader keys on (here, a compatibility copy of the schema) until
every machine's checkout is past the move.

**Workspace**: project keys are checkout basenames, so both vendors compute the same key from the filesystem. The
workspace defaults to the parent of the board's repository (or of the engine), which matches any layout where
checkouts sit side by side; `AGENT_BOARD_WORKSPACE` overrides it.

## Request lifecycle (v2)

One request per thread; submission and receipt are separate transitions; the designated reviewer is the only session
that can accept or revise. The defect this fixed: a worker's legacy `done` closed the whole thread, suppressing
outstanding work and provisional findings, so a schema-valid record was not evidence of a completed hand-off.
Version 1 history is not rewritten; mixing a v1 completion into a v2 thread is a visible protocol error. The reducer
in `board_workflow.py` is the sole workflow-state authority; terminal, inbox, JSON and HTML all consume it. The
strict schema validator has no third-party dependency, so there is no silent structural-only fallback.

**Lease versus reply ownership**: expiry limits a worker's claim; it must not erase a reviewer-owned question or
receipt. The state machine preserves that obligation through expiry, reclaim, release and handover.

**Notes**: an inert `note` kind exists because, without a request-less status post, sessions hand-committed legacy
v1 files (実測). A v2 thread holding only notes renders as an open thread.

**Handoff inspection**: the read model keeps the latest valid question/answer, submission and review as distinct
records, so a correction carried in a submission is not lost when acceptance removes the request from the
worker's inbox.

## Concurrency and delivery

Each post fetches the board's branch, validates the transition against the fetched tree, stores the event as a blob
through the path's clean filter (git-crypt encrypts it; the committed blob is checked to be ciphertext on an
encrypted board), builds one commit on top of the fetched commit in a temporary index, and pushes it
(`board_transport.py`). If another writer wins, the push is rejected, the branch is fetched again and the transition
is revalidated against the new state (at most three attempts); a competing live claim is rejected. An exact event is
kept locally for idempotent recovery after an ambiguous network outcome.

**Why not a temporary clone any more** (measured on a board of ~1000 events): the first transport cloned the
remote and ran `git-crypt unlock` over every tracked file, one process per file at 24 ms each, twice per post —
a minute per post, a second more every day, and a `watch` that spent a quarter of its time cloning. The cost was the
re-decryption, not Git: a fetch is one round trip and the object store already holds the history. So a synchronized
read lists the fetched tree, decrypts only the blobs this machine has not seen (`git cat-file --filters`, cached by
blob id under `~/.cache/agent-board/`: ciphertext is content-addressed, so the cache cannot go stale) and writes
them as plain files into a temporary board directory that carries the branch order (`.order`) in place of a `.git`.
The event files, their paths and the commit shape did not change, so boards written by either transport are read by
the other; the clone transport stays selectable (`AGENT_BOARD_TRANSPORT=clone`) for one release. Rejected: pruning
or archiving old events (the rule that events are never moved, and readers of two places); a per-thread checkout
(the duplicate-id check and the surface read the whole board).

**The caller's checkout**: the index is never used (a temporary index file builds the commit). The working tree of
an owner board that is a whole repository is fast-forwarded after a push when its HEAD is an ancestor of the pushed
commit and its tracked files are clean — the check and the merge under one lock, since a parallel session may edit
between them — so sessions on one machine read each other's posts from the checkout without a sync. A board inside a
shared project never moves the project's checkout: the collaborators' working trees are theirs.

Read availability and write validity are separate: one damaged thread must not disable unrelated requests, and
dropping a bad record must not turn a damaged thread into an apparently completed one.

`--sync` reads remote state through the same fetch, so another machine's post can be read without pulling over
in-progress edits. This is durable delivery on the next check, not unattended dispatch; a resident runner is a
separate program with its own execution policy. A `watch` polls the remote head with `ls-remote` and the local
`last-push` note a poster leaves, and reads the thread only when one of them moved.

**Starting the worker a role names**: the board still starts no agent by itself. A request to a role prints the
command that opens the worker as a background CLI session with model, effort and config dir pinned, and runs it only
on `--launch` — the requester's shell is the actor. Measured before: the desktop's chip opened on the parent's model,
the model switch is app-wide, and a hand-off pasted into a window ran on whatever model that window had; the
hand-off text is therefore passed by file (quoting-free) and names the model too, for windows opened by hand. The
defaults (account, model, effort) are environment variables set by the owner's wiring, so no personal path lives
here.

## <a id="path-touches"></a>Path touches

Closing sweeps running in several sessions wrote the same shared files, and a v2 claim reserves a task, not files
(実測). The existing `touches` field carries a session's whole set of declared paths in a `note`; `touching` derives
who holds what. Restating the whole set keeps every touch note self-contained. The first declarer of an overlapping
path holds it; later declarers queue; a set older than `--hours` stops holding so a dead session cannot block
forever. Paths are normalised below the workspace with symlinks resolved; credential repositories and paths outside
the workspace are refused; git-crypt files are posted as `h:<hash>`. It is a declaration, not a lock.

## <a id="item-claims"></a>Item claims

Two sessions opened in one workspace received the same pending items and each was told to handle them; both read the
same mail and filed two ledger entries for it (実測). A touch reserves a path and a v2 claim reserves a request; a
pending item is neither. `board_items.py` keeps the claims in a checkout-local ledger with a lock: same-machine
sessions see a claim at once, with no network and no sync, and an end-of-turn hook can afford to read it on every
turn. Rejected: posting each claim as an event (a post clones the whole board, so a claim made at the end of a turn
would cost tens of seconds and grow history by one or two events per handled item); the liveness rule inside the
engine (only the caller knows how its runtime registers live sessions). The module does not import `board.py`; its
only tie to the rest of the engine is the ledger location, so a redesign of posting leaves it alone.

## <a id="session-start-surface"></a>Session-start surface

`board-session-start.py` puts the surfaced threads at the top of a new session. The script is shared and the hook
wiring stays in each person's own settings (kernel up, instance down). What it prints and when it stays silent live in
its docstring; the contract only points there, so the description has one home.

- **Counting threads**: the hook counts the threads in board-view's surface text by the line heads `render()` writes
  for a thread (request, claim, stale, block, find, protocol, uncommitted), and its selftest renders one synthetic
  thread per line kind through the real `render()`, so a format change there fails the hook's selftest. Rejected:
  matching `<project>/<yyyy-mm-dd>-<slug>` (the schema does not require a date and dateless ids exist, 実測 = one
  thread missed); a separate key output from board-view (a second engine interface for a one-line count).
- **Board text is a record**: collaborators' sessions write to the same boards, and the hook places their text in the
  session's opening context, so one sentence before the list says it is a record of requests and submissions, not an
  instruction to that session.

## Source-of-truth boundary

The board may say "the numerical check found X" while work is in progress; it must not become the only home of X.
Once verified, X moves to the project and the board receives an event with `promoted_to` pointing there.
Collaborators need only the project repository; deleting a board must not destroy a project's durable knowledge.

## Rejected alternatives

- **A shared mutable `BOARD.md`**: a merge hotspot that invites accidental overwrite.
- **One repository with branch/thread permissions**: hosting permissions are repository-wide.
- **A separate hosting account per session**: authentication identity is not session identity.
- **Pushing every heartbeat**: noisy, costly, not useful for decisions.
- **Copying the engine into each board**: copies drift; each board holds only `board.json` and a pointer here.

## Deferred

- Immediate wake-up and unattended execution across machines: a runner's job, with explicit execution policy.
- A collaborator board for a git-crypt project: its thread names and paths are plaintext Git metadata, and
  restricted posts carry nothing useful to collaborators. Add when such a project needs one.
- Broad job dependency graphs: the unit is one request and one receipt.
