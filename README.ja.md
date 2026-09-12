# GWexpy Studio

[English](README.md)

GWexpy Studioは、科学データを確認し、GWexpyで解析し、projectを保存し、通常のPythonコードを書き出すためのdesktop GUIです。

PythonやJupyterに慣れていない研究者と学生が、Gitを使わずに解析へ入れることを対象にします。

## 試用版の配布

試用版は、targetごとのZIPと対応する外側checksum sidecarで配布します。

候補文書は`ubuntu24-x86_64`、`debian13-x86_64`、`wsl2-ubuntu24`、
`macos15-arm64`の4targetを扱います。

guideが存在することは、そのtargetが公開済みまたはqualification済みであることを意味しません。
送付者が明示した検証済みReleaseだけを使用してください。

試用者に必要なのは、Python 3.12を使うcondaです。
Git、source checkout、editable install、GitHub accountは不要です。

送付されたtarget名に対応するQuick Startを使ってください。
Quick Startは、配布物に含まれるZIP、wheel、constraintsを一意に確認し、専用conda環境を作り、OSのPythonを変更せずに導入します。

target別の手順は[docs/trial](docs/trial)にあります。
公開後は外部の配布一覧で、現在のReleaseと実際に試験したOS版を確認してください。

ZIPにはwheel、constraints、checksum、build identity、target別Quick Start、
日本語のfeedback formを収録します。

別のOS、CPU architecture、native／WSLの別、desktop環境では使用しません。

旧Ubuntu reference artifactは、今回の4target trialとは別に扱います。
schema 3のtagとassetは互換性確認用に保持するもので、新しいqualification済みtargetを意味しません。

## 基本Workflow

試用では、次の短い流れを確認します。

1. Studioを起動する。
2. **Try Sample**を選ぶ。
3. TimeSeriesをCropする。
4. **ASD**を実行する。
5. projectを保存し、Studioを閉じ、開き直す。

projectは`.gwxproj`として保存します。

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

[ROADMAP.md](ROADMAP.md)に、public source、Ubuntu reference、WSL2、macOS、
platform横断human trialのmilestoneを記載しています。

source codeは[MIT license](LICENSE)です。

意図的に開発環境を作るcontributorは、[docs/development.md](docs/development.md)を参照してください。

public trial contractは[docs/release/0.1.0a1-trial-readiness.md](docs/release/0.1.0a1-trial-readiness.md)です。

feedbackの送付方法は各bundleに収録します。
