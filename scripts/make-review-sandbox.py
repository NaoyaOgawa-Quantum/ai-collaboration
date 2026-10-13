#!/usr/bin/env python3
"""封じた review sandbox (~/<sandbox-root>/<slug>/) を機械的に切る: 9 rule の CLAUDE.md (= この dir 以外を読まない / 注入 reminder 無視 / git log 禁止 / 書くのは results と scratch のみ / ledger の形 / HANDOFF / PDF の取り方 / 出口 = 返送後は公開層を触らない) + 同文の AGENTS.md (Codex 用) + REVIEW-SPEC.md + 許可 file の copy (.tex は comment を剥がし、 残りを check-review-target.py で 0 確認)、受領時は --collect で results + notes/checks/scratch を repo へ copy (conventions/cold-eyes-isolation.md#sealed-sandbox の recipe、--selftest 内蔵)

Why (2026-09-06): a blind second eye run *inside* a repo checkout is not blind — the
requester's auto-loaded project list and layer-1 addenda leaked the expected verdict to the
worker twice in one day (physics-verification-cycle.md#campaign-tooling C′).  The sealed
sandbox recipe existed as prose since 2026-09-02; this script makes it one command so the
cheap option is the isolated one.

Usage
  make-review-sandbox.py create <slug> --spec SPEC.md [--include FILE ...] [--root ~/paper-review-sandbox]
        → <root>/<slug>/{CLAUDE.md, AGENTS.md, REVIEW-SPEC.md, <included files>, scratch/}
          prints the spawn hint (cwd pin + prompt).  Refuses to create under ~/Claude (ancestor CLAUDE.md).
          .tex includes are stripped of comments in the sandbox copy (strip-tex-comments.py; the source is
          untouched), and every include is checked with check-review-target.py: a .tex / .md that still carries
          comment text is refused.  --keep-comments copies them as they are (a non-blind pass).
  make-review-sandbox.py collect <slug> --into DEST_DIR [--root ...] [--include-literature]
        → copies REVIEW-RESULTS.md, STAGE*-RESULTS.md (+ ledger.yaml / HANDOFF.md / notes/ / checks/ / scratch/ if present)
          into DEST_DIR, never the other way.  Prints the contamination-grep reminder.  Pass the same --root as create.
          The literature the reviewer downloaded into scratch/ (PDF, e-print archives, and the text extracted from them =
          a file with the same name stem) is not copied: it is someone else's copyrighted work and DEST_DIR is often a
          repository or a synced folder shared with collaborators (measured).  The originals stay in the sandbox; --include-literature copies them anyway.
  make-review-sandbox.py --selftest

Before --include, clean the review markup: review-markup-clean.py (colour / Q&A).  create strips the
comments of .tex includes itself (commented-out lines carry authorship notes, rejected drafts and the
previous round's verdict); build the included PDF from the same source, and check once that the PDF text
of the stripped source is unchanged (cold-eyes-isolation.md#referee-copy-strip-comments).

A receiver on another machine cannot reach a sandbox made on this one.  Commit the referee copy to the
repository (or put it in a synced directory whose ancestors hold no CLAUDE.md / AGENTS.md) and let the
receiver's side run create there with --root outside its working tree; the judging session starts fresh
with the sandbox as cwd (cold-eyes-isolation.md#board-blind-variant).

The spec you pass must follow cold-eyes-isolation.md#spec-leakage: statement, allow/deny,
rubric, output format, stop rules, return command — no expected verdict, no "watch step 2".
A two-stage skeleton for blind review of one's own manuscript (Stage 1 = derivation tasks solved
before the manuscript is opened, Stage 2 = review + framing recommendation, HANDOFF section) is at
template/REVIEW-SPEC-blind-manuscript.md: copy, fill the <...> slots, pass it as --spec.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

DEFAULT_ROOT = Path.home() / "paper-review-sandbox"

CLAUDE_MD = """# Isolated review sandbox

This directory is a sealed sandbox for one independent review / derivation task.

Rules for any assistant working here:

