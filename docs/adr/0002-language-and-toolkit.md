# ADR-0002: 言語は Python-first、GUI は PySide6 + Qt Widgets

- Status: Accepted
- Date: 2026-08-16
- 出典: 構想メモ「アプリのバックエンドはどうするのがよいか」「GUI framework は何を選ぶか」の節

## Context

バックエンド言語（Python 単独か、C++/Rust/Zig による高速化か）と GUI フレームワークの
選定が必要だった。Studio で重くなりそうな処理（FFT、filtering、fitting、大規模ファイル
デコード等）の多くは、すでに NumPy/SciPy の compiled 実装を経由する。呼び出し側を
native 化しても、肝心の数値計算部分が最適化済みなら改善は限定的である。

## Decision

**Python-first, native-when-measured** とする。

1. 言語は Python。全体を最初から C++/Rust で書かない。
2. native 高速化は「言語を先に選ぶ」のではなく、benchmark/profile で hotspot を特定
   してから対象を決める。優先順位は Rust (PyO3/maturin) > Cython/Numba ≈ C++
   (pybind11) > Zig。
3. **高速化は Studio の機能ではなく gwexpy の実装詳細とする**。native kernel は
   `gwexpy._native` のように gwexpy 側へ置き、Notebook から呼んでも Studio から
   呼んでも同じ加速が効くようにする。Studio は最後まで
   `Human interaction → gwexpy public API` だけを見る。
4. GUI は **PySide6 + Qt Widgets**。
   - PySide6 は Qt 公式 binding で LGPLv3/GPLv3/commercial。PyQt6（GPL/commercial）
     より、OSS 配布・施設利用・standalone binary への展開で扱いやすい
     （ただし Qt module ごとのライセンス条件は配布前に個別確認する）。
   - Qt Widgets は menu / dock / tree / table / property inspector / undo framework /
     Model/View を標準装備し、desktop workbench との適合性が最も高い。
     QML/Qt Quick は fluid UI 向けであり、最初は使わない。
5. plot は **Matplotlib を canonical renderer** とする。GUI で見た plot と Python
   script で再生成した plot の一致を成立させるため。リアルタイム cursor や数百万点の
   高速 pan/zoom 用の preview layer として PyQtGraph を将来検討するが、alpha では
   Matplotlib のみ（複雑さ回避）。

## Consequences

- Studio 側リポジトリに C 拡張のビルド基盤を持たない。ビルドが重いのは gwexpy 側の
  責務になる。
- GUI プロセスで解析を実行しない前提（ADR-0003）と組み合わせて、Python の GIL が
  UI 応答性のボトルネックになることを避ける。
- headless プロトタイプ段階では PySide6 を依存に入れない（依存グラフに Qt が無いこと
  自体が domain 層の Qt 非依存の検証になる）。

## 検討した代替案

- **C++ アプリに Python を embed**: `C++ app → embedded Python → gwexpy` の形は
  技術的には可能だが、gwexpy が主役なのに Python が「plugin runtime」側へ逆転する。
  Studio の思想（GUI 操作 == gwexpy Python API）と矛盾するため不採用。
- **Tauri（Rust + Web frontend）**: Python/Rust/TypeScript の3スタックを抱えることに
  なり、Python ライブラリの GUI 化という目的には過剰。
- **wxPython / Kivy / Toga / Flet / Dear PyGui / Tkinter**: それぞれ強みはあるが、
  docking・Model/View・undo framework・成熟度の総合力で Qt Widgets が優位。
  Dear PyGui は将来の live DAQ viewer 用途でのみ再検討の余地がある。
- **PyQt6**: 技術的にはほぼ同等だが GPL/commercial のみで、配布形態の自由度で
  PySide6 に劣る。
