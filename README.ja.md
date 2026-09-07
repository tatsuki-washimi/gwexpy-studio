# GWexpy Studio

[English](README.md)

GWexpy Studioは、科学データを確認し、GWexpyで解析し、projectを保存し、通常のPythonコードを書き出すためのdesktop GUIです。

PythonやJupyterに慣れていない研究者と学生が、Gitを使わずに解析へ入れることを対象にします。

## 試用版の状態

最初のinstallable trial wheelを準備中です。

現在はpublic trial wheelもPyPI releaseもないため、このrepositoryは利用者向けの導入経路ではありません。

最初の対象は、conda Python 3.12を使うUbuntu 24.04です。

試用者向けの導入経路は、次の形にします。

```bash
conda create -n gwexpy-studio python=3.12
conda activate gwexpy-studio
pip install --only-binary=:all: \
  -c constraints-ubuntu24-x86_64.txt \
  ./gwexpy_studio-0.1.0a1+trial.<build-id>-py3-none-any.whl
gwexpy-studio
```

wheel、architecture別constraints、checksum、build identity、Quick Startは、一つのGitHub prereleaseに添付します。

ARM64では対応する`constraints-ubuntu24-aarch64.txt`をQuick Startに従って使います。

この経路にGit、source checkout、editable installは含めません。

## 最初の5分

試用では、次の短い流れを確認します。

1. Studioを起動する。
2. **Try Sample**を選ぶ。
3. TimeSeriesをCropする。
4. **ASD**を実行する。
5. projectを保存し、Studioを閉じ、開き直してrecoveryを確認する。

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

[ROADMAP.md](ROADMAP.md)に、public source、wheel、Ubuntu、WSL2のmilestoneを記載しています。

source codeは[MIT license](LICENSE)です。

意図的に開発環境を作るcontributorは、[docs/development.md](docs/development.md)を参照してください。

public trial contractは[docs/release/0.1.0a1-trial-readiness.md](docs/release/0.1.0a1-trial-readiness.md)です。
