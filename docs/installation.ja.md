# GWexpy Studioの導入

このページだけで、導入から最初の操作まで進められます。
通常の手順では、OS別Quick Startへ移動する必要はありません。

## 1. 導入経路を選ぶ

| 目的 | 選ぶ経路 | 用意するもの |
| --- | --- | --- |
| 検証済み試用版を使う | GitHub ReleaseのZIP | 対応するZIP、checksum、conda |
| sourceを実行または変更する | python -m pip install . | source checkout、Python 3.12 |

試用版とsource checkoutは別の経路です。
試用版はビルド済みwheel、固定したconstraints、platform qualificationを使います。
sourceの経路は、開発またはローカル評価のためにpackageの依存関係を解決します。

## 2. 試用版を導入する

### Releaseから2ファイルをダウンロードする

[GWexpy StudioのGitHub Releases](https://github.com/tatsuki-washimi/gwexpy-studio/releases)から、同じBuild IDを持つ次の2ファイルを空のディレクトリへダウンロードします。

~~~text
gwexpy-studio-trial-<target>-<build-id>.zip
gwexpy-studio-trial-<target>-<build-id>.zip.sha256
~~~

外側のchecksumを確認してから展開し、展開先で内部checksumを確認します。

UbuntuまたはWSL2では次を実行します。

~~~bash
set -euo pipefail
shopt -s nullglob
sidecars=(gwexpy-studio-trial-*.zip.sha256)
test "${#sidecars[@]}" -eq 1
sha256sum -c "${sidecars[0]}"
archive="${sidecars[0]%.sha256}"
unzip "$archive"
bundle="${archive%.zip}"
cd "$bundle"
sha256sum -c SHA256SUMS
~~~

Debianでは、下のDebian用セクションにあるchecksum確認手順を使います。
macOSでは sha256sum を shasum -a 256 に置き換えます。
どちらかのchecksumが失敗した場合は、installせずに終了します。

### 端末を確認してinstallする

次のセクションから、実際の端末に合うものを一つだけ開きます。
preflightが失敗した場合は、source installや別のconstraintsを回避策にしません。

<details open>
<summary>Windows 11のWSL2 / Ubuntu 24.04 / WSLg</summary>

この経路は、wsl2-ubuntu24 assetが同じReleaseに含まれる場合だけ使います。
x86_64とaarch64のどちらを使うかは、Windows側とWSL2 guest側の対応を確認して決めます。

~~~bash
set -euo pipefail
grep -qi microsoft /proc/sys/kernel/osrelease
test -d /mnt/wslg
grep '^ID=ubuntu$' /etc/os-release
grep '^VERSION_ID="24.04"$' /etc/os-release
architecture="$(uname -m)"
case "$architecture" in x86_64|aarch64) ;; *) exit 1 ;; esac
powershell.exe -NoLogo -NoProfile -NonInteractive -Command \
  '$env:PROCESSOR_ARCHITECTURE; [Environment]::OSVersion.Version'
~~~

/mnt/wslg、powershell.exe、Ubuntu 24.04、対応architectureのどれかが確認できない場合は、ここで終了します。

~~~bash
conda create -n gwexpy-studio-wsl2 python=3.12
conda activate gwexpy-studio-wsl2
export PYTHONNOUSERSITE=1
unset PYTHONPATH

python - <<'PY'
import ctypes

for library in ("libEGL.so.1", "libGL.so.1"):
    ctypes.CDLL(library)
print("Qt GL runtime: OK")
PY

architecture="$(uname -m)"
pip install --only-binary=:all: \
  -c "constraints-ubuntu24-${architecture}.txt" \
  ./gwexpy_studio-*.whl
gwexpy-studio
~~~

Qt GL runtimeの確認が失敗した場合は、Ubuntuの管理者権限で次を実行してから再確認します。

~~~bash
sudo apt-get update
sudo apt-get install --no-install-recommends -y libegl1 libgl1
~~~

