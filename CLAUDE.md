# CLAUDE.md — ai-collaboration

**AI との協働で研究を回すための運用基盤 (layer 1、public)。** 「AI に何を任せ、何を人が確かめるか」を、規約 (何を検査するか) + 運用 (どう回し続けるか) + 道具 (機械 gate・runner・sandbox) の三点セットで持つ。vendor 中立 (Claude Code / Codex / 別ベンダーの pass を同じ規律で扱う)。

2026-09-06 に [`claude-config`](https://github.com/odakin/claude-config) から分離 (= あちらは Claude Code の setup / hooks / domain 規約の harness に戻る、本 repo は AI 協働の platform として育てる)。分離の判断と段階は `DESIGN.md`。移設 file の git 履歴は `git format-patch` で持ち込み済 (= 「実践が講演に先行した」 等の credit 主張の根拠)。

## 構造

```
ai-collaboration/
├── AGENTS.md / CLAUDE.md / SESSION.md / DESIGN.md / README.md / LICENSE / .gitignore
├── conventions/
│   ├── physics-verification-cycle.md   # 何を検査するか: 4 station / 機械 anchor / foil / tier / 3 状態 / verify-to-learn /
│   │                                   #   第二の目 / rubric 事前登録 / 止まる規律 / cross-vendor / campaign 運用 A-K
│   ├── verification-cycle-ops.md       # どう回し続けるか: 6 原則 / 導出 state 機械 / 台帳 3 種 + retro / 無人層 / fresh session の手順
│   ├── cold-eyes-isolation.md          # 第二の目の隔離: 汚染経路 7 口 / 封じた sandbox / spec に書いてよいこと / 審査文書の変種 / 掲示板・別の機械の受け手の変種 / 受領後の汚染 grep
│   ├── edit-intent-record.md           # AI による原稿改稿の意図記録: 1 pass 1 sidecar (hunk → finding / decision / 種類 = 実装・裁量・削除) / 裁量枠 / 削除 verbatim + 共著者本文の単独削除禁止 / 量の指示 / commit 前 gate / 依頼 spec の 3 行 /
│   │                                   #   §7 実装 pass の作業規律 (当てる→組版 gate→記録、 anchor assert、 削除前の blame、 清掃版どうしの diff) / §8 投稿前の清掃
│   └── delegated-work-packages.md      # 相手側の AI への作業委譲: 判断と実行の分離 / 常設 3 層 (状態・手順・定義、README は入口だけ) + WP 1 本 = 1 セッション / WP の 7 要素 /
│                                       #   受入基準は依頼側の独立実装の出力 / 全部書いて ready だけ着手 / 書き込み zone / 結果ノート / 解釈は書かせない
├── board/                              # AI session どうしの掲示板の engine (board.py ほか)。 運用の契約 = board/CONTRACT.md (入口 = board/README.md)、 判断 = board/DESIGN.md
│                                       #   掲示板 1 枚 = repo の中の board.json + events/ (owner 専用 = 層3 / 共同研究者 = 層2 の `<project>/board/`)
├── docs/state-discrimination.md       # 状態識別の一般数式・凸錐と座標の仮定・certificate の正本
├── template/                           # clone-and-run skeleton of a private verification repo (scripts/init-verification-repo.py が展開)
├── examples/verification-repo/         # 完結した見本 campaign 1 本 (spec / ledger / check+foil / results AUTO block / retro + hoist)
└── scripts/
    ├── README.md                       # docstring から生成する全数索引
    ├── generate-script-index.py        # README を --write / --check、CI が drift を拒否
    └── *.py                            # 検証器・library・sandbox/receipt tooling、全て --selftest
```

全 script は `--selftest` を持つ。各 file の 1 行説明は file 冒頭の docstring が正本で、[`scripts/README.md`](scripts/README.md) は生成 view。

## 4 層モデルでの位置

layer 1 (public、全 Claude Code / Codex ユーザー向け)。**依存できるのは layer 1 のみ** (`claude-config` とは相互参照可 = 同じ層)。個人の instance (campaign dir・台帳の中身・launchd routine・hook 配線) は owner の private layer に置き、本 repo には kernel だけを書く (kernel-up / instance-down、正本 = `claude-config/docs/personal-layer.md`)。

## 使い方 (最小)

- 規約を読む順: `physics-verification-cycle.md` (§1 サイクルの形 → §15 campaign 運用) → `verification-cycle-ops.md` (§5 fresh session の手順) → 必要なら `cold-eyes-isolation.md`
- 自分の検証 repo を作る: 必須 4 file + `campaigns/<date>-<slug>/{spec.md, ledger.yaml}`、pre-commit から `ledger-commit-cadence-gate.py --pre-commit --worker-scope-env CAMPAIGN_WORKER_DIR`、完了時 `verification-campaign-report.py <dir> --run --write`、受領後 `--carryover --write` と `--index --write`。schema は `physics-verification-cycle.md#campaign-tooling` A。非有界な moment operator と有限窓を扱う場合は同 doc の [`#unbounded-moment-domain-audit`](conventions/physics-verification-cycle.md#unbounded-moment-domain-audit) を追加で使う
- 第二の目を別 session に出す: `make-review-sandbox.py create <slug> --spec REVIEW-SPEC.md --include <paper.pdf>` → cwd を sandbox に pin して spawn → `collect`
- 掲示板で別 vendor・別の機械の session に盲検を頼む: comment を剥がした写しを commit して `board.py request --review-target <写し>` (要約が査読に読める依頼は、 写しか `--not-blind` を求められる)、 判定は受け手の機械の sandbox の新しい session で ([`cold-eyes-isolation.md#board-blind-variant`](conventions/cold-eyes-isolation.md#board-blind-variant))。 写しの検査 = [`check-review-target.py`](scripts/check-review-target.py)
- 同じ reviewer を再開する前後で入力・旧成果物の不変性を確認する: [`review-artifact-snapshot.py`](scripts/review-artifact-snapshot.py) の `capture ROOT --out BASELINE.json` → `check ROOT --baseline BASELINE.json`。baseline は tree の外へ保存する。追加 file は別列挙され、回収可否の判断は呼び元に残る。検査中は writer を止め、詳細な除外・制約は script 冒頭を読む。
- 完成稿を別 session に**非盲検で**通読させる (repo を読ませ、報告だけ): [`template/READTHROUGH-SPEC-non-blind.md`](template/READTHROUGH-SPEC-non-blind.md) を原稿 repo に写して埋め、末尾の hand-off prompt を渡す → 受領は同 file の Receipt 節 (finding ごとに現稿で再読・reviewer の検算を再実行・真 / 誤読 / 決定と衝突 の 3 分類)
- AI に決定 ledger どおりの原稿改稿を実装させる: 依頼 spec に `edit-intent-record.md#requester-spec-line` の 3 行 → 実装側は `check-edit-intent.py --scaffold … --out review/edit-intent-<date>.md` → 種類 / ID / 意図 を埋める → 同 script の検査 ALL PASS → 原稿と同じ pass で commit。 受領は裁量枠から読む / --fill (JSON から 種類・ID・意図 を一括で埋めて検査)
- 自分が実装側のとき: 当てる → 別 dir で組版 → 記録 → commit の順 ([`#apply-then-record`](conventions/edit-intent-record.md#apply-then-record))。 削除の前に `git blame`。 共著 review 中の原稿の読み合わせは `review-markup-clean.py` を基準版と現在版の両方に当ててから latexdiff ([`#cleaned-base-diff`](conventions/edit-intent-record.md#cleaned-base-diff))、 投稿前清掃も同じ script ([`#submission-cleanup`](conventions/edit-intent-record.md#submission-cleanup))
- 「記法を統一しただけ」 型の機械的改稿を受け取った / 出した: [`check-rename-purity.py`](scripts/check-rename-purity.py) `--before … --after … --map … --strict` で **new→old の逆写像を後版に当てて前版と diff** し、畳まれずに残った行だけを読む (`--forbid` で旧綴りの残存を comment 行まで走査)。綴りの棚卸しは同 script の `--census '<regex>'` (綴りごとに出現数・行範囲・住んでいる節を出す = 2 綴りが別の節に居るのが一目で分かる)。1 文字の記号を macro に替える pass (frame 添字を太字に、 等) の前は [`tex-letter-census.py`](scripts/tex-letter-census.py) で候補を数式の文脈つきで全部並べ、 添字の中の案を行ごとに読んで当てる (同じ字が次元・冪・種類のラベルにも使われる = 案は判定ではない)。組版は [`compare-tex-builds.py`](scripts/compare-tex-builds.py) で 2 版の log と `.aux` を比べる (**総頁数の一致は頁割りが動いていない証拠でない**)。`--build <REV>:<file>.tex` は `git archive` で版ごと取り出して組むので、凍結稿の gate が live の図を掴まない ([`#frozen-revision-build`](conventions/edit-intent-record.md#frozen-revision-build))。他人が組んだ PDF を見て報告する前に `--reproduce` で自分の build が再現することを証明する。規律 = [`physics-verification-cycle.md#referee-side-kernels`](conventions/physics-verification-cycle.md#referee-side-kernels) kernel 17-21、人が読む側の検査 = [`paper-audit.md#notation-rename-sweep`](../claude-config/conventions/paper-audit.md#notation-rename-sweep)
- 共同研究者の AI session とも依頼・提出・受領を残したい: 共同研究の repo に掲示板を置く (`python3 board/board.py init --root <project>/board --audience collaborators --encryption none --sources <project>` → commit)。 共同研究者の手元の engine は、 その project に claude-config の collaborator-check の `repo` 行を入れて session 開始時に自動で clone・更新させる (init が install の command を出す。 一般則 = `claude-config/conventions/shared-repo.md#sibling-repo-auto-clone`)。 使い方・読む人の gate・owner 専用の掲示板との分け方 = [`board/CONTRACT.md`](board/CONTRACT.md#boards-and-audiences)
- 数か月の解析を共同研究者 (人間 + その AI) に実行してもらう: 定義を 1 つの SPEC に固め、1 セッション分の作業書 (WP) に割り、**受入基準は自分の独立実装で出した数値**で埋める ([`delegated-work-packages.md`](conventions/delegated-work-packages.md))。解釈・結論・基準値の書き換えは作業者に渡さない ([`#what-the-worker-must-not-write`](conventions/delegated-work-packages.md#what-the-worker-must-not-write))
- 印字した係数の符号を外部の絶対量で守る: project に登録簿 `sign-anchors.json` (印字量 → 外部 anchor → 全体反転 foil) → `check-sign-anchors.py --run --deferrals` (gate = anchor が現稿で PASS し、 全体反転の foil で assertion により FAIL、 carrier の無い「規約差」 0) / `--fleet-scan` (fleet のどの検査が変換を見分けるか) / `--readers` (どの検査が原稿を実行時に開くか)。 規則 = claude-config `paper-audit.md#absolute-sign-external-anchor` / `#convention-difference-closure`
- 共変作用の等価性・境界項・多項式性を検査する: [`physics-verification-cycle.md#action-equivalence-and-polynomiality`](conventions/physics-verification-cycle.md#action-equivalence-and-polynomiality) の順で、Euler微分、任意lapse、係数の特殊化、動く補助場制約、非等方sliceを分けて見る。再利用コードは [`density_frame_algebra.py`](scripts/density_frame_algebra.py) と [`covariant_action_audit.py`](scripts/covariant_action_audit.py)。個別論文の判定はowner側のprivate campaignに残す

## 安全規則 (public repo)

`claude-config/CLAUDE.md §安全規則` と同じ: 実名・email・非公開 repo 名 (例外 list 以外)・所属・金融・他ユーザー名を file 本文 / commit message / PR に書かない。**識別子を含まない文章でも leak は成立する** — 審査・査読中の文書や未公開原稿から文言を verbatim で引く / 図から読んだ実測値を写す / それを selftest fixture に使う、 は既存 gate を素通りする ([`claude-config/CLAUDE.md#non-identifier-content-leak`](../claude-config/CLAUDE.md#non-identifier-content-leak))。 campaign の finding (他者論文の誤り疑い) は本 repo に書かない (default 非公開 = `physics-verification-cycle.md#verify-to-learn`)。

## 規約参照

- 共通: `claude-config/CONVENTIONS.md` (git / 必須 file / sweep / 安全規則)
- 委譲と返送 spine (Claude 内): `claude-config/conventions/multi-session-coordination.md` (Phase 2 で本 repo へ移設予定 = DESIGN)
- Codex 統合 (AGENTS / PARITY / setup-codex): `claude-config/codex/` (同上)
- 無人 routine の一般則: `claude-config/conventions/scheduled-tasks.md` / `multi-machine-state.md`

## 検査

```bash
set -e
for s in scripts/*.py; do python3 "$s" --selftest; done
python3 board/board-view.py --selftest && python3 board/board-session-start.py --selftest && python3 board/board_transport.py --selftest && python3 board/board-post-reach-check.py --selftest && python3 board/test_board.py && python3 board/board_items.py --selftest   # git-crypt が要る (暗号化 transport の試験)
AGENT_BOARD_TRANSPORT=clone python3 board/test_board.py   # 旧 transport (1 版だけ残す) も同じ試験を通す
```

CI = `.github/workflows/checks.yml` (全 script の selftest。`secure-new-repo.sh --code` の baseline)。 `board/` の試験は CI に未配線 = 手元で回す (配線は本人の裁定待ち)。失敗した script の終了値を loop で失わない。

## How to Resume

1. `SESSION.md` → 直近の変更と残タスク
2. `DESIGN.md` → 分離の判断 / Phase 2 の trigger
3. 呼び元 (owner の private layer) は path を本 repo に向けている: owner の private 検証 repo の `scripts/*` shim / `odakin-prefs/scripts/check-verification-campaigns.py` / `odakin-prefs/skill/daily-verification-cycle-tick/SKILL.md`。旧 path (`claude-config/scripts/<same name>`) は forwarder として残る
