#!/usr/bin/env python3
"""2 版の LaTeX build を機械比較する組版 gate: log から error / 未定義参照 / overfull を折返し復元して数え、.aux の label→頁 写像の drift を出し (= 総頁数が同じでも中の頁割りは動く)、配布 PDF を自分の build が再現するかを pdftotext で証明する。--selftest 内蔵、規律 = conventions/edit-intent-record.md#build-gate

4 つの独立した機能。 どれも他版・他者の成果物を読む前に回す。

1. **log 比較** (`--before-log / --after-log`): error 数・未定義参照・未定義引用・多重定義・頁数・
   overfull/underfull を両版で出し、 **後版で新たに出たものだけ**を名指しする。 改稿で増えた組版警告と
   元から在ったものを混同しない。 ⚠️ TeX の log は 79 桁で折り返すので、 素の grep は
   `Overfull \hbox (N.NNpt too wide) in paragraph at lines A--B` を途中で切る。 本 script は
   折返しを復元してから数える (= 素の grep より多く見つかる)。

2. **label→頁 の drift** (`--before-aux / --after-aux`): `\newlabel` から label ごとの (番号, 頁) を
   取り、 版間で動いた label を出す。 **総頁数の一致は組版が動いていないことの証拠にならない** —
   記号幅の変わる一括置換では頁数が同じまま中の頁割りだけ動き、 図表・付録の位置が変わる。
   番号が動いた label は参照の意味が変わるので `[NUMBER]` として別に出す。

3. **build の再現証明** (`--reproduce given.pdf --built built.pdf`): 配布された PDF と自分の build の
   `pdftotext -layout` を比較し、 差分行数を出す。 **0 行を確かめてから**、 以後の観察 (頁の見た目・
   overfull・行かぶり) を source に帰属してよい。 0 行でないなら、 見ているものは相手の版ではなく
   自分の pipeline の産物かもしれない (engine 差の実例 = claude-config latex.md#latexmk-pdf-overrides-rc)。

4. **文字の多重集合** (`--chars before.pdf after.pdf`): 2 つの PDF の文字を数で比べる (空白・accent・
   行末の hyphenation は除く)。 記号を飾るだけの pass (hat・太字の macro) なら 0 になる = 飾り以外の
   文字が 1 つも変わっていない証拠。 改名する pass (文字が変わる) には使えない。

使い方:

    python3 compare-tex-builds.py --before-log before/main.log --after-log after/main.log \
        --before-aux before/main.aux --after-aux after/main.aux
    python3 compare-tex-builds.py --reproduce distributed.pdf --built mybuild/main.pdf
    python3 compare-tex-builds.py --chars before/main.pdf after/main.pdf
    python3 compare-tex-builds.py --build-cmd 'pdflatex -interaction=nonstopmode main'   # 参考出力のみ

`--strict` で、 後版にだけ在る error / 未定義参照 / 多重定義があれば exit 1 (overfull は既定で
gate にしない = `edit-intent-record.md#overfull-not-a-gate`、 投稿直前の final pass で 1 回だけ掃除する)。
`--strict-overfull` を足すと overfull の新規発生も FAIL にする。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

WRAP = 79  # pdftex wraps log lines at this width


# ---------------------------------------------------------------- log


def unwrap(text: str) -> list[str]:
    """Rejoin pdftex's hard-wrapped log lines so that a long warning is one string again."""
    out: list[str] = []
    buf = ""
    for ln in text.split("\n"):
        buf += ln
        if len(ln) < WRAP:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def parse_log(text: str) -> dict:
    lines = unwrap(text)
    pages = re.findall(r"Output written on .*?\((\d+) pages?", text)
    return {
        "errors": [l for l in lines if l.startswith("!")],
        "undefined_refs": [l for l in lines if "Reference" in l and "undefined" in l],
        "undefined_cites": [l for l in lines if "Citation" in l and "undefined" in l],
        "multiply_defined": [l for l in lines if "multiply defined" in l or "multiply-defined" in l],
        "overfull": [l for l in lines if l.startswith("Overfull")],
        "underfull": [l for l in lines if l.startswith("Underfull")],
        "pages": int(pages[0]) if pages else None,
    }


