# ADR-0003: UI プロセスと Scientific Worker プロセスの分離

- Status: Accepted
- Date: 2026-08-16
- 出典: 構想メモ ADR-001 節（「UIと科学計算を別processにする」）

## Context

GUI スレッドで解析を実行すると、重い処理中に UI が固まり、reader/backend の segfault や
optional native library の failure が GUI 本体を巻き込む。Jupyter は frontend と kernel を
分離し versioned message protocol で結び、VS Code は extension を UI 本体とは別の
Extension Host で動かして不調な extension が UI を阻害しにくくしている。Studio も
同じ構造上の理由で分離が必要である。

## Decision

Studio alpha の時点から、**PySide6 UI プロセス**と**長寿命の Scientific Worker
プロセス**を分離する。

```
Studio UI process                     Scientific Worker process
  Qt / View / Inspector                 object store (obj-001 = TimeSeries, ...)
  OperationGraph 表現       ──cmd──▶    execution / file inspection
  DataObjectRef（メタデータ） ◀─ref──    gwexpy public API → GWpy/NumPy/SciPy
```

- Worker は **persistent（長寿命）object store** を持つ。`read → crop → detrend → asd`
  の中間オブジェクトは worker 内に留まり、毎操作のシリアライズ往復をしない。
- UI 側は gwexpy オブジェクト本体を所有せず、**DataObjectRef**（id, kind, shape,
  dtype, unit 等のメタデータ snapshot）だけを保持する。
- **Qt オブジェクトを worker へ送らない**。gwexpy の import は worker 側に限定する
  （UI プロセスの起動を軽くする効果もある。実測で `import gwexpy` は約 3.8 秒）。
- Worker crash 時は UI から検知・再起動できる。object store は失われる前提とし、
  復旧は OperationGraph の replay（再計算）で行う（ADR-0007 の
  「source files + OperationGraph = recomputable state」の実演になる）。
- `ProcessPoolExecutor` をアプリの backbone にしない。picklable な callable/引数を
  前提とする短命タスクプールであり、大量の gwexpy オブジェクトを長時間保持する
  用途に合わない。
- 将来 `1 UI → N workers` や remote worker へ拡張できるよう、UI は抽象化された
  worker interface だけに依存する（remote 実行自体は延期。ADR-0004 参照）。

## Consequences

- プロトタイプで WorkerProtocol（メッセージ設計、crash 検知、restart、replay）を
  最初に検証する必要がある（headless プロトタイプ Spike 1）。
- 危険度の高い file parser を UI から隔離でき、セキュリティ境界（ADR-0012）の
  実装点にもなる。
- 重い処理のキャンセルは「worker への中断要求 or worker 再起動」として実装できる。
- プロセス間のデータ転送設計が必須になる（ADR-0004）。

## 検討した代替案

- **単一プロセス + スレッド**: GIL により CPU-bound 解析で UI が引っかかる。
  segfault 隔離もできない。
- **`ProcessPoolExecutor` を直接使う**: 上記のとおり長寿命 object store と相性が
  悪い。タスク単位の並列化が必要になった時に worker 内部で使うのは妨げない。
- **毎操作ごとに使い捨てプロセス**: `import gwexpy` 約 3.8 秒の起動コストを
  毎回払うことになり、対話的操作に耐えない。
