# ADR-0009: PlotSpec 抽象 — Matplotlib Figure を project state にしない

- Status: Accepted
- Date: 2026-08-16
- 出典: 構想メモ「12. PlotSpecを作るか」および ADR-009 節

## Context

GUI から `ts.plot()` を直接呼んで Matplotlib Figure を canonical state にしてしまうと、
Python export、fast preview renderer への差し替え、style 編集、plot の復元、project
保存のすべてが Figure オブジェクトの内部状態に縛られる。Figure は直列化に向かず、
renderer 非依存の宣言としても使えない。

## Decision

視覚化の**宣言**（PlotSpec）と**描画**（renderer）を分離する。

```
PlotSpec
├── source object refs（DataObjectRef の id）
├── plot kind（line / spectrogram / ...）
├── xscale / yscale
├── xlim / ylim
├── title / labels / legend
└── styles / annotations

             ┌── MatplotlibRenderer（canonical、再現可能・エクスポート可能）
PlotSpec ────┤
             └── FastPreviewRenderer（将来: PyQtGraph 等、downsampled・高速）
```

- 科学データは gwexpy、**視覚表現の宣言は Studio** という境界にする。
- canonical renderer は Matplotlib（ADR-0002）。「GUI で見た plot ≈ Python script で
  再生成した plot」を成立させる。
- PlotSpec は domain model の一部（ADR-0005）として JSON 化され、project manifest
  （ADR-0007）に保存される。
- preview 用データには `preview=True` / decimation 率 / 元ノード参照を明示し、
  最終的な科学成果物と混同しない（ADR-0006 の provenance 方針と整合）。

## Consequences

- plot の再現は「PlotSpec + 参照オブジェクトの再計算」で行える。Figure を
  直列化する必要がない。
- renderer を後から追加・差し替えても（PyQtGraph、GPU renderer）、科学的データモデル
  と project 形式に影響しない。
- plot 上の direct manipulation（軸ラベル編集、範囲選択）は「PlotSpec の変更」として
  モデル化され、UI undo（ADR-0006）の対象にできる。
- Python exporter は PlotSpec から `plot()` 呼び出しとスタイル設定コードを生成できる
  （プロトタイプでは対象外、beta までに）。

## 検討した代替案

- **Matplotlib Figure を直接保持・pickle**: 直列化の脆さと任意コード実行リスク
  （ADR-0007）で失格。
- **renderer 固有の設定オブジェクトをそのまま保存**: renderer を差し替えた瞬間に
  project が読めなくなる。
- **plot を毎回ゼロから設定させ状態を持たない**: Quick Look 用途では成立するが、
  project 再開時に「同じ見た目」を復元できず、Signal Analyzer の display 設定
  非保存と同じ不満を生む（ADR-0007 の Context 参照）。
