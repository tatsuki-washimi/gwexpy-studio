# ADR-0006: 科学操作は非破壊として扱い、履歴は DAG にする

- Status: Accepted
- Date: 2026-08-16
- 出典: 構想メモ ADR-004 節（「科学操作はStudio内では非破壊として扱う」）、
  Mantid Algorithm History / napari Labels undo の調査結果

## Context

GUI で `raw → crop → detrend → filter` と操作したとき、元オブジェクトを書き換えるか、
派生オブジェクトを生成するかを決める必要がある。また「undo できること」と
「何が試行されたかの記録」は別問題である。Mantid の history は audit と Python 再生成の
ための semantic な記録であり、一般的な inverse-operation 型 undo ではない。napari の
undo は Labels layer 固有実装に留まり、slice を変えると履歴が消える — application
レベルの設計を最初からしなかった場合の反面教師である。

## Decision

1. **Studio の semantic model では、科学操作はすべて派生オブジェクト生成として扱う**。

   ```
                ┌── crop ──▶ obj-B ── ASD ──▶ obj-D
   obj-A ───────┤
                └── spectrogram ─────▶ obj-C
   ```

   これは gwexpy API 自体を immutable に変更するという意味ではない。Studio の
   operation semantics として非破壊にするという意味である。解析は自然に分岐するため、
   内部モデルは線形 timeline ではなく **Operation DAG** とする。

2. **Scientific history と transient UI undo を分離する**。
   - Scientific history = OperationGraph（crop / filter / fit 等）。
   - Transient UI undo = QUndoStack（dock resize / legend 移動 / axis range 等）。
   - 「legend を 20px 動かす」と「highpass filter を適用する」を同じ undo スタックに
     入れない。

3. **Undo しても provenance から消さない**。undo は「active graph head を戻す」操作で
   あり、audit trail（operation B executed → reverted）は残る。failed operation も
   履歴から消さず、warning とともに参照可能にする。

4. provenance の各記録には algorithm 名と parameter、gwexpy/依存の version、
   source file の path/size/mtime（可能なら hash）、preview 用 downsampled data と
   実処理対象の区別、を残す（ExecutionRecord、ADR-0005）。

## Consequences

- undo / branch / before-after 比較 / cache / 再現 / Python export（ADR-0008）が
  すべて「DAG のノード参照」として単純化される。
- メモリ管理が課題になる（派生オブジェクトが溜まる）。worker の object store に
  参照カウント/明示 delete を持たせ、必要なら再計算（replay）で復元する方針
  （ADR-0007 の recomputable 原則）で吸収する。
- UI は「簡単な linear history view」と「詳細な node graph view」の両方を将来
  提供できる（内部が DAG なので、線形表示はその特殊ケースになる）。

## 検討した代替案

- **in-place 変更 + inverse operation で undo**: 逆演算が定義できない操作
  （filter、fit）が多く、破綻する。
- **CAD 型の完全線形 timeline**: 解析の自然な分岐（同じ TimeSeries から ASD と
  Spectrogram の両方を作る）を表現できない。
- **undo と provenance の一本化**: undo でノードを消すと「何を試したか」が失われ、
  再現可能な科学解析環境という目的（ADR-0001）に反する。Mantid が history を
  undo と別概念にしているのと同じ判断。
