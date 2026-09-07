# ADR-0007: project format は data-only の versioned JSON にする

- Status: Accepted
- Date: 2026-08-16
- 出典: 構想メモ ADR-005 節（「Project formatはdata-only + versioned」）、
  MATLAB `load` の警告・Glue `.glu` の調査結果

## Context

Studio は project の保存・再開・共有を想定する。Python 公式ドキュメントは、悪意ある
pickle が unpickle 時に任意コードを実行し得るため untrusted pickle を読み込んでは
ならないと明記している。MATLAB も MAT-file の `load` で変数初期化時にコードが実行され
得ると公式に警告している。drag & drop で受け取ったファイルを開くアプリ（ADR-0012）が、
project 形式自体を実行可能な直列化にするのは矛盾である。

また Signal Analyzer は session に display 設定が保存されない部分があり、「開き直したら
同じ見た目になる」という期待の裏切りが起きる。何を保存し何を保存しないかは仕様として
明示する必要がある。

## Decision

1. **project manifest は data-only JSON**（`project.gwxproj`）。pickle、Python object
   serialization、unsafe YAML tag、import path からの任意 constructor 実行を禁止する。
2. **`schema_version` を v1 から必須にする**。読み込み時に自分より新しい版は明確な
   エラーにする。migration の挿入点（from_dict）を最初から一つに定める。
3. manifest には sources / objects / operations / executions / plots / ui_state /
   compatibility（studio・gwexpy の版）を含める。`operations` 配列は OperationGraph
   （ADR-0006）の直列化形であり、in-memory では `Project.graph` フィールドが保持する。
   **JSON Schema**（2020-12）を併置し、形式文書とテストでの検証に使う。
4. **科学的状態の最終成果物は独自形式に閉じ込めない**。

   ```
   analysis.py            # 生成された通常の gwexpy スクリプト（ADR-0008）
   project.gwxproj        # OperationGraph + GUI 状態の manifest
   .gwxcache/             # 削除しても project が成立する派生物のみ
   ```

5. **cache は source of truth にしない**。
   `source files + OperationGraph = recomputable scientific state` を原則とし、
   `.gwxcache/`（materialized array、preview decimation、thumbnail 等）は消しても
   再計算で復元できる。
6. 保存の三層分離（構想メモの整理を採用）:

   | 層 | artifact | 実行可能か |
   |---|---|---|
   | 科学的 source of truth | OperationGraph（manifest 内） | No |
   | 再現可能な canonical 表現 | 生成された `.py` / Marimo | Yes（明示実行のみ） |
   | GUI 状態 | manifest の ui_state（layout, selection 等） | No |

   ※ 当初案では「Python が source of truth」だったが ADR-0008 のとおり
   OperationGraph に変更した。machine-readable provenance を独立 artifact
   （`.gwxprov.json` 等）に分ける案は将来の拡張として残す。

## Consequences

- project を開くだけでは何も実行されない（ADR-0012 のセキュリティ境界と整合）。
  スクリプトの実行は常に明示操作。
- JSON 化できない値（numpy スカラ、Quantity）は境界でプリミティブに変換する規約が
  必要（ADR-0008 の単位表現 `{"value", "unit"}` を使う）。
- 巨大な科学オブジェクトを manifest に埋め込まない（Mantid が project metadata と
  workspace data を分けているのと同じ判断）。再開時のデータ復元は cache または
  replay で行う。
- 「開き直したら何が復元されるか」を manifest スキーマがそのまま仕様として表現する
  （Signal Analyzer の反面教師への回答）。

## 検討した代替案

- **pickle / joblib**: 保存・復元の実装は最少だが、任意コード実行リスクと
  バージョン非互換（クラス定義変更で読めなくなる）の両方で失格。
- **独自バイナリ形式**: 構想初期に浮上したが撤回。可読性・diff 可能性・schema
  検証・外部ツールからの利用すべてで JSON に劣る。
- **SQLite**: 部分更新には強いが、プロトタイプ規模では diff 可能なテキストの利点が
  勝る。project が巨大化した将来に再検討。
