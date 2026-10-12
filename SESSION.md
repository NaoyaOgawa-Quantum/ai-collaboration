# SESSION.md — ai-collaboration

> 📌 SESSION.md = 案件ごとの現在地 + 正本への link (進んだら置き換える、 日付を見出しにした節・commit hash・messageId を置かない = 層1 claude-config/CONVENTIONS.md#session-no-durable-record)。 日付つきの節は SESSION-archive.md へ verbatim MOVE 済 (2026-09-28)。

## 現在地：並列 session の用事の claim

`board/board_items.py` を足した (掲示板の checkout の手元の台帳、 `board.py` は不変更)。 使い方は
[CONTRACT](board/CONTRACT.md#item-claims)、 採らなかった案は [DESIGN](board/DESIGN.md#item-claims)。
別の機械への写しは無い = 掲示板の見直しの結論が出てから決める。

## 現在地：Lindblad 観測量の検査

再利用 API と合成 selftest を整備した。次の利用では、
[作用空間とチャネルの境界](docs/lindblad-observables.md)を確認してから、
[生成索引](scripts/README.md)の module を使う。独立検査の原本は呼び元で保持する。

## 現在地：有限積分・外積・式の転記

共通道具を追加した。再利用時は [実装の分担](DESIGN.md#finite-vacuum-and-transcription-tools) と
[主張の依存関係・物理的な解釈](conventions/physics-verification-cycle.md#claim-dependencies-and-observables) を読み、
[生成索引](scripts/README.md)から該当 module へ進む。caller の量子測度・適用範囲・個別判定は各 project が持つ。
次の利用では必要な module の selftest を通し、独立検査の原本を共通実装への別名に置き換えない。

## 現在地：共変作用・密度frameの検査module

作用の等価性・境界項・有限多項式性を再利用可能な形にした。
入口は [検証kernel](conventions/physics-verification-cycle.md#action-equivalence-and-polynomiality)、
実装判断は [DESIGN](DESIGN.md#covariant-action-audit-hoist)、コードは生成された
[script索引](scripts/README.md)から辿る。次の利用時は、制限背景の必要条件と一般共変な十分条件、
有限ambient polynomialと場依存制約を別々に判定する。個別論文の判定はこのpublic repoに置かない。

## 現在地：掲示板の engine (board/)

owner 専用の掲示板から engine を移し、 共同研究の repo に層2 の掲示板を置けるようにした。 入口 = [board/README.md](board/README.md)、 運用の契約 = [board/CONTRACT.md](board/CONTRACT.md#boards-and-audiences)、 判断 = [DESIGN](DESIGN.md#board-engine) と [board/DESIGN.md](board/DESIGN.md#one-engine-many-boards)。 次 = 実際の共同研究の掲示板で、 共同研究者側の session が clone して読めるかを確かめる (読む人の gate は名前と path だけを見る = 文章の中身は書き手の判断)。 session の冒頭に要注意 thread を出す hook = `board/board-session-start.py` (配線は各自の settings、 判断 = [board/DESIGN.md](board/DESIGN.md#session-start-surface))。 新しい session から実際に発火したところはまだ見ていない。

## 現在地：レビュー成果物の不変性確認

再開前後の比較には [snapshot helper](scripts/review-artifact-snapshot.py) を使える。次の利用時は baseline の対象と writer の停止を確認し、追加物の回収範囲は呼び元で指定する。運用入口は [CLAUDE](CLAUDE.md)。

## 残タスク

- 状態識別の数式と道具を追加。入口 = [証明と利用範囲](docs/state-discrimination.md)、[library](scripts/state_discrimination.py)、[検証時の certificate](conventions/physics-verification-cycle.md#state-discrimination-certificates)。reporter は未知の foil crash と marker 後の非ゼロ終了を失敗にし、`--run` の失敗を呼び元へ返す。実装判断 = DESIGN.md。

- Session 宛て board との受領経路の接続は [verification-cycle-ops](conventions/verification-cycle-ops.md#board-receipt-boundary)。一般則の正本は README から既存 home へ参照し、Phase 2 の移設は未実施。

- [ ] Phase 2 の trigger 監視 (= DESIGN の表): Codex runner Pilot A 開始で `multi-session-coordination.md` と `codex/` を移設

- [ ] 掲示板の旧 transport (一時 clone + 全 file の復号 = `board.py` の `snapshot_clone` / `_post_clone`、 `AGENT_BOARD_TRANSPORT=clone`、 test の `*Clone` class) を消す: trigger = 掲示板を使う全機械の engine の checkout が fetch の transport 以降になり、 1 版の間に切り戻しが要らなかったこと。 判断と測定 = [board/DESIGN.md](board/DESIGN.md) の Concurrency and delivery

## 決定ログ

- **2026-09-06: 新設** — 判断は DESIGN.md (分離の理由 / Phase 1 の範囲 / 移し方 = format-patch 履歴 import / stub + forwarder / Phase 2 trigger)