def overfull_key(line: str) -> str:
    """Identity of an overfull box = its source line range (the amount drifts with wording)."""
    m = re.search(r"at lines (\d+)--(\d+)", line)
    return f"lines {m.group(1)}--{m.group(2)}" if m else line[:80]


# ---------------------------------------------------------------- aux


def parse_aux(text: str) -> dict[str, tuple[str, str]]:
    """{label: (number, page)} from \\newlabel; cleveref's `@cref` shadow entries are skipped."""
    out: dict[str, tuple[str, str]] = {}
    for m in re.finditer(r"\\newlabel\{([^}]*)\}\{\{([^{}]*)\}\{([^{}]*)\}", text):
        label = m.group(1)
        if label.endswith("@cref"):
            continue
        out[label] = (m.group(2), m.group(3))
    return out


def aux_drift(before: dict, after: dict) -> dict:
    moved_page, moved_number, added, removed = [], [], [], []
    for label, (num, page) in after.items():
        if label not in before:
            added.append(label)
            continue
        onum, opage = before[label]
        if onum != num:
            moved_number.append((label, onum, num))
        if opage != page:
            moved_page.append((label, opage, page))
    removed = [l for l in before if l not in after]
    return {"moved_page": moved_page, "moved_number": moved_number,
            "added": added, "removed": removed}


# ---------------------------------------------------------------- pdf


def pdf_text(pdf: Path) -> list[str]:
    if shutil.which("pdftotext") is None:
        raise RuntimeError("pdftotext not found (poppler); cannot prove build reproduction")
    r = subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                       capture_output=True, text=True, check=True)
    return r.stdout.split("\n")


def text_diff_count(a: list[str], b: list[str]) -> int:
    import difflib
    return sum(1 for d in difflib.unified_diff(a, b, lineterm="", n=0)
               if (d.startswith("-") or d.startswith("+"))
               and not d.startswith("---") and not d.startswith("+++"))


ACCENT_GLYPHS = set("^ˆ˜¯˙")  # spacing hat / tilde / macron / dot as pdftotext emits them


def char_multiset_diff(a: list[str], b: list[str]) -> dict[str, int]:
    """{char: count in b - count in a} over the non-space characters of two pdftotext outputs,
    ignoring accents (combining marks and the spacing accent glyphs) and end-of-line hyphenation.
    Zero = the two PDFs carry the same letters: a pass that only decorates symbols (a hat, a bold
    macro) changed nothing else. Not a proof for passes that rename (the letters change there)."""
    import unicodedata
    from collections import Counter

    def chars(lines: list[str]) -> Counter:
        t = "\n".join(lines).replace("-\n", "")
        return Counter(ch for ch in t if not ch.isspace() and not unicodedata.combining(ch)
                       and ch not in ACCENT_GLYPHS)
    ca, cb = chars(a), chars(b)
    return {k: cb.get(k, 0) - ca.get(k, 0) for k in set(ca) | set(cb) if ca.get(k, 0) != cb.get(k, 0)}


# ---------------------------------------------------------------- build


def parse_spec(spec: str) -> tuple[str | None, str]:
    """'paper.tex' -> (None, 'paper.tex');  'REV:paper.tex' -> ('REV', 'paper.tex')."""
    if ":" in spec and not Path(spec).exists():
        rev, _, f = spec.partition(":")
        return rev, f
    return None, spec


