# ADR-0010: plugin は Python entry points + capability 分割 + lazy activation

- Status: Accepted（実装は alpha 以降に延期）
- Date: 2026-08-16
- 出典: 構想メモ ADR-007 節、napari npe2 / VS Code extension host の調査結果

## Context

gwexpy 自体が多数の I/O・外部ライブラリ bridge を扱うため、Studio を monolithic に
すると際限なく大きくなる。napari の第2世代 plugin 機構（npe2）は contribution を
static manifest で宣言し、Python import を実際に必要になるまで遅延する。VS Code は
activation event により必要な extension だけを遅延ロードし、startup への影響を抑える。
Python packaging には installed distribution がコンポーネントを公開する標準機構
（entry points）がある。

## Decision

1. **発見機構は Python entry points** を使う（独自 plugin レジストリを作らない）。

   ```toml
   [project.entry-points."gwexpy_studio.viewers"]
   foo = "foo_studio:FooViewer"

   [project.entry-points."gwexpy_studio.exporters"]
   foo = "foo_studio:FooExporter"
   ```

2. **capability を分割する**。万能な `gwexpy_studio.plugins` 一種類にせず、
   ViewerPlugin / InspectorPlugin / ExporterPlugin / SourceBrowserPlugin /
   UIContributionPlugin 程度に分ける。
3. **scientific plugin と UI plugin の境界を守る**。新しい解析アルゴリズムや新しい
   file reader を Studio plugin として実装してはならない — それらは gwexpy 側の
   拡張である（ADR-0001 の原則の plugin 版）。Studio plugin は presentation /
   interaction / workflow integration に限る。
4. **lazy activation**。起動時に plugin を import せず、「file type X を開いた →
   X Viewer plugin を activate」のような契機で初めて import する。metadata
   （表示名、対応形式）は import なしで読めるようにする。
5. **alpha では first-party plugin のみ**で機構を実証し、third-party API は
   stable 化しない。
6. 将来の manifest には権限宣言（filesystem / network / subprocess）の場所を
   確保する。同一プロセス import である限り真の sandbox にはならないが、
   ユーザーへの trust 情報と将来の subprocess plugin host への移行路になる
   （ADR-0012 の trust model と接続）。

## Consequences

- plugin の追加は「パッケージを pip install する」ことに一本化され、Studio 側に
  独自インストーラを作らない。
- 起動時間が plugin 数に比例しない（lazy activation の効果）。plugin の failure も
  activate 時点に局所化される。
- headless プロトタイプでは実装しない。ただし OperationSpec レジストリ（ADR-0008 の
  stable operation_id と、実装計画 plans/headless-prototype-plan.md の curated 対応表）
  は将来の OperationPlugin の形を先取りしており、entry points 化は後から接げる。

## 検討した代替案

- **独自 plugin ディレクトリ + 動的スキャン**: 発見・版管理・依存解決を車輪の
  再発明することになる。entry points は pip/uv がそのまま面倒を見る。
- **起動時全 import**: plugin が増えるほど起動が遅くなり、壊れた plugin 一つで
  起動不能になる。napari が npe2 で明示的に脱却した方式。
- **Update Site 型の独自配布**（ImageJ/Fiji 方式）: 任意ユーザーがコード配布できる
  仕組みは、untrusted input を明示要件とする Studio（ADR-0012）では管理コストが
  高すぎる。PyPI エコシステムに乗る。
