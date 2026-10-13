<!-- doc-meta
when: AI (Claude / Codex / 別ベンダー) に決定 ledger に基づく原稿・ノートの改稿を実装させる依頼 spec を書くとき / 自分 (AI session) がその実装 pass を commit する前 / 実装 pass の後で「どの hunk がどの決定に対応するか」 を確かめるとき / 改稿 diff を著者が採否判定するとき
category: research-domain
summary: AI による原稿改稿の意図記録 (edit-intent record) + 実装 pass の作業規律 (当てる→組版 gate→記録、 削除前の blame、 清掃版どうしの diff) と投稿前の清掃 — 実装 pass ごとに hunk → {finding ID / decision ID / 種類 = 実装・裁量・削除} の対応表を sidecar (review/edit-intent-<date>.md) に残してから commit する。 決定 ledger に無い変更は裁量として別枠に列挙、 削除は削除前の全文を verbatim で添付、 「圧縮」「1 文のみ」 のような量の指示は守り、 越えるなら裁量枠へ。 機械 gate = scripts/check-edit-intent.py (--scaffold で骨組み生成 / hunk 被覆・位置・ID 実在・裁量枠・verbatim を PASS/FAIL)。 起源 = 2026-09 に 89 hunk を 1 週間後に人手で棚卸しし決定超過 3 種を見つけた事故
-->
# AI による原稿改稿の意図記録 (edit-intent record)

