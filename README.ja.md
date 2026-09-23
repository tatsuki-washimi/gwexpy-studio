# GWexpy Studio

[English](README.md)

GWexpy Studioは、科学データを確認し、GWexpyで解析し、projectを保存し、通常のPythonコードを書き出すためのdesktop GUIです。

PythonやJupyterに慣れていない研究者と学生が、Gitを使わずに解析へ入れることを対象にします。

![起動後のGWexpy Studio](docs/images/installation/01-welcome.png)

## まず導入ガイドを開く

[導入ガイド](docs/installation.ja.md)だけで、試用版またはsource checkoutの導入から最初の操作まで進められます。
OS別Quick Startは、配布ZIPのqualificationや診断を詳しく記録する場合だけ参照します。

- 検証済み試用版を使う場合は、対応するZIPとchecksumをダウンロードし、導入ガイド内の端末セクションを一つ選びます。
- source checkoutを実行または変更する場合は、Python 3.12の仮想環境を作り、python -m pip install . を実行します。

試用版はビルド済みwheelとqualification済みconstraintsを使います。
sourceの経路は、開発またはローカル評価のためにpackageの依存関係を解決します。

## 試用版の配布

試用者には、公開担当者が明示した一つのGitHub prereleaseリンクを共有します。

同じBuild IDを持つZIPとchecksum sidecarをダウンロードし、外側と内部のchecksumを確認してから、導入ガイドの端末セクションへ進みます。

現在公開済みのprereleaseは、conda Python 3.12を使うnative Ubuntu 24.04 x86_64専用です。
WSL2とmacOSは、対応するassetが同じReleaseに含まれ、qualification済みである場合だけ利用します。

試用版の経路にGit、source checkout、editable install、PyPIは含めません。
試用版のwheel installを python -m pip install . に置き換えたり、constraintsを変更したり、dependencyをsourceからbuildしたりしません。

## 最初の5分

導入後は、次の順に操作します。

1. Studioを起動する。
2. **Try Sample**を選ぶ。
3. TimeSeriesをLoadしてCropする。
4. **ASD**を実行する。
5. projectを保存し、Studioを閉じ、開き直す。

導入ガイドには、この流れを確認する4枚の実画面があります。

<details>
<summary>4つの操作画面を見る</summary>

![Try Sampleで読み込んだTimeSeries](docs/images/installation/02-try-sample.png)

![CropとASDの結果](docs/images/installation/03-asd.png)

![保存したprojectを再オープンした画面](docs/images/installation/04-reopen-project.png)

</details>

projectは .gwxproj として保存します。

projectにはsource reference、operation、active state、view state、scientific Undo/Redoの位置を保存します。
元データと計算済みarrayはprojectの外に置きます。

## I/Oの利用可否

GWexpyに登録されたformatが、trial buildで自動的に使えるとは限りません。

UIはdata type、format、read/write directionごとに次のいずれかを表示します。

- **Verified**：review済みpolicyとworker process内のruntime probeがともに通過した状態です。
- **Experimental**：runtime probeは通過したが、利用上の注意がある状態です。
- **Unavailable**：policy、registry、runtime dependencyのいずれかが利用を許可しない状態です。

effective capability snapshotは、pathを含まないDiagnosticsに記録します。

## ロードマップとsource開発

[ROADMAP.md](ROADMAP.md)に、public source、Ubuntu reference、WSL2、macOS、platform横断human trialのmilestoneを記載しています。

source codeは[MIT license](LICENSE)です。

開発環境を作るcontributorは、[導入ガイド](docs/installation.ja.md)と[開発ガイド](docs/development.md)を参照してください。

public trial contractは[docs/release/0.1.0a1-trial-readiness.md](docs/release/0.1.0a1-trial-readiness.md)です。
