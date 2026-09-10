# GWexpy Studio WSL2試用版クイックスタート

この手順は、Windows 11、Ubuntu 24.04のWSL2 guest、WSLgで使う
`wsl2-ubuntu24` prerelease専用です。x86_64とaarch64に対応します。
native UbuntuやmacOSでは使用しません。

参加条件は、今回がGWexpy Studioを初めて手動操作することです。
開始前にcondaを導入してください。Git、source checkout、editable installは不要です。

環境作成・導入時間と、Welcome表示後のWorkflow時間を分けて記録します。

## ダウンロードと検証

案内された一つのprereleaseリンクから、ZIPと対応する`.zip.sha256`だけを
空のディレクトリへダウンロードし、次を実行します。

```bash
set -euo pipefail
shopt -s nullglob
sidecars=(gwexpy-studio-trial-wsl2-ubuntu24-*.zip.sha256)
test "${#sidecars[@]}" -eq 1
sha256sum -c "${sidecars[0]}"
archive="${sidecars[0]%.sha256}"
unzip "$archive"
bundle="${archive%.zip}"
cd "$bundle"
sha256sum -c SHA256SUMS
```

すべて`OK`になることを確認します。失敗した場合はinstallせずに終了します。

## Windows、WSL2、Ubuntuの確認

```bash
set -euo pipefail
grep -qi microsoft /proc/sys/kernel/osrelease
test -d /mnt/wslg
grep '^ID=ubuntu$' /etc/os-release
grep '^VERSION_ID="24.04"$' /etc/os-release
architecture="$(uname -m)"
case "$architecture" in x86_64|aarch64) ;; *) exit 1 ;; esac
powershell.exe -NoLogo -NoProfile -NonInteractive -Command \
  '$env:PROCESSOR_ARCHITECTURE; [Environment]::OSVersion.Version'
```

commandが失敗した場合、またはWindowsとguestのarchitectureが対応しない場合は、
出力を記録して終了します。対応は`AMD64`と`x86_64`、`ARM64`と`aarch64`です。

## 分離した環境の作成

```bash
conda create -n gwexpy-studio-wsl2 python=3.12
conda activate gwexpy-studio-wsl2
export PYTHONNOUSERSITE=1
unset PYTHONPATH
python --version
conda --version
pip --version
```

試験中は環境をactivateしたままにします。別の端末を開いた場合は、activateと
二つの環境設定を再実行します。

## Qt GLの確認とinstall

```bash
python - <<'PY'
import ctypes

for library in ("libEGL.so.1", "libGL.so.1"):
    ctypes.CDLL(library)
print("Qt GL runtime: OK")
PY
```

失敗した場合は、Ubuntuのruntimeを導入してから再確認します。

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends -y libegl1 libgl1
```

検出したarchitecture用のconstraintsだけを使用します。

```bash
architecture="$(uname -m)"
pip install --only-binary=:all: \
  -c "constraints-ubuntu24-${architecture}.txt" \
  ./gwexpy_studio-*.whl
```

installまたは起動に失敗した場合は、constraintsの変更、source build、回避策を
試しません。

## Workflowの実行

```bash
gwexpy-studio
```

Welcome表示後に、**Try Sample → Load → Crop → ASD → Save → Close → Open**を
実行します。ReviewまたはRestoreが表示された場合は、そこから継続できたかを
記録します。About画面のBuild IDも記録します。

起動、project reopen、recoveryで進行不能になった場合は、回避策を試さず、
操作段階、画面、端末出力を保存して終了します。

`Feedback.ja.md`へ記入し、案内されたメールまたはチャットへ返信してください。
機密の測定データ、認証情報、username、hostname、端末固有path、無関係なlogは
送らないでください。
