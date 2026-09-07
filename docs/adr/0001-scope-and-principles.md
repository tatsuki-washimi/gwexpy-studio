# ADR-0001: スコープと基本原則 — Studio は gwexpy public API のみを使う独立アプリ

- Status: Accepted
- Date: 2026-08-16
- 根拠: gwexpy public APIだけを使う独立アプリケーションという設計判断

## Context

GWexpy のロードマップ検討の初期段階で、GUI アプリケーションは gwexpy core の非目標と
整理された（PR #488 による GUI 分離）。その後「GUI を捨てる」のではなく、gwexpy を
解析エンジンとして使う上位アプリケーションとして別プロジェクト化する案に発展した。
gwexpy 本体の ROADMAP.md にも Studio セクションがあり、「別リポジトリで開発し、
gwexpy 側が約束するのは headless contract のみ」と明記されている。

先行して別プロジェクト化した pyaggui（DiagGui 相当の診断・運用 GUI）との役割分担も
整理済みである。

## Decision

GWexpy Studio を次のように定義する:

> gwexpy の public API だけを使って、実験データを drag & drop で探索・可視化・
> 対話解析でき、その操作履歴を再現可能な通常の Python/Marimo プログラムとして
> 持ち出せる、独立した first-party desktop scientific workbench

基本原則:

1. **GUI 独自の科学計算アルゴリズムを作らない**。すべての科学処理は
   `GUI操作 → gwexpy public API → gwexpy result object → history/provenance/Python code`
   の経路を通る。GUI でしかできない解析、GUI 内部に隠れた数値処理は禁止。
2. **依存は一方向のみ**: gwexpy-studio → gwexpy。gwexpy が gwexpy-studio に依存する
   ことは決してない。`gwexpy[gui]` のような形で Qt 依存を gwexpy 本体へ持ち込まない。
3. **「Python を隠す GUI」ではない**。GUI を入口にしつつ、最終的に gwexpy/Python の
   通常の解析へ移行できる教育・Quick Look・解析再現ツールとする。
4. **Studio は gwexpy public API の統合試験を兼ねる**。GUI から使うことで
   「ファイル内 object を軽量列挙できない」「parameter の unit/range を機械的に
   取れない」等、Notebook だけでは見えない API 設計上の問題を発見し、
   gwexpy 1.0 API へフィードバックする。だから Studio alpha を gwexpy v0.x 期に動かす。

### pyaggui との役割分担

|  | pyaggui | GWexpy Studio |
|---|---|---|
| 主目的 | DiagGui 相当の診断・運用 | データ確認・解析・GWexpy 学習 |
| 中心対象 | 測定・診断 workflow | ファイル・データ object・解析 pipeline |
| 主ユーザー | 装置・制御系ユーザー | Python 初心者・実験データ解析者 |
| 最終成果 | GUI 上の診断結果 | 再実行可能な Python/Marimo |
| gwexpy との関係 | 別用途のアプリ | gwexpy public API の frontend |

### 責務分離

- gwexpy core 側: file I/O、format discovery、lightweight inspection、container
  operations、plotting、fitting、provenance 基盤、operation parameter の型・単位情報、
  serialization 可能な public API
- Studio 側: window/menu/dialog、drag & drop、tree view、parameter inspector、
  plot 上の direct manipulation、undo/redo、project state、Python/Marimo code
  generation、Studio 固有 plugin system

## Consequences

- Studio に科学処理を1行でも書きたくなったら、それは gwexpy 側の public API 不足の
  シグナルであり、gwexpy への機能要望（headless contract）として上流化する。
- gwexpy に未実装の inspection API（inspect_source 等）は、当面 Studio 側に curated な
  代替実装を置き、パターンが安定したら gwexpy へ昇格させる（ROADMAP の合意と整合）。
- DAQ 制御 GUI、DiagGui 代替、GUI 専用解析エンジン、独自 project format に科学解析を
  閉じ込めるアプリ、のいずれにもしない。要望が出た場合は pyaggui または gwexpy 本体の
  守備範囲として切り分ける。

## 検討した代替案

- **gwexpy 本体への GUI 同梱（`gwexpy[gui]`）**: Qt 依存が core に入り、テスト・配布・
  保守すべてが重くなる。ロードマップ検討で既に否決済み。
- **pyaggui への機能追加**: 対象ユーザーと成果物（診断結果 vs 再実行可能コード）が
  異なり、役割分担表のとおり別物。