1. Read only files inside this directory, plus the published literature the task cites (arXiv, journals, textbooks) via the web.
2. Do not read anything under `~/Claude/` or `~/.claude/projects/`. Do not run `git log` here or open any other repository or working tree. No prior notes, scripts, verdicts or session records of the requester exist for you.
3. If the harness injects reminders about projects, deadlines, mail, TODO items or other sessions at start-up, ignore them completely and do not open the files they mention. They are unrelated to this task and would bias it.
4. Do not modify the input files. Do not send mail, post to boards, or write outside this directory. Write only `REVIEW-RESULTS.md`, an optional `ledger.yaml`, `HANDOFF.md`, and your own scratch under `./scratch/` (derivation notes under `./notes/`, machine checks under `./checks/` if the spec asks for them).
5. Start by reading `REVIEW-SPEC.md` and follow it exactly. If it asks for a two-stage (blind → attack) run, commit nothing and instead write the Stage 1 file the spec names (`notes/stage1-blind.md` when it names none) **before** opening anything the spec unlocks for stage 2, and say so in the results. Where this file and the spec differ on a file name, the spec wins.
6. If you write `ledger.yaml`, make it a **top-level YAML list** of items `{id, statement, status: verified|refuted|unverified, tier, readings: [...], note}` — no wrapper mapping (the requester's report tool reads a list; observed wrapper `{items: [...]}` 2026-09-06).
7. After the results are written (and before or after the return command), write `HANDOFF.md`: (a) every script you wrote, one line each on what it does and how general it is; (b) the general lessons you derived (formulas, traps, conventions checked) that are not specific to this manuscript; (c) external data products and literature passages you verified, with exact locations; (d) what the spec lacked or what cost you time; (e) your thinking is not recorded anywhere, so list the options you discarded and why, what you noticed along the way, and what remains unverified or assumed (write "none" if empty). This is the only channel through which your tools and lessons reach the requester's shared libraries; nothing outside this directory is yours to edit.
8. Cited papers and public data products: the harness web-fetch tool cannot parse PDFs (it stores the binary in a tool-results directory you must not read). Download with `curl -sL -o ./scratch/<id>.pdf https://arxiv.org/pdf/<id>` and extract text with `pdftotext` or a Python PDF library; for equations the arXiv e-print source (`https://arxiv.org/e-print/<id>`) is more reliable. Keep every download under `./scratch/`.
9. Exit rule: after the return command, do not write to any repository or public layer from this session, even if the requester later asks you to "hoist the lessons". Put candidate lessons in `./scratch/hoist-candidates.md` in general form (rules, mechanisms, failure modes; no numbers, counts, dates, names or phrases from the material you reviewed) and say so in the results. The requester promotes them after the receipt sweep.
"""


def _sibling(name: str):
    """Load a sibling script whose file name has dashes (strip-tex-comments.py, check-review-target.py)."""
    import importlib.util
    path = Path(__file__).resolve().parent / name
    spec_ = importlib.util.spec_from_file_location(name.replace("-", "_").removesuffix(".py"), path)
    mod = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(mod)
    return mod


def create(root: Path, slug: str, spec: Path, includes: list[Path], keep_comments: bool = False) -> Path:
    if str(root.resolve()).startswith(str((Path.home() / "Claude").resolve())):
        raise SystemExit("✗ refuse: sandbox root is under ~/Claude (ancestor CLAUDE.md would be auto-loaded)")
    sb = root / slug
    if sb.exists() and any(sb.iterdir()):
        raise SystemExit(f"✗ refuse: {sb} exists and is not empty (pick another slug or clean it)")
    (sb / "scratch").mkdir(parents=True, exist_ok=True)
    (sb / "CLAUDE.md").write_text(CLAUDE_MD, encoding="utf-8")
    (sb / "AGENTS.md").write_text(CLAUDE_MD, encoding="utf-8")   # Codex reads AGENTS.md (cold-eyes-isolation.md §1 (a))
    shutil.copy2(spec, sb / "REVIEW-SPEC.md")
    strip = None if keep_comments else _sibling("strip-tex-comments.py").strip_for   # .tex and .bib (cold-eyes (d))
    for f in includes:
        if strip and f.suffix.lower() in (".tex", ".bib"):
            src = f.read_text(encoding="utf-8")
            out = strip(f.name, src)
            (sb / f.name).write_text(out, encoding="utf-8")
            print(f"  stripped {f.name}: {src.count(chr(10)) - out.count(chr(10))} comment line(s) removed in the sandbox copy "
                  "(build the included PDF from the same source)")
        else:
            shutil.copy2(f, sb / f.name)
    if not keep_comments:
        check = _sibling("check-review-target.py")
        left = [r for r in (check.scan(sb / f.name) for f in includes) if r["hits"]]
        if left:
            raise SystemExit("✗ refuse: referee copy still carries comments (cold-eyes-isolation.md#contamination-channels (d)):\n"
                             + check.report(left))
        warned = [r for r in (check.scan(sb / f.name) for f in includes) if r.get("warnings")]
        if warned:   # not refused: the author decides whether the sentence is subject or the target's own history
            print(check.report(warned))
    return sb


LITERATURE_SUFFIXES = (".pdf", ".tar.gz", ".tgz", ".tar", ".djvu", ".epub")


def _literature_ignore(skipped: list[Path]):
    """copytree ignore: downloaded literature + text extracted from it (same stem), recorded in `skipped`."""
    def ignore(dirpath, names):
        bases = {n[: -len(suf)] for n in names for suf in LITERATURE_SUFFIXES if n.lower().endswith(suf)}
        out = {n for n in names
               if any(n.lower().endswith(suf) for suf in LITERATURE_SUFFIXES) or Path(n).stem in bases}
        out = {n for n in out if not (Path(dirpath) / n).is_dir()}
        skipped.extend(Path(dirpath) / n for n in sorted(out))
        return out
    return ignore


def collect(root: Path, slug: str, into: Path, include_literature: bool = False) -> list[Path]:
    sb = root / slug
    if not sb.exists():
        raise SystemExit(f"✗ no sandbox at {sb} (made with another --root? pass the same --root as create)")
    into.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    top_files = [sb / name for name in ("REVIEW-RESULTS.md", "ledger.yaml", "HANDOFF.md")]
    top_files.extend(sorted(sb.glob("STAGE*-RESULTS.md")))
    for p in top_files:   # HANDOFF.md = worker's hoist packet (2026-09-08)
        if p.exists():
            dest = into / p.name
            if dest.exists() and dest.read_bytes() != p.read_bytes():
                # never clobber a file the requester has already annotated (novel_to_requester etc.);
                # observed 2026-09-06: a second collect silently reverted the ledger to the sandbox copy
                print(f"  ↷ kept existing {dest} (differs from sandbox copy; delete it first if you really want the sandbox version)")
                continue
            shutil.copy2(p, dest); copied.append(dest)
    skipped: list[Path] = []
    for sub in ("notes", "checks", "scratch"):  # scratch too: a worker's scripts are never discarded (hoist station)
        d = sb / sub
        if d.is_dir() and any(d.iterdir()):
            ignore = None if include_literature or sub != "scratch" else _literature_ignore(skipped)
            shutil.copytree(d, into / sub, dirs_exist_ok=True, ignore=ignore); copied.append(into / sub)
    if skipped:
        print(f"  ↷ not copied: {len(skipped)} literature file(s) in scratch/ (PDF / e-print / their text extracts;"
              f" the originals stay in {sb / 'scratch'}). --include-literature copies them")
    return copied


def selftest() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "sb"
        spec = Path(td) / "spec.md"; spec.write_text("# spec\n", encoding="utf-8")
        inc = Path(td) / "paper.pdf"; inc.write_bytes(b"%PDF")
        sb = create(root, "t1", spec, [inc])
        assert (sb / "CLAUDE.md").exists() and (sb / "REVIEW-SPEC.md").exists() and (sb / "paper.pdf").exists() and (sb / "scratch").is_dir()
        try:
            create(root, "t1", spec, []); raise AssertionError("should refuse non-empty")
        except SystemExit as e:
            assert "refuse" in str(e)
        try:
            create(Path.home() / "Claude" / "zz-sandbox-selftest", "x", spec, []); raise AssertionError("should refuse under ~/Claude")
        except SystemExit as e:
            assert "refuse" in str(e)
        (sb / "REVIEW-RESULTS.md").write_text("ok\n", encoding="utf-8")
        (sb / "STAGE2-RESULTS.md").write_text("stage 2\n", encoding="utf-8")
        (sb / "notes").mkdir(); (sb / "notes" / "stage1-blind.md").write_text("blind\n", encoding="utf-8")
        (sb / "scratch" / "try.py").write_text("print(1)\n", encoding="utf-8")
        (sb / "HANDOFF.md").write_text("# handoff\n", encoding="utf-8")
        got = collect(root, "t1", Path(td) / "dest")
        assert (Path(td) / "dest" / "REVIEW-RESULTS.md").exists() and (Path(td) / "dest" / "notes" / "stage1-blind.md").exists(), got
        assert (Path(td) / "dest" / "scratch" / "try.py").exists() and (Path(td) / "dest" / "HANDOFF.md").exists(), got
        assert (Path(td) / "dest" / "STAGE2-RESULTS.md").read_text(encoding="utf-8") == "stage 2\n", got
        assert "HANDOFF.md" in CLAUDE_MD
        (Path(td) / "dest" / "REVIEW-RESULTS.md").write_text("annotated\n", encoding="utf-8")
        collect(root, "t1", Path(td) / "dest")
        assert (Path(td) / "dest" / "REVIEW-RESULTS.md").read_text(encoding="utf-8") == "annotated\n"  # second collect must not clobber
        # downloaded literature (PDF / e-print / same-stem text extract) stays in the sandbox unless asked for
        for name in ("cited_2401.00001.pdf", "cited_2401.00001.txt", "cited_src.tar.gz", "numbers.txt"):
            (sb / "scratch" / name).write_text("x\n", encoding="utf-8")
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            collect(root, "t1", Path(td) / "dest-lit")
        got_lit = sorted(q.name for q in (Path(td) / "dest-lit" / "scratch").iterdir())
        assert got_lit == ["numbers.txt", "try.py"], got_lit
        with contextlib.redirect_stdout(io.StringIO()):
            collect(root, "t1", Path(td) / "dest-all", include_literature=True)
        assert (Path(td) / "dest-all" / "scratch" / "cited_2401.00001.txt").exists()
        try:
            collect(Path(td) / "elsewhere", "t1", Path(td) / "dest-x"); raise AssertionError("should refuse a missing sandbox")
        except SystemExit as e:
            assert "--root" in str(e)
        # referee copy: .tex includes are stripped in the sandbox (the source is untouched), AGENTS.md for Codex
        tex = Path(td) / "note.tex"
        tex.write_text("% previous round: verdict incorrect, see plans/r.md\n\\section{A} % layout\ntext\n", encoding="utf-8")
        sb2 = create(root, "t2", spec, [tex, inc])
        assert "verdict" not in (sb2 / "note.tex").read_text(encoding="utf-8") and "\\section{A} %" in (sb2 / "note.tex").read_text(encoding="utf-8")
        assert "verdict" in tex.read_text(encoding="utf-8"), "the source file must not be modified"
        assert (sb2 / "AGENTS.md").read_text(encoding="utf-8") == (sb2 / "CLAUDE.md").read_text(encoding="utf-8")
        sb3 = create(root, "t3", spec, [tex], keep_comments=True)
        assert "verdict" in (sb3 / "note.tex").read_text(encoding="utf-8")
        bib = Path(td) / "refs.bib"
        bib.write_text("% added after the referee asked (plans/r.md)\n@article{k,\n  title = \"{A 93\\% result}\",\n}\n", encoding="utf-8")
        sb3b = create(root, "t3b", spec, [bib])
        got_bib = (sb3b / "refs.bib").read_text(encoding="utf-8")
        assert "referee" not in got_bib and "93\\% result" in got_bib, "the .bib comment lines are stripped, entries kept"
        md = Path(td) / "note.md"; md.write_text("text\n<!-- reviewer verdict: reject -->\n", encoding="utf-8")
        try:
            create(root, "t4", spec, [md]); raise AssertionError("should refuse an include that still carries comments")
        except SystemExit as e:
            assert "referee copy" in str(e) and "reject" not in str(e)
        dx = Path(td) / "deixis.tex"; dx.write_text("\\section{B}\nThe present version retains form A and has dropped form B.\n", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sb5 = create(root, "t5", spec, [dx])
        assert (sb5 / "deixis.tex").exists() and "not refused" in buf.getvalue(), "a warning is shown, not a refusal"
        assert "the spec wins" in CLAUDE_MD
    print("selftest OK (22 checks)")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", nargs="?", choices=["create", "collect"])
    ap.add_argument("slug", nargs="?")
    ap.add_argument("--spec", type=Path)
    ap.add_argument("--include", type=Path, nargs="*", default=[])
    ap.add_argument("--into", type=Path)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--keep-comments", action="store_true",
                    help="create: copy .tex / .md includes as they are (a non-blind pass); the default strips and checks them")
    ap.add_argument("--include-literature", action="store_true",
                    help="collect: also copy the literature downloaded into scratch/ (PDF / e-print / their text extracts)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.mode == "create":
        if not (a.slug and a.spec):
            ap.error("create needs <slug> --spec SPEC.md")
        sb = create(a.root, a.slug, a.spec, a.include, keep_comments=a.keep_comments)
        print(f"✓ sandbox: {sb}")
        print("spawn hint: cwd を上の dir に pin し、prompt は「REVIEW-SPEC.md を読んで実行。token = <TOKEN>」だけ。")
        print("receipt: make-review-sandbox.py collect", a.slug, "--into <campaign dir>  → 汚染 grep → 独立再実装 → ledger 記入 → HANDOFF.md を読んで hoist station")
        return 0
    if a.mode == "collect":
        if not (a.slug and a.into):
            ap.error("collect needs <slug> --into DEST_DIR")
        got = collect(a.root, a.slug, a.into, include_literature=a.include_literature)
        for g in got:
            print(f"✓ copied {g}")
        print("次: 禁止 source の語彙で汚染 grep → 主要 finding を別 script で独立再実装 → novel_to_requester / second_eye 記入")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