</details>

<details>
<summary>native Ubuntu 24.04</summary>

この経路は、対応するUbuntu assetが同じReleaseに含まれる場合だけ使います。

~~~bash
set -euo pipefail
grep '^ID=ubuntu$' /etc/os-release
grep '^VERSION_ID="24.04"$' /etc/os-release
architecture="$(uname -m)"
case "$architecture" in x86_64|aarch64) ;; *) exit 1 ;; esac
~~~

~~~bash
conda create -n gwexpy-studio python=3.12
conda activate gwexpy-studio
export PYTHONNOUSERSITE=1
unset PYTHONPATH

python - <<'PY'
import ctypes

for library in ("libEGL.so.1", "libGL.so.1"):
    ctypes.CDLL(library)
print("Qt GL runtime: OK")
PY

architecture="$(uname -m)"
pip install --only-binary=:all: \
  -c "constraints-ubuntu24-${architecture}.txt" \
  ./gwexpy_studio-*.whl
gwexpy-studio
~~~

libEGL.so.1またはlibGL.so.1が見つからない場合は、libegl1とlibgl1を導入してから再確認します。

</details>

<details>
<summary>native Debian 13 / x86_64</summary>

同じReleaseに`debian13-x86_64` assetが含まれる場合だけ、この経路を使います。
画面を使えるnative Debian 13 x86_64、Bash、conda、`unzip`、`sha256sum`を用意します。
condaとpipにはネット接続が必要です。
Qtの画面表示に必要なパッケージが不足している場合は、管理者権限で導入します。

Debian用ZIPと`.zip.sha256`を保存した空のフォルダーで、対応する一組だけを確認して展開します。

~~~bash
set -euo pipefail
shopt -s nullglob
archives=(gwexpy-studio-trial-debian13-x86_64-*.zip)
sidecars=(gwexpy-studio-trial-debian13-x86_64-*.zip.sha256)
test "${#archives[@]}" -eq 1
test "${#sidecars[@]}" -eq 1
test "${sidecars[0]%.sha256}" = "${archives[0]}"
sha256sum -c "${sidecars[0]}"
archive="${archives[0]}"
unzip "$archive"
bundle="${archive%.zip}"
cd "$bundle"
sha256sum -c SHA256SUMS
~~~

すべてのchecksumが`OK`になることを確認します。
失敗した場合はインストールせずに終了します。
デスクトップで開いた端末から、OS、CPU、画面の有無を確認します。

~~~bash
set -euo pipefail
if grep -qi microsoft /proc/sys/kernel/osrelease; then
  echo "WSL is not supported by this guide." >&2
  exit 1
fi
. /etc/os-release
test "${ID:-}" = debian
test "${VERSION_ID:-}" = 13
test "$(uname -m)" = x86_64
test -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}"
printf '%s\n' "$PRETTY_NAME" "$(uname -m)"
~~~

確認に失敗した場合は導入を中止します。
既存の環境を上書きせず、Python 3.12の専用conda環境を作ります。

~~~bash
set -euo pipefail
env_name=gwexpy-studio-debian13
if ! env_list="$(conda env list)"; then
  echo "conda env list failed; stop." >&2
  exit 1
fi
if printf '%s\n' "$env_list" | awk -v name="$env_name" \
  '$1 == name { found=1 } END { exit found ? 0 : 1 }'; then
  echo "conda environment already exists: $env_name" >&2
  exit 1
fi
conda create -n "$env_name" python=3.12 pip
conda activate "$env_name"
export PYTHONNOUSERSITE=1
unset PYTHONPATH
python - <<'PY'
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine() == "x86_64"
PY
python --version
conda --version
python -m pip --version
~~~

試用中はこの環境を有効にしたままにします。
別の端末を開く場合は、環境を再び有効にし、`PYTHONNOUSERSITE=1`を設定して`PYTHONPATH`を解除します。
Qtの画面表示用ライブラリを確認します。
不足があれば記載したDebianパッケージを導入し、同じ確認を再実行します。

