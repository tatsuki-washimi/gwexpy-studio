# ADR-0008: OperationGraph を source of truth とし、Python/Marimo は生成物にする

- Status: Accepted
- Date: 2026-08-16
- 出典: 構想メモ ADR-006 節（「Python/MarimoはOperationGraphから生成する」）、
  Mantid GeneratePythonScript / Signal Analyzer の分散 generator / ImageJ Recorder の調査結果

## Context

構想の初期案は「scientific state の source of truth は Python/Marimo スクリプト」だった。
しかし検討の結果、次の理由で修正された:

- 任意の Python を GUI 構造へ逆変換するのは一般に困難（round-trip が成立しない）。
- Python の AST 構造は release 間で変わり得ると公式に明記されており、serialized AST や
  Python source を Studio の永続内部形式にすると migration が困難になる。
- Signal Analyzer は preprocess 用と各解析用で code generator が分散しており、
  「GUI では再現できたが export した script 単体では再現しない」失敗例が公式 Tips に
  記載されている（derived signal の export 忘れで生成 script が動かない等）。

一方 Mantid は「workspace に適用された algorithm の semantic history」から再実行可能な
Python を生成することに成功しており、marimo は notebook を pure Python として保存する
ため export 先として相性がよい。

## Decision

1. **科学的意味の source of truth は versioned OperationGraph**（ADR-0006 の DAG）。
   Python / Marimo は「必ず生成できる canonical export」とする。
2. **exporter は OperationGraph の唯一の consumer にする**。Python exporter /
   Marimo exporter / provenance exporter は同じ DAG だけを読む。機能別に独立した
   generator を作らない（Signal Analyzer の反面教師）。
3. **GUI event からコードを生成しない**。`mouse click → Python` ではなく
   `Operation semantic → Python`。巨大な文字列 template の寄せ集めも避け、
   パラメータのリテラル化は単一の render 関数に集約する。
4. **operation_id は stable ID**（例 `"timeseries.asd"`）+ operation schema version。
   Python callable path（`gwexpy.timeseries.TimeSeries.asd`）を永続 ID にしない。
   将来メソッドが rename されても migration で追える。
5. **生成コードは Studio runtime に依存しない**素の gwexpy public API 呼び出しにする:

   ```python
   from gwexpy.timeseries import TimeSeries

   raw = TimeSeries.read("/path/to/source.h5", format="hdf5")
   cropped = raw.crop(1000000001.0, 1000000010.0)
   detrended = cropped.detrend("linear")
   asd = detrended.asd(fftlength=4.0)  # fftlength: 4 s
   ```

   `studio.apply("filter", ...)` のような Studio でしか実行できないコードを成果物に
   しない。
6. **単位付きパラメータは canonical 単位へ正規化した素の float として呼び出し・生成
   する**。ドメイン/保存形は `{"value": 4.0, "unit": "s"}` で単位を保持するが、
   gwexpy 呼び出しと生成コードには正規化済み float（`fftlength=4.0`）を渡す。
   根拠は実測: astropy 7.2 + gwpy 4.0.1 で `ts.asd(fftlength=4*u.s)` は gwpy の
   `normalize_fft_params` が `bool(Quantity)` を評価して `ValueError` になる
   （構想メモ段階の `4 * u.s` を生成する案はそのままでは動かない）。正規化関数を
   一箇所に集約し、GUI 実行と生成コードの一致を構造的に保証する。
7. **GUI→code 再現性テストを first-class にする**。

   ```
   operation graph → code generation → fresh Python process → re-run
   → result equivalence test
   ```

   を CI/テストの必須項目とし、「GUI 上では動くが生成 script では再現しない」
   failure class をリリース前に検出する。
8. round-trip 保証は段階的にする: (1) Studio 生成 script → 完全再読込を保証、
   (2) 単純な gwexpy script → 可能な範囲で import、(3) 任意 Python → 逆変換は
   非保証。

## Consequences

- ユーザーが生成 `.py` を手で編集した場合、その編集は OperationGraph に反映されない
  （逆変換非保証の帰結）。「編集したら普通の Python として扱う」ことを UI 上でも
  明示する必要がある。
- Marimo exporter は同じ DAG から別のセル構造を出すだけであり、Python exporter と
  意味解釈が分かれない。
- operation schema version の運用（gwexpy 側 API 変更への追随 migration）が
  必要になる。ExecutionRecord に gwexpy/依存の版を記録して照合可能にする。

## 検討した代替案

- **Python source を source of truth にする**（当初案）: 上記 Context の3理由で変更。
- **serialized AST を project 形式にする**: AST の版間非互換で migration 困難。
  `ast` module は生成・検証の中間処理としてのみ利用余地がある。
- **ImageJ1 Recorder 型の stringly-typed 記録**（`"low=20 high=500"`）: 型・単位・
  version・input/output 関係が弱く、schema evolution / unit check / code generation /
  security validation のすべてが難しくなるため不採用。