def build_one(spec: str, repo: Path, assets: Path | None, engine: str, out: Path) -> dict:
    """Typeset one version in its own directory and return the parsed log + paths.

    A revision spec (`REV:file.tex`) is extracted with `git archive`, so the build uses THAT
    revision's figures, .bst and .bib — not the live ones. Symlinking live assets into a temp
    dir would typeset "that revision's prose with today's figures", which is not the revision
    (conventions/edit-intent-record.md#frozen-revision-build).
    """
    rev, texfile = parse_spec(spec)
    out.mkdir(parents=True, exist_ok=True)
    if rev:
        tar = subprocess.run(["git", "-C", str(repo), "archive", rev],
                             capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(out)], input=tar.stdout, check=True)
        # typeset from the .tex's own directory inside the archive, so that a .bib / .bst / figure
        # sitting next to it is found the way the author's build finds it (bibtex searches the
        # working directory, not the directory of the .tex)
        workdir = out / Path(texfile).parent
        texfile = Path(texfile).name
    else:
        workdir = out
        src = Path(texfile)
        shutil.copy2(src, out / src.name)
        texfile = src.name
        if assets is None:
            # default = the .tex's own directory, so that the plain side of --build-against finds the same
            # .bib / .bst / figures as the revision side (which is typeset in its own directory) does;
            # without this the plain side reports undefined citations that the revision side does not
            assets = src.parent
        if assets:
            for f in assets.iterdir():
                if f.is_file() and f.suffix.lower() in (
                        ".png", ".jpg", ".jpeg", ".pdf", ".eps", ".bst", ".bib", ".cls", ".sty"):
                    shutil.copy2(f, out / f.name)
    stem = Path(texfile).stem
    steps = [[engine, "-interaction=nonstopmode", "-halt-on-error", texfile],
             ["bibtex", stem],
             [engine, "-interaction=nonstopmode", "-halt-on-error", texfile],
             [engine, "-interaction=nonstopmode", "-halt-on-error", texfile]]
    codes = []
    for cmd in steps:
        # the engine's terminal output can carry non-UTF-8 bytes (e.g. a .bib in latin-1);
        # it is not parsed here, so decode leniently instead of aborting the build
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, errors="replace")
        codes.append(r.returncode)
    log = workdir / f"{stem}.log"
    aux = workdir / f"{stem}.aux"
    return {"dir": workdir, "log": log, "aux": aux, "exit_codes": codes,
            "parsed": parse_log(log.read_text(encoding="latin-1")) if log.exists() else None,
            "sha256": _sha256(workdir / texfile)}


def _sha256(p: Path) -> str:
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else "-"


# ---------------------------------------------------------------- report


def report(bl: dict | None, al: dict | None, drift: dict | None,
           strict: bool, strict_overfull: bool) -> int:
    fail = 0
    if bl and al:
        print("| quantity | before | after |")
        print("|---|---|---|")
        for key, label in (("errors", "errors"), ("undefined_refs", "undefined references"),
                           ("undefined_cites", "undefined citations"),
                           ("multiply_defined", "multiply defined"),
                           ("overfull", "Overfull hbox"), ("underfull", "Underfull box")):
            print(f"| {label} | {len(bl[key])} | {len(al[key])} |")
        print(f"| pages | {bl['pages']} | {al['pages']} |")

        for key, label, gated in (("errors", "error", True),
                                  ("undefined_refs", "undefined reference", True),
                                  ("undefined_cites", "undefined citation", True),
                                  ("multiply_defined", "multiply-defined label", True),
                                  ("overfull", "Overfull hbox", strict_overfull),
                                  ("underfull", "Underfull box", False)):
            old = {overfull_key(x) if key == "overfull" else x for x in bl[key]}
            new = [x for x in al[key]
                   if (overfull_key(x) if key == "overfull" else x) not in old]
            if new:
                print(f"\n[NEW] {len(new)} {label}(s) present in after but not before:")
                for x in new:
                    print(f"   {x[:160]}")
                if strict and gated:
                    fail += len(new)

        if bl["pages"] != al["pages"]:
            print(f"\n[NEW] page count changed: {bl['pages']} -> {al['pages']}")

    if drift is not None:
        print(f"\n[info] labels: {len(drift['moved_page'])} moved page, "
              f"{len(drift['moved_number'])} changed number, "
              f"{len(drift['added'])} added, {len(drift['removed'])} removed")
        for label, o, n in drift["moved_number"]:
            print(f"[NUMBER] {label}: {o} -> {n}")
            if strict:
                fail += 1
        for label, o, n in drift["moved_page"]:
            print(f"[PAGE]   {label}: p{o} -> p{n}")
        for label in drift["removed"]:
            print(f"[GONE]   {label}")
            if strict:
                fail += 1
        if not drift["moved_page"] and not drift["moved_number"]:
            print("[info] pagination and numbering are identical "
                  "(page-count equality alone would not have shown this)")

    if fail:
        print(f"\nFAILED: {fail} gated regression(s)")
        return 1
    print("\nALL PASS" if (bl or drift) else "")
    return 0