~~~bash
set -euo pipefail
probe_runtime() {
  python - <<'PY'
import ctypes

libraries = (
    "libEGL.so.1", "libGL.so.1", "libfontconfig.so.1", "libglib-2.0.so.0",
    "libdbus-1.so.3", "libxkbcommon.so.0", "libzstd.so.1", "libX11-xcb.so.1",
    "libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0",
    "libxcb-randr.so.0", "libxcb-render-util.so.0",
    "libxcb-shape.so.0", "libxcb-shm.so.0", "libxcb-sync.so.1",
    "libxcb-xfixes.so.0", "libxcb-xkb.so.1", "libxkbcommon-x11.so.0",
    "libSM.so.6", "libICE.so.6", "libwayland-client.so.0",
    "libwayland-cursor.so.0", "libxcb-keysyms.so.1",
)
missing = []
for library in libraries:
    try:
        ctypes.CDLL(library)
    except OSError:
        missing.append(library)
if missing:
    print("Missing runtime libraries:", " ".join(missing))
    raise SystemExit(1)
print("Qt display runtime: OK")
PY
}

if probe_runtime; then
  :
else
  packages=(
    libegl1 libgl1 libfontconfig1 libglib2.0-0t64 libdbus-1-3
    libxkbcommon0 libzstd1 libx11-xcb1 libxcb-cursor0 libxcb-icccm4
    libxcb-keysyms1 libxcb-randr0 libxcb-shape0 libxcb-sync1
    libxcb-xfixes0 libxkbcommon-x11-0 libsm6 libwayland-cursor0
  )
  sudo apt-get update
  sudo apt-get install --no-install-recommends -y "${packages[@]}"
  probe_runtime
fi
~~~

Debian用constraintsと配布物内のwheel一つを使ってインストールし、Studioを起動します。

~~~bash
set -euo pipefail
shopt -s nullglob
wheels=(gwexpy_studio-*.whl)
test "${#wheels[@]}" -eq 1
test -f constraints-debian13-x86_64.txt
python -m pip install --only-binary=:all: \
  -c constraints-debian13-x86_64.txt \
  "./${wheels[0]}"
gwexpy-studio
~~~

別のライブラリの不足やインストール失敗が表示された場合は、エラーと失敗した段階を記録します。
詳しい診断は[Debian Quick Start](trial/debian/Quick-Start.ja.md)を参照してください。

</details>

<details>
<summary>macOS 15以上 / Apple Silicon</summary>

この経路は、macos15-arm64 assetが同じReleaseに含まれる場合だけ使います。
Intel Macでは使用しません。

~~~bash
set -euo pipefail
test "$(uname -m)" = "arm64"
test "$(sw_vers -productVersion | cut -d. -f1)" -ge 15
sw_vers
~~~

~~~bash
conda create -n gwexpy-studio-macos python=3.12
conda activate gwexpy-studio-macos
export PYTHONNOUSERSITE=1
unset PYTHONPATH
pip install --only-binary=:all: \
  -c constraints-macos15-arm64.txt \
  ./gwexpy_studio-*.whl
gwexpy-studio
~~~

</details>

## 3. source checkoutから導入する

試用版のqualificationを必要としないsource checkoutでは、次の経路を使います。
対応するPythonは3.12です。

~~~bash
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m pip check
.venv/bin/gwexpy-studio
~~~

python -m pip install . はPySide6を含むruntime dependencyを導入し、.venv内に gwexpy-studio launcherを作成します。
保存済みprojectを開く場合は、launcherに .gwxproj pathを一つ渡します。

testとlintのtoolが必要な開発者は、同じ環境で dev extraを導入します。

~~~bash
.venv/bin/python -m pip install '.[dev]'
~~~

sourceを編集中はeditable installも使えます。