> 位置づけ: 外部 AI 査読 → 前提検証 → 著者判断 (decision ledger) までは [`physics-verification-cycle.md#external-ai-referee-premise-verification`](physics-verification-cycle.md#external-ai-referee-premise-verification) が正本。
> 本 doc はその次の station = 「著者の決定を AI が原稿に実装する」 pass の**記録形式**を定める。
> 実装後の検品 (再測・独立再走) は同 §12 の項 3 と [`paper-audit.md#relocation-rebinding-sweep`](../../claude-config/conventions/paper-audit.md#relocation-rebinding-sweep)、 referee への返信は [`rebuttal-letter.md`](../../claude-config/conventions/rebuttal-letter.md) が担う。
> vendor 中立 — 実装者が Claude でも Codex でも別ベンダーでも人間でも、 sidecar の形式と gate は同じ。

## <a id="why"></a>1. 何が起きたか — 決定の実装と実装者の裁量が diff の中で同じ顔をしていた

- 2026-09-01、 別ベンダーの AI が査読 finding 15 件と著者の決定 ledger に基づき、 private paper repo の原稿を 1 commit で 89 hunk / 474 行改稿した。
- hunk ごとに「どの finding / どの決定に対応する編集か」 は commit にも ledger にも残らなかった。
- 1 週間後、 著者側の session が 89 hunk を人手で ledger と突合し、 決定を越えた編集を見つけた。
  - 決定「その文は消すな (反証可能性の framing)」 に反する削除。
  - 決定「一般論を**圧縮**」 に対する、 共著者が書いた 96 行の削除 (模型固有の部分ごと)。
  - 棚卸し自身も 1 件を誤分類した (「**1 文のみ**」 の決定を越えた、 と読んだ 3 箇所は、 report と突合すると著者判断の内容そのものだった)。 記録が無いと、 事後の棚卸しの verdict も推測になる。
- 物理的に誤った編集は 1 つも無かった。 問題は**正しさでなく追跡可能性**で、 「決定の実装」 「report 文言の転記」 「実装者の裁量」 が diff の中で区別できなかった。
- 著者の言葉: 「編集の意図を記録してなかったのは問題」。
- 事後棚卸しの cost = 89 hunk の本文読みで 1 session。 commit 時に記録すれば hunk あたり 1 行で済む。
- 同型の既知事故 = 記録 (task tracker) を元に referee 返信を書くと本文とズレる ([`rebuttal-letter.md`](../../claude-config/conventions/rebuttal-letter.md) §1)。 どちらも「編集の意図が編集の隣に無い」 が根。

## <a id="rules"></a>2. 規則

1. <a id="one-pass-one-sidecar"></a>**1 実装 pass = 1 sidecar**。 pass = 1 commit、 または実装者が連続して積んだ commit 範囲 (base → head)。 file ごとに 1 sidecar (本文と bib を 1 pass で触ったら sidecar は 2 つ)。
2. **対応表は hunk ごとに 種類 / ID / 意図 の 3 列**。 hunk 番号は `git diff -U0 <base> <head> -- <file>` の `@@` の出現順。 全 hunk を過不足なく被覆する (1 行で複数 hunk を束ねてよい)。
3. <a id="discretion-is-explicit"></a>**決定 ledger に無い変更は、 どんなに無害でも 種類 = 裁量**。 typo 修正も metadata も語句の統一も裁量。 裁量の節に「何を・なぜ」 を 1 行ずつ列挙し、 採否を著者に渡す。 裁量が 0 件なら節に「なし」 と書く (= 空欄と区別する)。
   - <a id="protected-regions-need-ruling"></a>**保護領域 (表題・概要・序論・結論・式) は裁量でも当てない**: 決定 ledger に無い変更が保護領域に及ぶなら、 当てずに提案で止まり、 著者の項目ごとの裁定を記録してから当てる。 正本 = claude-config [`manuscript-claim-ownership.md#rule`](../../claude-config/conventions/manuscript-claim-ownership.md#rule) (機械 gate あり)。
4. <a id="deletion-verbatim"></a>**削除 hunk (= 削除行が追加行より多い hunk) は削除前の全文を verbatim で添える**。 git に diff は残るが、 意図の隣に無い verbatim は棚卸しのとき読まれない。 Overleaf 等の外部 git は履歴が squash され得るので、 sidecar を自己完結の証拠にする。 <a id="no-unilateral-deletion"></a>**共著者が書いた本文を実装者 (AI) の単独判断で削除しない** — 「圧縮」 の指示に対しては付録へ verbatim で MOVE + 本文に短い pointer が下限で、 削除そのものは著者の決定に戻す (2026-09-08 の著者規範。 96 行の削除はこの規範で是正対象になった)。
5. <a id="quantity-instruction"></a>**決定が量の指示を含むとき、 実装はその量を守る**。 「圧縮」 は削除ではない。 「1 文のみ」 は 1 文。 「脚注 1 本」 は 1 本。 越える必要があると判断したら、 越えた分を裁量枠に書いて**止まり**、 著者に判断を渡す。 黙って越えない。
6. <a id="gate-before-commit"></a>**commit 前に check を PASS させる**。 sidecar は原稿と同じ pass で commit し、 decision ledger から sidecar の path と head hash へ pointer を張る。 gate は散文でなく exit code (= cold な worker session に散文規律は効かない、 [`physics-verification-cycle.md#campaign-tooling`](physics-verification-cycle.md#campaign-tooling) E と同じ理由)。
7. **記録するのは意図であって正当化ではない**。 対応表の 意図 は 1 行。 長い理由は裁量の節へ。 物理の議論は ledger か note へ。
8. **遡及作成はしない**。 規則は導入後の pass から。 過去の pass は事後棚卸し record (audit) が代替する。
9. <a id="no-draft-color-marks"></a>**AI が原稿に入れた文を、 印字面の色や印で区別しない**。 著者が裁定した変更も、 著者がまだ読んでいない試稿 (保護領域の外に足す節・付録の初稿) も、 地の文で入れる。 `\cl{}` のような着色の macro・下線・欄外の印で包まない。 裁定を待つ状態は印字面でなく、 決定 ledger の未決の項目と、 その箇所の `% PROPOSAL` で始まる comment で追い、 pass の対応表では 種類 = 裁量 に挙げる (規則 3)。 理由 (実測) = 裁定が進むと、 色の付いた文と付いていない文の境が裁定の有無と合わなくなり、 印は読む人の雑音になる。 投稿前と latexdiff の前に外す手間も増える (§8 と [`latex.md`](../../claude-config/conventions/latex.md) の markup unwrap)。 保護領域に入れること自体の可否は [`manuscript-claim-ownership.md#rule`](../../claude-config/conventions/manuscript-claim-ownership.md#rule) が決める。 提案の注記を PDF に印字するかは、 同じ節の 2 のとおり repo の慣行と著者の指示で決まる。 本規則が決めるのは、 地の文に入れた AI の文の印である。 著者が自分の推敲に使う一時的な着色 ([`latex.md#preamble-macros-first`](../../claude-config/conventions/latex.md#preamble-macros-first) の author drafting marker) は著者の物で、 本規則の主語は AI である。

## <a id="sidecar-format"></a>3. sidecar の形式

path = 原稿 repo の `review/edit-intent-<date>.md` (review dir が無ければ決定 ledger の隣)。
Markdown + frontmatter。 YAML にしない理由 = verbatim の code block を持ちたい + 依存なしの python で読みたい ([`yaml-hazards.md`](../../claude-config/conventions/yaml-hazards.md))。

````markdown
---
sidecar: edit-intent
date: 2026-09-01
implementer: codex          # claude / codex / human / <vendor 名>。 運搬者であって判断主体ではない
repo: external/overleaf     # 原稿の git dir (この repo root からの相対、 同じ repo なら .)
file: paper.tex             # その git dir 内の path。 1 sidecar = 1 file
base: 5079606               # 実装前の commit
head: 0b43326               # 実装 commit (範囲なら末尾)
ledgers:                    # ID の実在を確かめる file (先頭 = decision ledger)
  - review/author-decisions.md
  - review/referee-report-and-revision-plan.md
---
# Edit intent 2026-09-01 (paper.tex, 5079606 → 0b43326)

## 対応表

| hunk | 位置 | 種類 | ID | 意図 |
|---|---|---|---|---|
| 1 | -12,1 +12,1 | 実装 | F3, R3 | 符号 typo を修正 |
| 2-4 | -40,6 +40,9 | 実装 | R2 | 検証範囲の限定を 3 箇所で統一 |
| 5 | -52,0 +55,1 | 裁量 | R0 | 序論に限定句を追加 (決定に無い) |
| 37 | -610,85 +613,6 | 実装+削除 | F9, R8 | 一般論を数行に圧縮 (決定 = 圧縮。 模型固有の部分も落とした → 裁量) |

## 裁量

- hunk 5: 「<限定句>」 を追加。 序論でも前提を言うため。 決定に無い。
- hunk 37: 決定は「一般論の圧縮」。 模型固有の数十行 (展開式・別稿予告など) も落とした。 規則 4 では付録へ MOVE が下限なので、 復元か MOVE かは著者判断。

## 削除

### hunk 37
```latex
(削除前の 85 行を verbatim)
```
````

列の意味:

- **hunk**: `N` / `N-M` / `N,M`。 対応表全体で 1..N を重複なく被覆する。
- **位置**: `@@ -a,b +c,d @@` の中身。 scaffold が埋める。 diff を取り直して番号がずれたら check が落ちる (= stale sidecar の検出)。
- **種類**: `実装` / `裁量` / `削除` を `+` で連結 (`実装+削除`)。 削除 hunk は 削除 を必ず含む。
- **ID**: 実装なら必須。 ledger に literal で存在する token (finding ID / task ID / 決定節の anchor id / 決定を確定した commit hash)。 複数は `,` 区切り。 裁量は任意 (越えた決定があれば書く)。
- **意図**: 1 行。 空にしない。

## <a id="check-script"></a>4. 機械検査 — `scripts/check-edit-intent.py`

python3 のみ、 依存なし。 3 mode。

```bash
# 1. 骨組みを diff から生成 (全 hunk の行 + 位置 + 削除 hunk の verbatim を埋めた状態で出す)
python3 scripts/check-edit-intent.py --scaffold --root <原稿 repo> --repo external/overleaf --file paper.tex \
    --base 5079606 --head 0b43326 --implementer codex \
    --ledger review/author-decisions.md --ledger review/referee-report-and-revision-plan.md \
    --out review/edit-intent-2026-09-01.md
# 2. 検査 ([PASS]/[FAIL] 行 + ALL PASS / FAILED + exit code)
python3 scripts/check-edit-intent.py review/edit-intent-2026-09-01.md
# 3. selftest (合成 git repo で PASS 1 + foil)
python3 scripts/check-edit-intent.py --selftest
```

実装者が埋めるのは 種類 / ID / 意図 だけ。 hunk 番号・位置・verbatim は機械が出すので、 記録の cost は hunk あたり 1 行。

検査項目 (1 つでも落ちたら FAILED、 exit 1):

1. frontmatter の必須 key (`repo` / `file` / `base` / `head` / `ledgers`) と、 base / head が git に実在すること。
2. **hunk 被覆** — 対応表の hunk が 1..N を重複なく被覆する。 N は同じ diff コマンドで再計算する。
3. **位置** — 列があれば実 diff の `@@` と一致する。
4. **種類** の語彙 (実装 / 裁量 / 削除 のみ)。
5. **ID の実在** — 実装 row は ID ≥ 1 で、 各 token が ledger のどれかに literal で存在する。 裁量 row に ID があれば同じ検査。
6. **裁量枠** — `## 裁量` の節が在り、 裁量 row の hunk 番号が全部そこに列挙されている。 裁量 row が無ければ節に「なし」。
7. **削除の verbatim** — 削除 hunk は 種類 に 削除 を含み、 `## 削除` の `### hunk N` block に diff の削除行が全部 verbatim で在る。 削除行の無い hunk に 削除 を付けたら FAIL (誤 label)。
8. **意図** が空でない。

INFO (FAIL にしない): net 削除 ≥ 10 行の hunk に ID が付いていたら「量の指示を確認」 と 1 行出す (= §2 規則 5 の reminder)。

検査しないもの (= 人間の床、 著者の受領で読む):

- 意図の真偽。 ID を貼れば通る。 「その決定がその編集を許すか」 は ledger を読む人間が判定する。
- 量の指示の遵守。 INFO で名指しするまで。
- 裁量の理由の妥当性。

受領側 (著者 session) の読み方: **裁量の節から**読む。 次に 削除。 対応表は ID 順に眺めて「決定 1 つに対して何 hunk 動いたか」 を見る。 89 hunk なら 10 分。

## <a id="requester-spec-line"></a>5. 依頼側 — spec に入れる 3 行

AI に改稿の実装を依頼する spec (spawn spec / board request / 別ベンダー宛 AGENTS) には次を入れる。

```
- 実装 pass ごとに review/edit-intent-<date>.md を `check-edit-intent.py --scaffold` から作り、 種類 / ID / 意図 を埋める (正本 = ai-collaboration/conventions/edit-intent-record.md)。
- 決定 ledger に無い変更は全部 裁量 に列挙する。 「圧縮」「1 文のみ」 等の量は守り、 越える必要があれば裁量枠に書いて止まる。
- commit 前に `check-edit-intent.py review/edit-intent-<date>.md` が ALL PASS。 受領条件 = PASS + 著者が裁量枠と削除を読んで採否を ledger に記録。
```

- 受領 (accept) は「PASS した」 で閉じない。 PASS は形式の充足で、 採否は人間 ([`physics-verification-cycle.md#rubric-before-run`](physics-verification-cycle.md#rubric-before-run) の「成果物の存在は正しさの証拠でない」 と同じ)。
- 別ベンダー宛の spec では、 受け手の既定 (= 「確定知見は昇格」 等) より強い language で書く ([`physics-verification-cycle.md#campaign-tooling`](physics-verification-cycle.md#campaign-tooling) H)。
- 依頼を board / spawn のどの経路で運ぶかは owner の運用側 (Claude 内の返送 spine = [`multi-session-coordination.md#spawn-handoff-token-return`](../../claude-config/conventions/multi-session-coordination.md#spawn-handoff-token-return)、 session 宛て掲示板 = 同 `#board-request-receipt`)。

## <a id="sibling-routing"></a>6. 隣接 doc への routing

- 上流 (finding → 決定): [`physics-verification-cycle.md#external-ai-referee-premise-verification`](physics-verification-cycle.md#external-ai-referee-premise-verification)。 決定 ledger の分離 (report は finding を定義し採否を決めない) はそこが正本。
- 実装後の検品: 同 §12 項 3 (fan-out + verbatim 引用) / 文脈手術 turn の sweep = [`paper-audit.md#relocation-rebinding-sweep`](../../claude-config/conventions/paper-audit.md#relocation-rebinding-sweep)。 sidecar は検品の入口 (どの hunk を何として読むか) を与える。
- commit 単位の意図 = MOVE: / RESHAPE: prefix (同 § の verbatim-first 2 commit 分解)。 sidecar は hunk 単位で、 両者は補完する。
- 帰属: sidecar の `implementer` は運搬者であって判断主体ではない ([`actor-attribution.md`](../../claude-config/conventions/actor-attribution.md))。 決定主体は ledger の author。
- worker の書込み scope を repo 側で縛る gate = [`physics-verification-cycle.md#campaign-tooling`](physics-verification-cycle.md#campaign-tooling) J (`ledger-commit-cadence-gate.py --worker-scope-env`)。 sidecar gate はその隣に pre-commit で並べられる。
- 記録ベースで書いた文が本文とズレる同型 = [`rebuttal-letter.md`](../../claude-config/conventions/rebuttal-letter.md) §1。

## <a id="implementation-pass-discipline"></a>7. 実装 pass の作業規律 — 当てる、確かめる、記録する

sidecar は「何を書くか」 の規約。 ここは「どの順で手を動かすか」。 2026-09-08 の実運用 (1 日で live 原稿へ 9 pass) で効いたものだけを置く。

1. <a id="apply-then-record"></a>**順序は 当てる → 組版 gate → 記録 → commit。 記録を先に commit しない。** 同日、 適用 script が anchor 不一致で abort したのに、 同じ 1 コマンドに並べた記録更新だけが走り commit された (原稿は未変更のまま「変更した」 と書かれた状態が 1 commit だけ存在)。 **適用と記録を 1 つの shell 行に並べるときは `&&` で連結し、 記録側は適用の成功を前提にする。** ⚠️ **その鎖の gate をパイプの後ろに置かない** (`cmd | tail -1 && next` は cmd の失敗を隠す = 終了値は tail のもの)。 **Claude Code の Bash tool の最上位では `set -e` が効かない** (2026-09-11 実測、 zsh 5.9: `set -e; false; echo X` が X を出す。 `bash -c '…'` や `bash script.sh` の中では効く)。 gate は出力を file に落として `grep -q PASS out || exit 1` で確かめ、 長い pass は script file に `die()` を置いて 1 手ずつ確かめる (同日、 この 2 つで gate が 2 回すり抜けた = 著者の手編集の行数ガードと、 repository audit の FAILED)。 事故ったら取り消さず、 次の commit で「前 commit は適用前に記録した」 と明記して直す (履歴を書き換えない)。 (2026-09-13 にも再発: `audit … | tail -1 && git commit` で repository audit の FAILED 〔原稿 identity の遅れ〕 を通した。 出力は file に落として `grep -q '^ALL PASS' out || exit 1`。) 記録の道具が live の HEAD や hash を書き込み時に実測するなら、書いた hash が自分の作った commit かを確かめる。並列 session が間に commit を積むと、相手の commit を自分の pass として記録する ([multi-session-coordination.md#measured-record-race](../../claude-config/conventions/multi-session-coordination.md#measured-record-race))。
2. <a id="anchor-assert"></a>**置換は anchor の一意性を assert してから。** `str.replace` の前に `text.count(old) == 1` を必ず確かめ、 複数一致・不一致はその場で止める。 正規表現より literal anchor が安全 (LaTeX の `\` は正規表現の置換文字列で壊れる)。 **著者が同じ file を並行編集している場合は、 読み込み時の SHA を記録して書き込み直前に再確認する。** 機械化 = claude-config [`scripts/apply-text-pairs.py`](../../claude-config/scripts/apply-text-pairs.py) (`PAIRS = [(old, new), …]` を一意性・挿入型 pair の再実行・識別子境界で検査してから書く)。 pass ごとに置換 script を手書きしない (2026-09-13 に 40 pass 分を手書きした)。
3. <a id="build-gate"></a>**commit の前に、 別ディレクトリで組版する。** live の作業ツリーを汚さずに図表・bst を symlink した temp dir で `pdflatex → bibtex → pdflatex ×3` を回し、 **頁数・未定義参照の数・error 0 を確かめてから** commit する。 「compile は後で」 は後で直す量を増やす。 機械化 = [`scripts/compare-tex-builds.py`](../scripts/compare-tex-builds.py) (2 版の log を比べて**後版にだけ在る** error / 未定義参照 / overfull を名指し、 log の折返しを復元してから数える)。 ⚠️ **頁数は総和なので、 前半が縮んで後半が伸びると相殺して気付かない** — 記号の幅が変わる改稿 (記法の一括置換・macro の入れ替え) では、 `.aux` の label→(番号, 頁) 写像を版間で比べる (同 script `--before-aux/--after-aux`、 規律 = [`physics-verification-cycle.md#page-count-is-not-pagination`](physics-verification-cycle.md#page-count-is-not-pagination))。 <a id="frozen-revision-build"></a>**特定の commit を対象にする gate (投稿前 readiness など) では、 live の図表・bst・bib を symlink しない** — symlink した temp dir が組むのは「対象 commit の本文 + 今の図と bib」 で、 対象版の組版を確かめたことにならない。 `git worktree add <dir> <rev>` か `git archive <rev> | tar -x -C <dir>` で版ごと取り出して組み、 本文の SHA-256 を記録と照合する。 機械化 = [`scripts/compare-tex-builds.py`](../scripts/compare-tex-builds.py) `--build <REV>:<file>.tex --repo <dir>` (= `git archive` で版ごと展開して組み、 SHA-256 を出す。 `--build-against` で 2 版を組んで log と `.aux` まで比較する。 selftest に「live の図を差し替えても、 revision build はその版の図で組む」 foil を入れてある)。 起源 2026-09-11: 凍結稿の gate を live の図・bst・bib の symlink で組んだ。 対象 commit 以降それらに変更は無く結果は同じだったが、 それは後から `git log <rev>..HEAD -- <files>` で確かめるまで分からなかった。 <a id="overfull-not-a-gate"></a>**overfull hbox は gate に入れない** — 改稿 pass の途中で語順を変えて overfull を消さない。 改行位置は後続の編集で動くので、 最後の最後まで分からない (著者 2026-09-09「こんなんやらんでいいよ。 最後の最後になるまで分からんじゃん」 = 0.7 pt の overfull を 2 回語順変更で消そうとした pass への訂正)。 overfull の掃除は投稿直前の final pass で 1 回だけ、 該当頁を render して見ながら行う (claude-config `latex.md#pdf-visual-verification` 項目 4)。 **追補 (2026-09-12、再発)**: 規則の射程は **改稿 pass だけでなく、 検査の finding の triage と検査 spec にも及ぶ**。 隔離 sandbox の冷目検査が新記法で生じた overfull 6.8 pt を should-fix の finding として返し、 受領側がそのまま「語順で式を行末から外す」 と提案した (著者「そんなものはドラフトの時点で気にしない！これはマジで大事。 そんなことのために語順を変えるとか有り得ない」)。 検査 spec には「overfull / underfull は報告のみ (数と行番号)、 severity を付けない」 と書き、 受領側は overfull を should-fix に格上げしない。 ドラフト段階で overfull を理由に本文の語順・語彙を変えることは、 誰の提案であっても採らない。 **頁の目視 (画像化) も途中の pass の gate に入れない** — 文・数式の推敲 pass では変更頁を画像にして見ない。 gate は頁数・未定義参照・error の機械判定だけで、 目視は final pass で 1 回 (著者 2026-09-11「論文の文章だけ編集してるときにいちいち目でチェックは無駄」、 基準 = claude-config `latex.md#visual-verification-intensity` の「通常の本文・数式編集に毎回適用しない」)。
4. <a id="blame-before-delete"></a>**削除の前に `git blame`。** 規則 4 (削除は verbatim) の運用手順。 削除しようとしている範囲の筆者分布を数え、 **共著者の本文なら削除しない** (付録へ verbatim MOVE + 本文に pointer が下限)。 「査読が短くしろと言った」 は削除の根拠にならない — 量の指示は §2 規則 5 の通り、 越えるなら止めて著者に返す。
5. <a id="cleaned-base-diff"></a>**読み合わせは「清掃版どうし」 の diff で。** 共著 review 中の原稿は着色 (`\red{...}`) と著者間問答を含み、 latexdiff にかけると **着色の差が実質の差を埋める**。 `scripts/review-markup-clean.py` を **基準版と現行の両方に当ててから** latexdiff を回すと、 実質の変更だけが見える。 基準版は「信用できない AI pass の直前の commit」 に固定する (§1 の事例では、 そこが著者と共著者の最後の合意点だった)。 再生成は 1 コマンドの script にして、 **live を変えるたびに回す** (人が「あの PDF は古いかも」 と迷う余地を消す)。
6. <a id="retro-audit"></a>**意図記録の無い過去の pass を後から棚卸しするときは 3 列で。** 「査読が要求したこと」「著者が決めたこと」「実装が実際にしたこと」 を hunk ごとに並べる。 2 列 (決定 vs 実装) だけだと、 実装が **査読の文言をそのまま転記した**箇所を「著者決定の実装」 と誤読する。 ⚠️ **棚卸し自身も誤分類する**: 同日の棚卸しは 89 hunk のうち 2 件を最初に取り違え (共著者本文の削除を「決定通り」、 著者判断そのものの実装を「決定超過」)、 査読レポートの原文と `git blame` を引いて初めて直った。 棚卸しの結論は「読んだ範囲」 を明記して人間に渡す。
7. <a id="carrier-commit-hash"></a>**著者の手編集を carrier として commit するときは、 確認した差分と同じかを hash で確かめる。** 差分を読んで確認した時点で `git diff -- <file> | shasum` を控え、 commit の直前に同じ値かを比べる。 違っていたら commit せず、 新しい差分を読み直す。 行数の比較は弱い (2026-09-11: 確認の後に著者がコメント行を 1 行消していた。 中身は無害だったが、 行数ガードは上の `set -e` の不発で止まらず、 確認していない版を commit した)。
8. <a id="fill-mode"></a>**sidecar の行は `--fill` で一括で埋める。** `check-edit-intent.py --fill <sidecar> --intents <json>` (JSON file の path か、`{` で始まる JSON そのもの) が、 scaffold の空の 種類 / ID / 意図 を、 hunk 番号か旧行番号 (`L<n>`) をキーにした JSON から埋める。 実装+削除 の事前記入は残し、 ## 裁量 の空欄は `--discretion` (既定 なし) で埋め、 同じ呼び出しで検査まで回す。 行の無い key・key の無い行・`|` を含む値は拒否する。 手で正規表現を書いて埋めない (2026-09-11 に同じ埋め込み処理を 10 回近く手書きした)。 identity など project 固有の記録は project 側の script に置く。 **ID は ledger に先に書いてから `--fill`** する: `--fill` は同じ呼び出しで検査まで回すので、 決定 ledger に ID がまだ無いと「ID 実在」 で 11/12 に落ちる (2026-09-13 に 1 回、 ledger 追記を後に並べた chain で発生)。 記録を 1 本の script にまとめるなら順序は ledger → `--fill` → identity → commit。 改名のように全 hunk が同じ意図の pass は、 scaffold の表から hunk 番号を読み、 同じ 種類 / ID / 意図 を並べた JSON を機械で作って渡す (hunk を手で書かない)。
9. <a id="editor-open-precheck"></a>**著者が editor で同じ file を開いている日は、 書き込みと commit を、 それぞれ exit code の検査と同じ command 行に置く: `check && 書き込み && mark`、 組版と記録のあとに `verify && commit`。** 検査と書き込みの間、 書き込みと commit の間に時間が空くと、 その間に保存された著者の編集が**自分の commit に、 自分の commit message で**入る。 2026-09-13 に 3 回: `git status --short <file> && 適用 && commit` は変更があっても exit 0 で鎖が止まらなかった (2 回 = 検査は表示でなく exit code で止める)。 検査を編集と別の tool 呼び出しにした版では、 検査 (exit 0) と次の呼び出しの書き込みの間に著者が保存した (1 回 = 別の呼び出しは窓を広げる)。 書き込みから commit までの組版・記録の時間も同じ窓なので、 commit の直前に `verify` を置く。 これは並行 session の staging race (claude-config `multi-session-coordination.md#staging-window-race`) の人間 editor 版で、 `git commit -- <path>` では防げない (同じ path なので)。 機械化 = [`scripts/editor-guard.py`](../scripts/editor-guard.py): `check <file>` は変更ありで exit 3 (差分を表示し hash を保存)、 `mark <file>` は書き込み直後の内容を記録、 `verify <file>` は mark のあとに着地した差分を表示して exit 4、 `carrier <file> -m MSG` は表示した差分と同じ hash のときだけ著者の変更を commit する (規則 7 を兼ねる、 読んだ後に着地した編集は exit 4 で止める)。 `check` が変更を見つけたら自分の書き込みを見送り、 差分を読んで carrier してから当て直す。 `verify` が exit 4 なら file には自分の編集とあとの差分が両方入っている = 誰の変更かを記録してから commit を決める。 ⚠️ どちらの場合も自動で carrier しない — 差分を読む手が抜けると規則 7 が空になる。
10. <a id="abort-must-stop-the-commit"></a>**適用が abort したら commit も止める。** 規則 1 の再掲だが、破れ方は毎回同じ = 適用 (heredoc の python) と commit を `;` で並べる。 適用が assert で落ちても shell は次へ進み、**その時たまたま working tree に在った他人の編集だけが、自分の message で commit される**。 適用 → gate → commit は `&&` で 1 本に繋ぐか、適用だけを単独 commit で回す。 2 例目 = 2026-09-12 (`a15bad0`: message は段落 MOVE、中身は著者が打った空行 3 行)。
11. <a id="question-is-not-an-order"></a>**著者の疑問形の指摘は、 変更の指示ではなく見立ての依頼として扱う。** 「A と言った直後に B は矛盾では？」 のような問いには、 まず物理・論理として矛盾かどうかを答え、 直し方を 2〜3 通り見せて、 選ばれてから当てる。 2026-09-13: 問いに対して並べ替えを当てて push した直後に著者が「いや、 戻して」 → revert。 revert で原稿が直前の commit と一致したら (net 差分 0)、 sidecar は作らず decision ledger に 1 行 (何を当て、 なぜ戻したか) を残す。
12. <a id="diff-base-is-the-authors-version"></a>**「私が用意した版との diff」 の基準は、 著者が最後に「完成」 と言った commit。** 記録に残る標準の基準版 (例: 査読対応前の版) と別に指定されうるので、 基準の hash を file 名に入れて別 file で作る (`latexdiff-<base>-vs-live.pdf`)。 基準が曖昧なら候補の commit と日時を 1 行で示して確かめる (2026-09-13: 標準の基準版で作って開き、 著者の意図した「今日さっき終わった版」 と違った)。 regen script は基準と出力先を環境変数で差し替えられるようにしておく。
13. <a id="identity-covers-every-hashed-file"></a>**identity を記録する道具は、 audit が hash を宣言する file を全部更新する。** 原稿 `.tex` だけを見る記録 script だと、 `.bib` だけを変えた pass で audit の「refs SHA-256」 が FAIL する (2026-09-13 に 2 回、 手で hash を書き直した)。
14. <a id="ruling-on-a-proposal"></a>**著者の裁定が「提案書に対する可否」 のとき、 提案書から変えた点は全部 裁量**。 保護領域 (表題・概要・序論・結論・式) を含む改稿は、 具体的な文面を提案書 (作業ノートの Markdown。 abstract を持つ `.tex` の写しは scratch でも gate の範囲に入る) で見せ、 領域ごとの可否をもらってから当てる。 裁定が起票側の再検討を条件にした可否なら、 再検討で直した点 (記号の変更、 言い直し、 足した 1 文) は裁定の範囲外として hunk ごとに裁量の節へ挙げ、 提案書の冒頭に「block は提案時点、 正本は原稿」 と記す。 移設を伴うなら、 verbatim の移設だけの commit を先に切り、 参照先の結び直し (this / the opposite / 指示語の先) は次の commit で行って裁量に挙げる。 1 つの sidecar が両 commit の範囲を覆う。

**2026-09-08 追記 (研究 LaTeX project の 22 pass の実測)**:
- **gate の exit code を pipe で潰さない**: `check-edit-intent.py … | tail -1` は tail の exit 0 で `&&` 連鎖を通してしまい、 FAIL (11/12) の sidecar が commit された。 `set -o pipefail` を置くか、 検査を単独 command にしてから `&&` で commit に繋ぐ。
- **hunk → 意図の対応は `git diff -U0 <base> <head> -- <file>` の hunk 一覧を見てから埋める**: scaffold の「位置」 だけでは何の hunk か判別できない (14 hunk の pass で誤記入しかけた)。
- **net 削除の hunk の 種類**: scaffold は `実装+削除` を仮置きする (2026-09-09 から)。 裁量なら `裁量+削除` に書き換える。 「削除」 単独は語彙違反で FAIL。
- **同じ file を触る pass が続く日は 1 pass 1 sidecar のまま**: 22 pass = 22 sidecar でも検査は 1 秒。 まとめると hunk の対応が取れなくなる。

## <a id="submission-cleanup"></a>8. 投稿前の清掃 (review markup)

共著 review が終わったら、 着色と問答を落とす。 `scripts/review-markup-clean.py` が 3 つだけする:
着色の解除 (中身は保持) / 問答ブロックの削除 (`--qa-prefix` で指定した著者イニシャル等で始まるもの) / 自動日付の非表示 (`--suppress-date`)。
コメント行の中は触らない。

```bash
python3 scripts/review-markup-clean.py paper.tex clean.tex \
    --macro red --qa-prefix '\bf [XX]' --qa-prefix '[YY:' --suppress-date
```

- **削除した問答は sidecar の 削除 節に verbatim で残す** (§2 規則 4)。 返答の中身が本文・脚注に反映済みであることを確認してから消す。
- 清掃の実行自体が 1 つの実装 pass。 sidecar を作り、 決定 ledger には「いつ、 誰の指示で、 何を落としたか」 を書く。 **共著 review 中に前倒しで実行するなら、 それは著者の明示指示による決定の上書き** (deferral の supersede) として記録する。
- 清掃版は投稿用であると同時に §7-5 の diff の材料になる。 同じ script を使うことで「読み合わせで見えていたもの」 と「投稿するもの」 がずれない。

## <a id="examples"></a>9. 実例 ledger

| 日付 | 事例 | 結果 |
|---|---|---|
| 2026-09-01 → 09-08 | 別ベンダー AI が private paper repo の原稿を 89 hunk / 474 行改稿、 意図記録なし。 1 週間後に著者側 session が 89 hunk を ledger と人手で突合 | 決定超過 2 件 (消すなと決めた文の削除 = 同日復元 / 「圧縮」 に対する共著者 96 行の削除 = MOVE か復元を著者判断) + 棚卸し自身の誤分類 1 件 (report との突合で訂正) を事後発見。 著者規範「共著者の本文を単独判断で削除しない」 (規則 4) も同日に明文化。 本 doc + `check-edit-intent.py` の起源。 遡及 sidecar は作らず、 棚卸し record が代替 |
| 2026-09-08 | 同じ原稿で、 著者が居る状態で 1 日に 9 pass (採用・語の統一・投稿前清掃・abstract 4 件・題扉の余白)。 各 pass で sidecar + 組版 gate + 清掃版どうしの diff 再生成 | 全 pass が gate PASS。 事故は 1 件 = 記録を適用前に commit (§7-1 の由来)。 棚卸しは 2 件の誤分類を経て確定 (§7-6) |
