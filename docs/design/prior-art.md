# 先行事例調査 — GWexpy Studio が学ぶべき既存アプリ

この文書は、各製品の公開一次資料を踏まえて、Studioの設計判断に関係する点だけを
public source向けに整理した比較メモである。

個別製品の最新の仕様は、設計時に各製品の公式ドキュメントで確認する。

## 位置づけの結論

GWexpy Studio に相当する既存アプリ（heterogeneous I/O → GWpy 互換 object →
多チャンネル/matrix 解析 → GUI 操作 → provenance → 通常の Python/Marimo へ export を
一本化したもの）は確認できなかった。ただし必要な設計原則は既存製品に分散して存在する:

> **Mantid の semantic history、MATLAB Signal Analyzer の interactive processing UX、
> ImageJ2 の typed command schema、Glue の core/state/event 分離、napari の
> model/Qt/plugin architecture を組み合わせ、security だけは5製品より意図的に厳しくする**

## 5製品の横断比較

「強・中・弱」は製品全体の評価ではなく、**Studio が必要とする能力への近さ**。

| 製品 | 主データモデル | undo / provenance | GUI→code | core/GUI 境界・plugin | serialization | untrusted input | Studio への主要教訓 |
|---|---|---|---|---|---|---|---|
| **Mantid Workbench** | Workspace / Algorithm | provenance **強**（Algorithm 名・property・version・環境）。一般 undo は弱 | **強**。History→Python / notebook | Framework ← mantidqt ← Workbench | project state + NeXus | script は通常 Python、sandbox 記載なし | **semantic operation history を採用** |
| **MATLAB Signal Analyzer** | vector/timetable 等 | **中**。preprocess history + 一段ずつ undo | **強いが分散**。機能別 generator | 内部非公開。custom preprocessing 拡張のみ | MAT/MLDATX。display 設定は保存されない | MAT `load` にコード実行の公式警告 | **Preview→Apply→Accept と code-gen 検証を採用** |
| **ImageJ / Fiji** | ImagePlus + ImageJ2 Dataset | **弱〜中**。Recorder は強いが undo は限定的 | **中〜強**。Macro Recorder | SciJava Context/Service/Command | 統一 project なし（script 中心） | script は classpath へ広くアクセス | **typed command schema + recorder 思想を採用、string option は避ける** |
| **Glue** | Data/Component/Subset/Link | undo **中**（command stack）。科学 provenance は弱 | **弱〜中**。state API は強いが recorder なし | **非常に強い**。glue-core / glue-qt、Hub、registry | `.glu` session | CWD `config.py` 自動実行（反面教師） | **model/state/event 分離を採用** |
| **napari** | ViewerModel→LayerList→Layer | **局所 undo**（Labels のみ）。global provenance 弱 | **中以下**。Python API parity のみ | Python model / Qt / VisPy、npe2 static manifest | model JSON + layer writer | plugin 同一環境、remote script は trust 前提 | **model/UI/render 分離と manifest plugin を採用** |

## 採るべきパターン

1. **semantic operation history**（Mantid）
   履歴の単位を「ボタンを押した」ではなく「この algorithm をこの property で実行した」
   にする。Mantid はそこから再実行可能な Python / IPython notebook を生成できる。
   → ADR-0006 / ADR-0008。
2. **Preview → Apply → Accept**（MATLAB Signal Analyzer）
   parameter 調整中の試行を正式な history node にせず、preview（一時結果）→ apply →
   commit の段階を分ける。slider を 1px 動かすたびに provenance を汚さない。
   → Studio 0.2 の processing UX の基本形。
3. **typed parameter schema からの UI 自動生成**（ImageJ2 / SciJava）
   操作の input/output/parameter を machine-readable に宣言すれば、GUI が widget を
   自動構築でき、同じ操作を headless でも実行できる。操作ごとの手書き dialog 量産を
   避ける。→ OperationSpec / ParamSpec（実装計画）と将来の gwexpy 上流化。
4. **state-driven UI + publish/subscribe**（Glue）
   viewer/layer state を Python object として持ち、Qt widget はそれを表示・変更する。
   コンポーネント間は Hub 経由で疎結合にする。「Qt を install していない環境で core
   テストが全部通る」を architecture invariant にする。→ ADR-0005。
