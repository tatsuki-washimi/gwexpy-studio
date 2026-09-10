# GWexpy Studio macOS試用版クイックスタート

この手順は、Apple Silicon MacとmacOS 15以上で使う`macos15-arm64`
prerelease専用です。Intel Mac、WSL2、Linuxでは使用しません。

参加条件は、今回がGWexpy Studioを初めて手動操作することです。
開始前にcondaを導入してください。Git、source checkout、editable installは不要です。

環境作成・導入時間と、Welcome表示後のWorkflow時間を分けて記録します。

## ダウンロードと検証

案内された一つのprereleaseリンクから、ZIPと対応する`.zip.sha256`だけを
空のディレクトリへダウンロードし、次を実行します。

```bash
set -euo pipefail
sidecars=(gwexpy-studio-trial-macos15-arm64-*.zip.sha256)
test "${#sidecars[@]}" -eq 1
shasum -a 256 -c "${sidecars[0]}"
archive="${sidecars[0]%.sha256}"
unzip "$archive"
bundle="${archive%.zip}"
cd "$bundle"
shasum -a 256 -c SHA256SUMS
```

すべて`OK`になることを確認します。失敗した場合はinstallせずに終了します。

## Macの確認

```bash
set -euo pipefail
test "$(uname -m)" = "arm64"
test "$(sw_vers -productVersion | cut -d. -f1)" -ge 15
sw_vers
```

commandが失敗した場合は、出力を記録して終了します。

## 分離した環境の作成とinstall

```bash
conda create -n gwexpy-studio-macos python=3.12
conda activate gwexpy-studio-macos
export PYTHONNOUSERSITE=1
unset PYTHONPATH
python --version
conda --version
pip --version
pip install --only-binary=:all: \
  -c constraints-macos15-arm64.txt \
  ./gwexpy_studio-*.whl
```

試験中は環境をactivateしたままにします。別の端末を開いた場合は、activateと
二つの環境設定を再実行します。installまたは起動に失敗した場合は、constraintsの
変更、source build、回避策を試しません。

## Workflowの実行

```bash
gwexpy-studio
```

Welcome表示後に、**Try Sample → Load → Crop → ASD → Save → Close → Open**を
実行します。保存と再オープンではnative file pickerを使用します。
ReviewまたはRestoreが表示された場合は、そこから継続できたかを記録します。
About画面のBuild IDも記録します。

起動、project reopen、recoveryで進行不能になった場合は、回避策を試さず、
操作段階、画面、端末出力を保存して終了します。

`Feedback.ja.md`へ記入し、案内されたメールまたはチャットへ返信してください。
機密の測定データ、認証情報、username、hostname、端末固有path、無関係なlogは
送らないでください。