~~~bash
.venv/bin/python -m pip install -e '.[dev]'
~~~

editable installは開発用であり、試用版の導入には使いません。

## 4. 起動後の4ステップ

以下の画面は、試用版とsource checkoutに共通する操作を示します。
OS別のページへ移動せず、このページの順に確認できます。

### Welcome画面でTry Sampleを押す

![起動後のWelcome画面](images/installation/01-welcome.png)

起動準備が完了すると、Welcome画面の **Try Sample** が有効になります。
自分のデータを使う場合は **Open Data** を選びます。

### サンプルをLoadする

![Try Sampleで読み込んだTimeSeries](images/installation/02-try-sample.png)

サンプルを確認し、**Load** を押すとTimeSeriesが中央に表示されます。
左のSourcesで選択中のobject、右のMetadataで基本情報を確認できます。

### CropしてASDを表示する

![CropとASDの結果](images/installation/03-asd.png)

TimeSeriesを選んだ状態で **Operations → Crop** を実行し、続けて **Operations → ASD** を実行します。
Historyには timeseries.crop と timeseries.asd が記録されます。

### projectを保存して再オープンする

![保存したprojectを再オープンした画面](images/installation/04-reopen-project.png)

**File → Save** で .gwxproj を保存し、Studioを閉じてから再起動します。
再オープン直後に **Review / Restore Data** が必要な場合は、表示された確認手順を選びます。
projectはsource reference、operation、active state、view state、Undo/Redoの位置を保存します。

## 5. 試用版で利用できるデータを読む

試用版では一覧のルートで元データを読み込みますが、元ファイルへ書き戻しません。

| data type | formatとreader |
| --- | --- |
| `TimeSeries` | CSV |
| `TimeSeries`、`TimeSeriesDict` | GWF：`gwf.lalframe`（推奨）、`gwf`も選択可能 |
| `TimeSeries`、`TimeSeriesDict`、`TimeSeriesMatrix` | DiagGUI XML：`xml.diaggui`、product `TS` |
| `TimeSeries`、`TimeSeriesDict` | NDScope HDF5：`hdf.ndscope` |

**Open Data**でdata typeとformatを選び、**Inspect / Review**で内容を確認してから、明示的に**Read Data**を押します。
ファイルをドロップすると汎用の**Open Data** formに入力されますが、data typeやformatの自動判別も読込開始も行いません。

GWexpy 0.2.0では、DiagGUI XMLから`FrequencySeries`、`FrequencySeriesDict`、`FrequencySeriesMatrix`への読込は現在利用できず、fail-closedで扱います。

## 6. 最低限の確認

起動した環境で、次を実行します。

~~~bash
python -m pip check
python -c "import gwexpy, PySide6; print('runtime imports: OK')"
test -x "$(command -v gwexpy-studio)" && echo "launcher: OK"
~~~

試用版では、これらに加えてRelease assetと同梱manifestのBuild IDを確認します。
試用版のwheel installを python -m pip install . に置き換えたり、constraintsを変更したり、dependencyをsourceからbuildしたりしません。

## 起動できない場合

最初に、pip check、architecture、Python version、Qt GL runtimeの出力を保存します。
WSL2では、/mnt/wslg と powershell.exe が使えることを再確認します。

詳細なqualification条件、診断記録、フィードバック形式が必要な場合だけ、次の補足資料を使います。

- [Ubuntu Quick Start](trial/ubuntu/Quick-Start.ja.md)
- [Debian Quick Start](trial/debian/Quick-Start.ja.md)
- [WSL2 Quick Start](trial/wsl2/Quick-Start.ja.md)
- [macOS Quick Start](trial/macos/Quick-Start.ja.md)

WSL2のpreflightに失敗した場合は、native Ubuntuから続行したり、source buildを回避策にしたりしません。
sourceの経路には、試用版のBuild ID、release checksum、qualification記録、固定済みtrial resolutionは付きません。