5. **model / Qt / rendering 分離 + static plugin manifest + lazy import**（napari）
   napari の npe2 は contribution を manifest で宣言し、import を必要時まで遅延する。
   → ADR-0005 / ADR-0010。
6. **screen gesture → domain object 変換**（Glue の ROI → SubsetState）
   plot 上の drag をまず `TimeRangeSelection(t0, t1)` へ正規化し、それから
   `CropOperation(selection)` に変換する。同じ selection を crop / statistics /
   fit region で再利用できる。
7. **data object と display object の分離**（ImageJ2、napari）
   `TimeSeries object ≠ plot layer ≠ plot window`。plot を閉じても scientific object は
   消えない。→ ADR-0009。

## 避けるべきパターン

1. **stringly-typed parameter**（ImageJ1 Recorder の `"low=20 high=500"`）—
   型・単位・version が失われ、schema evolution と検証が困難になる。
2. **機能別に分散した code generator**（Signal Analyzer）— 「GUI では再現できたが
   export した script 単体では再現しない」failure class を生む。exporter は
   OperationGraph の唯一の consumer にする（ADR-0008）。
3. **layer/型ごとの局所 undo**（napari Labels）— application-level の command stack を
   最初から設計しないと後付けできない（ADR-0006）。
4. **CWD からの config 自動実行**（Glue）— untrusted directory で起動しただけで
   任意コードが走る（ADR-0012）。
5. **session file だけに再現性を依存**（Glue `.glu` 中心の運用）— 科学的解析を
   独自形式に閉じ込めない。Python/Marimo + sidecar の三層（ADR-0007）。
6. **「Python から操作できる」ことと「GUI 操作から Python を生成できる」ことの混同**
   （Glue / napari は前者のみ）— Studio の要件は明確に後者（ADR-0008）。
7. **display 設定が保存されない session**（Signal Analyzer）— 「開き直したら何が
   復元されるか」を仕様として明示する（ADR-0007）。

## UI/UX 参照群（第2回調査: IDE・DAQ・CAD・Office・画像/動画編集・DAW）

| 分野 | 製品 | Studio へ移植するパターン |
|---|---|---|
| IDE | VS Code | Activity Bar → Side Bar → Editor → Panel の安定 shell、Command Palette。左 Data Explorer / 中央 Plot / 右 Inspector / 下 History・Console の基本形 |
| Notebook | JupyterLab | tabbed main area + workspace 永続化。ただし file-centric すぎる点は避ける |
| DAQ | NI FlexLogger / DewesoftX | `DataSource → Channel → View → Analysis` の導線。「まず測定結果を見る」までが短い実験屋向け UX |
| CAD | Autodesk Fusion / SOLIDWORKS | **Browser + Canvas + Timeline** — object tree・画面上の対象・生成履歴の三者連動。feature tree 上の error/rollback 表示 |
| Office | Excel | **結果と式の二層表示** — セルに結果、Formula Bar に式。GUI 表示 `ASD → fftlength 4s → median` の直下に `asd = ts.asd(fftlength=4, method="median")` を常時表示し、Copy Python / Export に接続 |
| 画像 | Photoshop | selection-sensitive な Properties と Contextual Task Bar — 選択中の型に応じ「今できる操作」だけを出す（TimeSeries 選択中は Crop/Filter/FFT/ASD…） |
| 動画 | Premiere / DaVinci Resolve | 作業別 workspace preset。Resolve Fusion の node graph は「詳細ビュー」として参考（既定 UI は node editor にしない） |
| DAW | Ableton Live | channel strip + processing chain 表示 — TimeSeriesDict/Matrix を「Channel / Unit / Fs / Visible / Processing」の表として扱う |

一言まとめ:

> **VS Code の shell + Fusion の history + Photoshop の context tools + Excel の
> code transparency + Dewesoft/FlexLogger の measurement workflow + Resolve の
> node graph**

避けること: Office Ribbon の全面採用（toolbar 膨張）、dockable panel の無制限化
（初学者に複雑）、Resolve 型の過剰なページ分割（同じ object の解析で画面往復）、
CAD 型の完全線形 history（解析 DAG に合わない）。

## security 横断所見

5製品はいずれも trusted scientific desktop 前提（詳細は ADR-0012 の Context）。
Studio は drag & drop 入力を untrusted と定義するため、この点だけは先行製品に
追随せず、意図的に厳しくする。
