# ADR-0005: canonical domain model は Qt 非依存の pure Python にする

- Status: Accepted
- Date: 2026-08-16
- 出典: 構想メモ ADR-003 節（「Canonical domain modelをQtから独立させる」）、
  Glue（glue-core / glue-qt 分離）・napari（Python model / Qt / VisPy 分離）の調査結果

## Context

最も避けたいのは `QTreeWidgetItem` / `QTableWidgetItem` / `QUndoCommand` が科学的
state そのものになることである。参照した先行例のうち、Glue は core を GUI 非依存の
別パッケージに分離し「Qt が無い環境で core テストが通る」ことを設計規則にしている。
napari も Python model / Qt presentation / VisPy rendering を分離し、Qt は model の
event を購読して同期する構造をとる。Mantid も Framework ← mantidqt ← Workbench の
一方向依存である。

## Decision

Studio の canonical model を pure Python で定義し、Qt は projection に徹する。

```
DataSourceRef    外部データの所在（file / directory / URI）
DataObjectRef    worker 内オブジェクトへの stable reference
                 （kind, shape, dtype, unit, axes 等のメタデータ snapshot）
Operation        何を実行するか（operation_id + parameters + inputs → outputs）
OperationGraph   object / operation の DAG（ADR-0006）
ExecutionRecord  実際にいつ・どの環境で実行されたか
                 （timestamp, duration, warnings, error, versions）
PlotSpec         視覚化の宣言（ADR-0009）
Project          集約ルート（ADR-0007 の保存単位）
```

- **Operation と ExecutionRecord を分離する**。`ASD(fftlength=4)` という科学的意味は
  Operation、「2026-08-16 11:32 に gwexpy 0.1.14 で 1.47 s かかり warning が出た」は
  ExecutionRecord。再実行しても Operation の identity は変わらず、ExecutionRecord が
  追記される。
- **DataObjectRef は worker が報告したメタデータ snapshot** であり、科学的意味論には
  関与しない（意味論は Operation 側が持つ）。
- Qt へは adapter で投影する:
  `OperationGraph → QAbstractItemModel → TreeView / History / Inspector`。
  Qt Model/View 自体が「一つの model を複数 view に提示する」設計を想定している。
- domain 層は gwexpy にも依存しない（gwexpy を import してよいのは worker 側の
  実行系と operation adapter の apply 内部のみ）。これにより UI プロセスは gwexpy を
  import せずに動ける（ADR-0003 の起動コスト対策と一体）。

## Consequences

- headless プロトタイプ（GUI なし）で domain model の大部分を検証できる。
  テストも Qt なしで走る。
- 依存関係に PySide6 が現れない増分を作れる — 「依存に無い」こと自体が本 ADR の
  機械的な検証になる。
- Qt adapter 層を書くコストは増えるが、script / GUI / test が同じ state model を
  共有できる（Glue/napari で実証済みのパターン）。
- serialization（ADR-0007）と code generation（ADR-0008）は domain model だけを
  入力にできる。

## 検討した代替案

- **Qt widget 直結の状態管理**: 最短で動くが、保存・コード生成・テスト・将来の
  renderer 差し替えのすべてが Qt に汚染される。Glue/napari が分離を明文化している
  のはこの失敗を避けるためであり、追随する。
- **pydantic 等による model 定義**: バイナリ依存が増え、serialization 形式が
  ライブラリ都合になり migration の制御点が曖昧になる。クラス数は少ないので
  stdlib dataclass + 明示的 to_dict/from_dict で足りる（from_dict が validation と
  将来 migration の単一挿入点になる）。