# ---------------------------------------------------------------- selftest


def selftest() -> int:
    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(f"[{'PASS' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    # A wrapped Overfull warning: pdftex breaks it at 79 columns. The naive reader misses it.
    head = "Overfull \\hbox (3.14159pt too wide) in paragraph at lines 120--124"
    pad = "x" * (WRAP - len(head))          # forces the first physical line to be full width
    wrapped_log = (
        "This is pdfTeX\n"
        f"{head}{pad}\n"
        " []\n"
        "LaTeX Warning: Reference `eq:foo' on page 3 undefined on input line 12.\n"
        "! Undefined control sequence.\n"
        "Output written on main.pdf (12 pages, 600000 bytes).\n"
    )
    parsed = parse_log(wrapped_log)
    check("wrapped Overfull line is recovered by unwrapping  [foil for a naive grep]",
          len(parsed["overfull"]) == 1)
    check("the recovered line keeps its source range",
          overfull_key(parsed["overfull"][0]) == "lines 120--124")
    check("a naive line-by-line scan would also have seen this one (it starts the line)",
          any(l.startswith("Overfull") for l in wrapped_log.split("\n")))
    check("error count", len(parsed["errors"]) == 1)
    check("undefined reference count", len(parsed["undefined_refs"]) == 1)
    check("page count", parsed["pages"] == 12)
    check("clean log parses to zeros",
          parse_log("Output written on main.pdf (12 pages, 1 bytes).\n")["errors"] == [])

    # aux: same page count, different pagination — the case page-count equality misses
    before_aux = (r"\newlabel{eq:one}{{5}{2}{}{equation.5}{}}" "\n"
                  r"\newlabel{app:z}{{C}{9}{}{appendix.C}{}}" "\n"
                  r"\newlabel{eq:one@cref}{{[equation][5][]5}{[1][2][]2}}" "\n"
                  r"\newlabel{tab:x}{{1}{11}{}{table.1}{}}" "\n")
    after_aux = (r"\newlabel{eq:one}{{5}{2}{}{equation.5}{}}" "\n"
                 r"\newlabel{app:z}{{C}{8}{}{appendix.C}{}}" "\n"
                 r"\newlabel{tab:x}{{2}{10}{}{table.1}{}}" "\n")
    b, a = parse_aux(before_aux), parse_aux(after_aux)
    check("@cref shadow entries are skipped", "eq:one@cref" not in b and len(b) == 3)
    d = aux_drift(b, a)
    check("label that kept its page is not reported", "eq:one" not in [x[0] for x in d["moved_page"]])
    check("label that moved page is reported", ("app:z", "9", "8") in d["moved_page"])
    check("label whose number changed is reported separately",
          ("tab:x", "1", "2") in d["moved_number"])
    check("identical aux reports no drift",
          aux_drift(b, b)["moved_page"] == [] and aux_drift(b, b)["moved_number"] == [])
    check("removed label is reported", aux_drift(a | {"gone": ("1", "1")}, a)["removed"] == ["gone"])

    # text comparison
    check("identical text diffs to 0 lines", text_diff_count(["a", "b"], ["a", "b"]) == 0)
    check("one changed line diffs to 2 lines", text_diff_count(["a", "b"], ["a", "c"]) == 2)

    # strict gating: a new error fails, a new overfull does not (unless asked)
    clean = parse_log("Output written on main.pdf (12 pages, 1 bytes).\n")
    broke = parse_log("! Undefined control sequence.\n"
                      "Overfull \\hbox (1.0pt too wide) in paragraph at lines 1--2\n"
                      "Output written on main.pdf (12 pages, 1 bytes).\n")
    check("strict fails on a new error", report(clean, broke, None, True, False) == 1)
    only_of = parse_log("Overfull \\hbox (1.0pt too wide) in paragraph at lines 1--2\n"
                        "Output written on main.pdf (12 pages, 1 bytes).\n")
    check("strict does NOT fail on a new overfull by default",
          report(clean, only_of, None, True, False) == 0)
    check("--strict-overfull does fail on it", report(clean, only_of, None, True, True) == 1)

    # spec parsing (no TeX needed)
    check("plain path spec", parse_spec("paper.tex") == (None, "paper.tex"))
    check("revision spec splits on the first colon",
          parse_spec("abc123:paper.tex") == ("abc123", "paper.tex"))

    # end-to-end: a revision build must use THAT revision's assets, not the live ones
    if shutil.which("pdflatex") and shutil.which("git"):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            run_g = lambda *c: subprocess.run(["git", "-C", str(repo), *c],
                                              capture_output=True, check=True)
            run_g("init", "-q")
            run_g("config", "user.email", "t@t")
            run_g("config", "user.name", "t")
            (repo / "inc.tex").write_text("OLD-ASSET", encoding="utf-8")
            (repo / "m.tex").write_text(
                "\\documentclass{article}\\begin{document}\\input{inc}\\end{document}\n",
                encoding="utf-8")
            run_g("add", "-A")
            run_g("commit", "-q", "-m", "v1")
            rev = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, check=True).stdout.strip()
            (repo / "inc.tex").write_text("NEW-ASSET", encoding="utf-8")  # live tree moves on
            built = build_one(f"{rev}:m.tex", repo, None, "pdflatex", Path(td) / "out")
            txt = ""
            if (built["dir"] / "m.pdf").exists() and shutil.which("pdftotext"):
                txt = "\n".join(pdf_text(built["dir"] / "m.pdf"))
            check("revision build typesets that revision's asset, not the live one  [foil]",
                  "OLD-ASSET" in txt and "NEW-ASSET" not in txt)
            check("revision build produces a parsable log", built["parsed"] is not None)
            check("revision build records the source sha256", len(built["sha256"]) == 16)
            # a .tex in a sub-directory: the revision side runs in that directory, the plain side copies
            # that directory's assets by default -> both find the sibling .sty
            (repo / "sub").mkdir()
            (repo / "sub" / "mark.sty").write_text("\\newcommand\\subdirmarker{SUBDIR-ASSET}\n", encoding="utf-8")
            (repo / "sub" / "s.tex").write_text(
                "\\documentclass{article}\\usepackage{mark}\\begin{document}\\subdirmarker\\end{document}\n",
                encoding="utf-8")
            run_g("add", "-A")
            run_g("commit", "-q", "-m", "v2")
            rev2 = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()
            b_rev = build_one(f"{rev2}:sub/s.tex", repo, None, "pdflatex", Path(td) / "o2")
            b_plain = build_one(str(repo / "sub" / "s.tex"), repo, None, "pdflatex", Path(td) / "o3")
            for label, b in (("revision", b_rev), ("plain (no --assets)", b_plain)):
                t = ""
                if (b["dir"] / "s.pdf").exists() and shutil.which("pdftotext"):
                    t = "\n".join(pdf_text(b["dir"] / "s.pdf"))
                check(f"{label} build of a sub-directory .tex finds the sibling .sty", "SUBDIR-ASSET" in t)
    else:
        print("[SKIP] end-to-end build check (pdflatex or git not on PATH)")

    # character multiset: accents and hyphenation do not count, a real letter does
    base = ["The field e and the connection w-", "ord appear."]
    hatted = ["The field ê and the connection wˆord", "appear."]
    check("char multiset: an accent and a line-end hyphen are not a difference",
          char_multiset_diff(base, hatted) == {})
    check("char multiset: a changed letter is a difference  [foil]",
          char_multiset_diff(base, ["The field f and the connection word", "appear."]) == {"e": -1, "f": 1})

    print("\nALL PASS" if ok else "\nFAILED")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--before-log", type=Path)
    p.add_argument("--after-log", type=Path)
    p.add_argument("--before-aux", type=Path)
    p.add_argument("--after-aux", type=Path)
    p.add_argument("--reproduce", type=Path, help="PDF you were given")
    p.add_argument("--built", type=Path, help="PDF you built yourself")
    p.add_argument("--chars", nargs=2, type=Path, metavar=("BEFORE_PDF", "AFTER_PDF"),
                   help="compare the letters of two PDFs as a multiset, ignoring accents, spaces and "
                        "line-end hyphenation (a decoration-only pass must give 0)")
    p.add_argument("--build", metavar="SPEC",
                   help="typeset SPEC and report its log/aux. SPEC = path to a .tex, or "
                        "REV:file.tex to take the whole revision via `git archive` "
                        "(that revision's figures/bst/bib, not the live ones)")
    p.add_argument("--build-against", metavar="SPEC",
                   help="second SPEC; both are built and compared")
    p.add_argument("--repo", type=Path, default=Path("."), help="git repo for REV: specs")
    p.add_argument("--assets", type=Path, help="dir with figures/bst/bib for a plain .tex build (default: the .tex's own directory)")
    p.add_argument("--engine", default="pdflatex")
    p.add_argument("--out", type=Path, help="where to build (default: a temp dir, kept)")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--strict-overfull", action="store_true")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()

    if a.selftest:
        return selftest()

    if a.build:
        import tempfile
        root = a.out or Path(tempfile.mkdtemp(prefix="texbuild-"))
        one = build_one(a.build, a.repo, a.assets, a.engine, root / "a")
        print(f"[build] {a.build}  -> {one['dir']}  (tex sha256 {one['sha256']}, "
              f"exit {one['exit_codes']})")
        if not a.build_against:
            return report(one["parsed"], one["parsed"], None, False, False)
        two = build_one(a.build_against, a.repo, a.assets, a.engine, root / "b")
        print(f"[build] {a.build_against}  -> {two['dir']}  (tex sha256 {two['sha256']}, "
              f"exit {two['exit_codes']})")
        drift = None
        if one["aux"].exists() and two["aux"].exists():
            drift = aux_drift(parse_aux(one["aux"].read_text(encoding="latin-1")),
                              parse_aux(two["aux"].read_text(encoding="latin-1")))
        return report(one["parsed"], two["parsed"], drift, a.strict, a.strict_overfull)

    if a.chars:
        d = char_multiset_diff(pdf_text(a.chars[0]), pdf_text(a.chars[1]))
        tot = sum(abs(v) for v in d.values())
        print(f"[{'PASS' if tot == 0 else 'FAIL'}] characters differing (accents, spaces and line-end "
              f"hyphenation ignored): {tot}")
        for k, v in sorted(d.items(), key=lambda kv: -abs(kv[1]))[:40]:
            print(f"   {k!r}: {v:+d}")
        return 0 if tot == 0 else 1

    if a.reproduce and a.built:
        n = text_diff_count(pdf_text(a.reproduce), pdf_text(a.built))
        print(f"[{'PASS' if n == 0 else 'FAIL'}] pdftotext differing lines: {n}")
        if n:
            print("   Your build does not reproduce the given PDF. Do not attribute visual "
                  "observations to the source until this is 0 (check engine, assets, revision).")
            return 1
        print("   Build reproduces the given PDF; later visual observations are attributable "
              "to the source.")
        return 0

    bl = parse_log(a.before_log.read_text(encoding="latin-1")) if a.before_log else None
    al = parse_log(a.after_log.read_text(encoding="latin-1")) if a.after_log else None
    drift = None
    if a.before_aux and a.after_aux:
        drift = aux_drift(parse_aux(a.before_aux.read_text(encoding="latin-1")),
                          parse_aux(a.after_aux.read_text(encoding="latin-1")))
    if not (bl or drift):
        p.error("give --before-log/--after-log, --before-aux/--after-aux, or --reproduce/--built")
    return report(bl, al, drift, a.strict, a.strict_overfull)


if __name__ == "__main__":
    sys.exit(main())
