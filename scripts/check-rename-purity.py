#!/usr/bin/env python3
"""機械的と主張される一括改稿 (記法 rename・単位変更・記号統一) の純粋性を、読まずに逆写像して検査する: new→old の逆写像を後版に当てて前版と diff し、残った行だけを非機械変更として出す + 旧綴りの残存 (orphan) 走査 + 変更行の前後行番号列挙。--selftest 内蔵、正本 = conventions/physics-verification-cycle.md#referee-side-kernels

なぜ逆写像か: 「記法を変えただけ」 の diff を目視で読むと、 renamed 行の中に紛れた符号・添字・係数の
変更を見落とす (renamed 行は「変わっていて当然」 に見える)。 逆写像を当てれば、 純粋な rename は
前版と完全一致に畳まれ、 **畳まれずに残った行だけ**が非機械変更の全部になる。 読む量が diff 全体から
残差だけに落ち、 見落としが「見落とし」 でなく「残差」 として機械に出る。

使い方:

    # 1. 純粋性の検査 (map = new→old の逆写像規則)
    python3 check-rename-purity.py --before old.tex --after new.tex --map rename-map.json

    # 2. gate (残差が allow に無ければ exit 1) + 旧綴りの残存を禁止
    python3 check-rename-purity.py --before old.tex --after new.tex --map rename-map.json \
        --allow allow.txt --forbid '\\Gamma_\\{\\\\Lambda' --strict

    # 3. 変更行の列挙 (前版 / 後版の行番号つき、 hunk 単位)
    python3 check-rename-purity.py --before old.tex --after new.tex --classify
    # 4. 単射でない改名 (旧い綴り 2 つ -> 新しい綴り 1 つ) は逆写像で畳めない -> 前向きに当てて照合
    #    (map は old->new として読む。 行数が同じこと、 変わった行 = 前版の像、 変わらない行に旧い綴りが残らない)
    python3 check-rename-purity.py --before old.tex --after new.tex --map forward.json --forward --strict

map file (JSON):

    {"rules": [
       {"kind": "regex",   "new": "\\\\Gamma\\^\\{\\((\\d)\\)\\}\\|_\\{\\\\phi\\\\phi\\}",
                           "old": "\\\\Gamma^{(\\1)}_{\\\\Lambda}", "scope": [400, 520]},
       {"kind": "literal", "new": "\\big|", "old": "|"}
    ]}

- `kind`: `literal` (str.replace、 既定) か `regex` (re.sub、 後方参照は `\\1`)。 LaTeX の `\` は
  regex の置換文字列で壊れやすいので、 迷ったら literal (conventions/edit-intent-record.md#anchor-assert)。
- `scope`: 後版の行番号 [from, to] (1 始まり・両端含む)。 省略で全行。 節ごとに綴りが違う改稿で使う。
- 規則は **書かれた順**に適用する (長い綴りを先に置く)。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------- core


def apply_rules(lines: list[str], rules: list[dict]) -> list[str]:
    """Apply the inverse (new -> old) rules to `lines`, honouring per-rule line scopes."""
    out = list(lines)
    for idx, rule in enumerate(rules):
        kind = rule.get("kind", "literal")
        new, old = rule["new"], rule["old"]
        lo, hi = rule.get("scope", [1, len(out)])
        if kind not in ("literal", "regex"):
            raise ValueError(f"rule {idx}: kind must be literal or regex, got {kind!r}")
        for i in range(max(1, lo) - 1, min(len(out), hi)):
            if kind == "literal":
                out[i] = out[i].replace(new, old)
            else:
                out[i] = re.sub(new, old, out[i])
    return out


def residual_diff(before: list[str], mapped: list[str]) -> list[str]:
    """Unified diff of the reverse-mapped after-file against the before-file (context 0)."""
    return list(
        difflib.unified_diff(before, mapped, "before", "after(reverse-mapped)", lineterm="", n=0)
    )


def residual_lines(diff: list[str]) -> list[tuple[str, int, str]]:
    """[(side, line_number, text)] for every residual line; side is '-' (before) or '+' (after)."""
    out: list[tuple[str, int, str]] = []
    b = a = 0
    for ln in diff:
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", ln)
        if m:
            b, a = int(m.group(1)), int(m.group(2))
            continue
        if ln.startswith("---") or ln.startswith("+++"):
            continue
        if ln.startswith("-"):
            out.append(("-", b, ln[1:]))
            b += 1
        elif ln.startswith("+"):
            out.append(("+", a, ln[1:]))
            a += 1
        else:
            b += 1
            a += 1
    return out


def changed_lines(before: list[str], after: list[str]) -> list[tuple[int, str, int, str]]:
    """[(hunk, side, line_number, text)] over the raw diff — the 'what actually changed' inventory."""
    diff = list(difflib.unified_diff(before, after, "before", "after", lineterm="", n=0))
    out: list[tuple[int, str, int, str]] = []
    hunk = 0
    b = a = 0
    for ln in diff:
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", ln)
        if m:
            hunk += 1
            b, a = int(m.group(1)), int(m.group(2))
            continue
        if ln.startswith("---") or ln.startswith("+++"):
            continue
        if ln.startswith("-"):
            out.append((hunk, "-", b, ln[1:]))
            b += 1
        elif ln.startswith("+"):
            out.append((hunk, "+", a, ln[1:]))
            a += 1
        else:
            b += 1
            a += 1
    return out


def census(lines: list[str], pattern: str, section_re: str) -> dict[str, list[tuple[int, str]]]:
    """Inventory every spelling of a symbol family: {matched text -> [(line, section)]}.

    Two spellings of one object are the residue a purity check cannot see (the diff is clean
    because neither section was touched). Grouping by the exact matched string makes them
    adjacent in the report. Comment-only lines are skipped; a trailing comment is trimmed.
    """
    rx, sx = re.compile(pattern), re.compile(section_re)
    out: dict[str, list[tuple[int, str]]] = {}
    sec = "(before the first section)"
    for i, raw in enumerate(lines, 1):
        m = sx.search(raw)
        if m:
            sec = (m.group(1) if m.groups() else m.group(0))[:40]
        if raw.lstrip().startswith("%"):
            continue
        body = re.sub(r"(?<!\\)%.*$", "", raw)
        for mm in rx.finditer(body):
            out.setdefault(mm.group(0), []).append((i, sec))
    return out


def forward_check(before: list[str], after: list[str], rules: list[dict]
                  ) -> tuple[int, list[tuple[int, str, str, str]], list[tuple[int, str]]]:
    """Forward purity for a line-count-preserving rewrite: apply the rules old -> new to each
    before-line (in order, honouring each rule's scope). Returns (changed, unexplained, missed):
    unexplained = changed lines whose after-text is not the forward image of the before-text
    (an edit the map does not explain), missed = unchanged lines that still contain a rule's
    old text (an occurrence the rewrite skipped). Use when the rename is not injective (two old
    spellings -> one new one), where the inverse map cannot fold the after-file back."""
    unexplained: list[tuple[int, str, str, str]] = []
    missed: list[tuple[int, str]] = []
    changed = 0
    n = len(before)
    for i, (x, y) in enumerate(zip(before, after), 1):
        z = x
        for r in rules:
            lo, hi = r.get("scope", [1, n])
            if not (lo <= i <= hi):
                continue
            if r.get("kind", "literal") == "literal":
                z = z.replace(r["old"], r["new"])
            else:
                z = re.sub(r["old"], r["new"], z)
        if x != y:
            changed += 1
            if z != y:
                unexplained.append((i, x, y, z))
        elif z != x:
            missed.append((i, x))
    return changed, unexplained, missed


def forbid_scan(lines: list[str], patterns: list[str]) -> list[tuple[int, str, str]]:
    """Surviving old spellings: [(line_number, pattern, text)]. Comments are scanned too."""
    hits: list[tuple[int, str, str]] = []
    for pat in patterns:
        rx = re.compile(pat)
        for i, ln in enumerate(lines, 1):
            if rx.search(ln):
                hits.append((i, pat, ln))
    return sorted(hits)


def load_allow(path: Path | None) -> list[re.Pattern]:
    if path is None:
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if s and not s.startswith("#"):
            out.append(re.compile(s))
    return out


def allowed(text: str, allow: list[re.Pattern]) -> bool:
    return any(rx.search(text) for rx in allow)


# ---------------------------------------------------------------- report


def run(before_p: Path, after_p: Path, map_p: Path | None, allow_p: Path | None,
        forbid: list[str], strict: bool, classify: bool,
        census_re: str | None = None, section_re: str = r"\\(?:sub)*section\*?\{([^}]*)\}",
        forward: bool = False) -> int:
    before = before_p.read_text(encoding="utf-8").split("\n") if before_p else []
    after = after_p.read_text(encoding="utf-8").split("\n")

    if census_re:
        groups = census(after, census_re, section_re)
        total = sum(len(v) for v in groups.values())
        print(f"[info] {total} occurrence(s) in {len(groups)} distinct spelling(s)\n")
        for text, hits in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            secs = sorted({s for _, s in hits})
            print(f"{len(hits):4d}x  {text}")
            print(f"        lines {hits[0][0]}..{hits[-1][0]}   sections: {', '.join(secs)}")
        if len(groups) > 1:
            print("\n⚠️ more than one spelling matched. That is expected for a family "
                  "(different orders/fields); it is a finding when two of them denote the "
                  "SAME object — check the sections that differ.")
        return 0

    if classify:
        rows = changed_lines(before, after)
        for hunk, side, num, text in rows:
            print(f"H{hunk} {'before' if side == '-' else 'after '}:{num:6d} {side} {text[:150]}")
        print(f"\n# hunks: {rows[-1][0] if rows else 0}   changed lines: {len(rows)}")
        return 0

    rules = json.loads(map_p.read_text(encoding="utf-8"))["rules"] if map_p else []
    if forward:
        if len(before) != len(after):
            print(f"FAILED: --forward needs the same line count ({len(before)} -> {len(after)}); "
                  "use the inverse map instead")
            return 1
        changed, unexpl, missed = forward_check(before, after, rules)
        print(f"[info] rules (old -> new): {len(rules)}   changed lines: {changed}")
        for num, x, y, z in unexpl:
            print(f"[UNEXPLAINED] line {num}\n   before : {x[:150]}\n   after  : {y[:150]}\n"
                  f"   forward: {z[:150]}")
        for num, x in missed:
            print(f"[MISSED] line {num} still has an old spelling: {x[:150]}")
        bad = len(unexpl) + len(missed)
        print()
        if bad:
            print(f"FAILED: {len(unexpl)} changed line(s) not explained by the forward map, "
                  f"{len(missed)} unchanged line(s) still matching an old spelling")
            return 1 if strict else 0
        print("ALL PASS (every changed line is the forward image of its before-line; nothing missed)")
        return 0
    mapped = apply_rules(after, rules)
    diff = residual_diff(before, mapped)
    res = residual_lines(diff)
    allow = load_allow(allow_p)

    raw = changed_lines(before, after)
    folded = len(raw) - len(res)
    print(f"[info] rules: {len(rules)}   changed lines in raw diff: {len(raw)}")
    print(f"[info] folded by the inverse map (= pure rename): {folded}")
    print(f"[info] residual (= NOT explained by the map): {len(res)}\n")

    unexplained = 0
    for side, num, text in res:
        ok = allowed(text, allow)
        if not ok:
            unexplained += 1
        tag = "allow" if ok else "RESIDUAL"
        print(f"[{tag}] {'before' if side == '-' else 'after '}:{num:6d} {side} {text[:150]}")

    hits = forbid_scan(after, forbid) if forbid else []
    if forbid:
        print(f"\n[info] forbidden-orphan patterns: {len(forbid)}   hits: {len(hits)}")
        for num, pat, text in hits:
            print(f"[ORPHAN] after:{num:6d}  /{pat}/  {text[:130]}")

    print()
    if strict:
        bad = unexplained + len(hits)
        if bad:
            print(f"FAILED: {unexplained} unexplained residual line(s), {len(hits)} surviving orphan(s)")
            return 1
        print("ALL PASS (every changed line is explained by the map or the allow list; no orphans)")
        return 0
    print(f"done (non-strict): {unexplained} residual line(s) not covered by the allow list, "
          f"{len(hits)} orphan hit(s)")
    return 0


# ---------------------------------------------------------------- selftest


def selftest() -> int:
    """Synthetic rename + foils: a sign flip hidden inside a renamed line must NOT fold away."""
    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(f"[{'PASS' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    before = [
        r"The vertex \Gamma^{(2)}_{\Lambda} is defined by",
        r"\Gamma^{(2)}_{\Lambda} = + A + B,",
        r"\Gamma^{(1)}_{\Lambda} = C,",
        r"where \Gamma^{(2)}_{\Lambda} is the second variation.",
        r"An untouched line.",
    ]
    # pure rename on lines 1-3, plus (a) a prose edit on line 4, (b) a SIGN FLIP on line 2.
    after = [
        r"The vertex \Gamma^{(2)}|_{\phi\phi} is defined by",
        r"\Gamma^{(2)}|_{\phi\phi} = - A + B,",
        r"\Gamma^{(1)}|_{\phi} = C,",
        r"where \Gamma^{(2)}|_{\phi\phi} is the second variation in \phi.",
        r"An untouched line.",
    ]
    rules = [
        {"kind": "literal", "new": r"\Gamma^{(2)}|_{\phi\phi}", "old": r"\Gamma^{(2)}_{\Lambda}"},
        {"kind": "literal", "new": r"\Gamma^{(1)}|_{\phi}", "old": r"\Gamma^{(1)}_{\Lambda}"},
    ]
    mapped = apply_rules(after, rules)
    res = residual_lines(residual_diff(before, mapped))
    res_after = {num for side, num, _ in res if side == "+"}

    check("line 1 (pure rename) folds away", 1 not in res_after)
    check("line 2 (sign flip inside a renamed line) survives as residual  [foil]", 2 in res_after)
    check("line 3 (pure rename, different label) folds away", 3 not in res_after)
    check("line 4 (prose edit) survives as residual", 4 in res_after)
    check("exactly 2 residual lines on the after side", res_after == {2, 4})

    # regex rules with a back-reference
    rx_rules = [{"kind": "regex", "new": r"\\Gamma\^\{\((\d)\)\}\|_\{\\phi+\\?p?h?i?\}",
                 "old": r"\\Gamma^{(\1)}_{\\Lambda}"}]
    rx_mapped = apply_rules([r"\Gamma^{(2)}|_{\phi\phi} = X"], rx_rules)
    check("regex rule with back-reference rewrites",
          rx_mapped == [r"\Gamma^{(2)}_{\Lambda} = X"])

    # scope limits a rule to a line range
    scoped = apply_rules([r"\A", r"\A"], [{"kind": "literal", "new": r"\A", "old": r"\B",
                                           "scope": [2, 2]}])
    check("scope restricts the rule to its line range", scoped == [r"\A", r"\B"])

    # orphan scan: an un-renamed occurrence (here inside a comment) is found
    orphan_src = after + [r"% leftover \Gamma^{(2)}_{\Lambda} in a comment"]
    hits = forbid_scan(orphan_src, [r"\\Gamma\^\{\(\d\)\}_\{\\Lambda\}"])
    check("orphan scan finds the surviving old spelling in a comment", len(hits) == 1)
    check("orphan scan is clean when nothing survives",
          forbid_scan(after, [r"\\Gamma\^\{\(\d\)\}_\{\\Lambda\}"]) == [])

    # changed-line inventory covers every changed line
    raw = changed_lines(before, after)
    check("changed-line inventory reports both sides of all 4 touched lines", len(raw) == 8)

    # census: two spellings of one object in two different sections, plus comment handling
    doc = [r"\section{First}", r"\Op^{(2)}|_{xx} = A", r"% \Op^{(2)}_{old} in a comment",
           r"\subsection{Second}", r"\Op^{(2)}_{old} = B  % trailing \Op^{(2)}|_{xx}"]
    g = census(doc, r"\\Op\^\{\(2\)\}(?:\|_\{\w+\}|_\{\w+\})", r"\\(?:sub)*section\*?\{([^}]*)\}")
    check("census groups by exact spelling (2 distinct)", len(g) == 2)
    check("census reports the section each spelling lives in",
          g[r"\Op^{(2)}|_{xx}"][0][1] == "First" and g[r"\Op^{(2)}_{old}"][0][1] == "Second")
    check("census skips comment-only lines and trailing comments  [foil]",
          len(g[r"\Op^{(2)}_{old}"]) == 1 and len(g[r"\Op^{(2)}|_{xx}"]) == 1)

    # allow list suppresses a known non-rename edit, strict exit codes
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "b.tex").write_text("\n".join(before), encoding="utf-8")
        (d / "a.tex").write_text("\n".join(after), encoding="utf-8")
        (d / "map.json").write_text(json.dumps({"rules": rules}), encoding="utf-8")
        (d / "allow.txt").write_text("second variation in\n", encoding="utf-8")
        rc_strict = run(d / "b.tex", d / "a.tex", d / "map.json", d / "allow.txt", [], True, False)
        check("strict FAILS while the sign flip is unexplained  [foil]", rc_strict == 1)
        # the residual is a PAIR of lines (before '-' and after '+'); an allow list that covers
        # only the after side still leaves the before side unexplained, so cover both.
        (d / "allow2.txt").write_text("second variation\n= [-+] A \\+ B,\n", encoding="utf-8")
        rc_ok = run(d / "b.tex", d / "a.tex", d / "map.json", d / "allow2.txt", [], True, False)
        check("strict PASSES once every residual is on the allow list", rc_ok == 0)

    # --forward: a non-injective rename (two old spellings -> one new spelling) cannot be folded
    # back by an inverse map, but the forward image explains it
    f_before = [r"S = (P^{(0sw)}+P^{(0ws)})_{ab}", r"T = \sqrt3\,P^{(sw)}", r"U = P^{(0s)}", r"V = P^{(0s)}"]
    f_after = [r"S = (P^{(0)\perp\parallel}+P^{(0)\parallel\perp})_{ab}",
               r"T = \sqrt3\,(P^{(0)\perp\parallel}+P^{(0)\parallel\perp})", r"U = P^{(0)\perp}",
               r"V = P^{(0s)}"]
    f_rules = [{"old": r"P^{(sw)}", "new": r"(P^{(0)\perp\parallel}+P^{(0)\parallel\perp})"},
               {"old": r"P^{(0sw)}", "new": r"P^{(0)\perp\parallel}"},
               {"old": r"P^{(0ws)}", "new": r"P^{(0)\parallel\perp}"},
               {"old": r"P^{(0s)}", "new": r"P^{(0)\perp}"}]
    ch, un, mi = forward_check(f_before, f_after, f_rules)
    check("--forward explains a non-injective rename (changed lines 1-3)", ch == 3 and un == [])
    check("--forward reports the skipped occurrence on line 4  [foil]", [n for n, _ in mi] == [4])
    flip = list(f_after)
    flip[2] = r"U = -P^{(0)\perp}"
    _, un2, _ = forward_check(f_before, flip, f_rules)
    check("--forward flags an edit hidden in a renamed line (sign flip)  [foil]",
          [n for n, *_ in un2] == [3])
    inv = [{"kind": "literal", "new": r["new"], "old": r["old"]} for r in f_rules]
    res_inv = residual_lines(residual_diff(f_before, apply_rules(f_after, inv)))
    check("the inverse map cannot fold the same rename back (why --forward exists)", len(res_inv) > 0)

    print("\nALL PASS" if ok else "\nFAILED")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--before", type=Path, help="manuscript before the mechanical edit")
    p.add_argument("--after", type=Path, help="manuscript after it")
    p.add_argument("--map", type=Path, help="JSON file of new->old inverse rules (old->new with --forward)")
    p.add_argument("--forward", action="store_true",
                   help="apply the map old->new to --before and require every changed line to be the "
                        "forward image and no unchanged line to keep an old spelling (same line count; "
                        "for renames that are not injective, where the inverse map cannot fold back)")
    p.add_argument("--allow", type=Path, help="file of regexes for residual lines that are expected")
    p.add_argument("--forbid", action="append", default=[],
                   help="regex whose survival in --after is an orphan (repeatable)")
    p.add_argument("--strict", action="store_true", help="exit 1 on unexplained residual / orphans")
    p.add_argument("--classify", action="store_true",
                   help="only enumerate changed lines with before/after line numbers")
    p.add_argument("--census", metavar="REGEX",
                   help="inventory every spelling of a symbol family in --after, grouped by the "
                        "exact matched text with line range and enclosing sections "
                        "(finds two spellings of one object; needs only --after)")
    p.add_argument("--section-re", default=r"\\(?:sub)*section\*?\{([^}]*)\}",
                   help="regex whose group 1 names the enclosing section (default: LaTeX)")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()

    if a.selftest:
        return selftest()
    if a.census:
        if not a.after:
            p.error("--census needs --after")
        return run(None, a.after, None, None, [], False, False, a.census, a.section_re)
    if not (a.before and a.after):
        p.error("--before and --after are required (or --selftest)")
    if not a.classify and not a.map:
        p.error("--map is required unless --classify")
    return run(a.before, a.after, a.map, a.allow, a.forbid, a.strict, a.classify, forward=a.forward)


if __name__ == "__main__":
    sys.exit(main())
