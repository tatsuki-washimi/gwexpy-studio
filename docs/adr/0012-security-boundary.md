# ADR-0012: セキュリティ境界 — 入力は untrusted、暗黙のコード実行を全面禁止

- Status: Accepted
- Date: 2026-08-16
- 出典: 構想メモ「10. セキュリティ境界」、Deep Research の security 横断調査
  （MATLAB / ImageJ / Glue / napari の trusted-code 前提の確認）

## Context

Studio は drag & drop でファイルを受け取るため、入力ファイルは untrusted input として
扱う必要がある。参照した5製品はいずれも基本的に trusted scientific desktop
environment を前提にしている: MATLAB は MAT-file `load` のコード実行リスクを公式に
警告し、ImageJ/Fiji の script は application classpath 上の任意 class にアクセスでき、
Glue は起動時に current working directory の `config.py` を探索・実行し、napari は
remote script 実行について「信頼する script のみ使え」と公式に警告している。
untrusted file を明示的な設計要件とする Studio は、**5製品より意図的に厳しい境界**が
必要である。

ここでいう sandbox は完全な OS-level sandbox を意味しない。「untrusted な data /
project / script / plugin が任意コード実行や想定外の filesystem/network アクセスを
引き起こすことを防ぐ境界」を指す。

## Decision

自動的にしてはいけないこと（禁止リスト）:

1. **pickle の自動 load**（project 形式にも使わない — ADR-0007）。
2. **`.py` / `.C` 等の自動実行**。script 系ファイルは data file と同じ drag & drop
   経路に乗せず、明示的な「Open Script」workflow に分離する。
3. **project reopen 時の script 無確認実行**。project を開くだけでは何も実行されない。
   生成 script の実行は常に明示操作。
4. **CWD からの config 自動探索・実行**（Glue の `config.py` 方式の反面教師）。
   untrusted directory でアプリを起動しても任意コードが走らないこと。
5. **schema 不明の HDF5 等を勝手に解釈して深く読む**こと、および **external command の
   暗黙実行**。

段階化された reader パイプライン:

```
extension / magic bytes
        ↓
safe lightweight inspection（メタデータのみ）
        ↓
candidate readers の提示
        ↓
ユーザーが明示的に reader を選択（自動判定結果は明示表示）
        ↓
full load
```

- format auto-identification の結果と optional backend の失敗はユーザーに明示する。
- 高リスク・native-code-heavy な parser は将来 subprocess 隔離・resource limit を
  検討できる（worker プロセス分離 = ADR-0003 が既にその足場になる）。
- v0.x での現実的な優先順位は「非実行形式（ADR-0007）」「schema validation」
  「no implicit code」「reader allow-list」「size/depth limits」。完全な
  cross-platform OS sandbox は実装コストに見合わないため要求しない。

plugin の trust model（ADR-0010 と接続）:

```
Built-in plugin        trusted
Installed plugin       trusted code（ユーザーが pip install した事実を trust の根拠に）
Project-local plugin   デフォルト無効
Downloaded plugin      自動実行しない
```

Python plugin を通常 import する限り Studio プロセスと同権限で動くため、
「安全でない plugin を Python sandbox で動かす」ことは目指さない。必要になったら
別プロセス隔離を選ぶ。

## Consequences

- reader の UX に一段階（inspection → 選択）が挟まる。Quick Look の軽快さとの
  トレードオフは「既知の安全な形式は既定 reader を事前選択済みにする」等の UI 側
  工夫で吸収する（境界そのものは緩めない）。
- worker プロセス（ADR-0003）が parser 隔離・クラッシュ封じ込めの実装点を兼ねる。
- remote script 実行機能は default では提供しない。
- この ADR は機能追加のたびに参照されるチェックリストとして機能する（「この機能は
  暗黙実行を増やしていないか」）。

## 検討した代替案

- **5製品と同じ trusted 前提**: 対象ユーザー（初心者が任意の場所で任意のファイルを
  開く）と drag & drop 中心の UX に対して前提が成立しない。
- **完全な OS-level sandbox（コンテナ/seccomp 等）**: cross-platform 実装コストが
  現段階の規模に見合わない。プロセス分離 + 非実行形式 + 明示実行で v0.x の脅威
  モデルには足りる。将来、subprocess plugin host と併せて再評価する。
