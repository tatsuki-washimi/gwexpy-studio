# GWexpy Studio

[English](README.md)

GWexpy Studioは、科学データを確認し、GWexpyで解析し、projectを保存し、通常のPythonコードを書き出すためのdesktop GUIです。

PythonやJupyterに慣れていない研究者と学生が、Gitを使わずに解析へ入れることを対象にします。

## インストール

**OSを選び、試用ZIPをダウンロードし、専用のconda環境へインストールして起動します。**
Pythonコードを書く必要はありません。Git、ソースコードの取得、GitHubアカウントも不要です。

### 公開状況とダウンロード

**2026年9月17日時点：新しい4環境向け試用版は、まだReleaseとして公開されていません。**
下表の手順は準備済みですが、現在は新しい試用ZIPのダウンロードリンクはありません。
旧Ubuntu reference版は別の配布物です。下の新しい試用版の手順とは組み合わせないでください。

| 使用する環境 | 新しい試用版の入手先 | このページ内のインストール手順 |
|---|---|---|
| Ubuntu 24.04、x86_64、通常のデスクトップ（WSLではない） | 未公開 | [Ubuntuの手順](#install-ubuntu) |
| Debian 13、x86_64、通常のデスクトップ（WSLではない） | 未公開 | [Debianの手順](#install-debian) |
| Windows 11 x86_64 ＋ WSL2 Ubuntu 24.04 ＋ WSLg | 未公開 | [Windows／WSL2の手順](#install-wsl2) |
| Windows 11 ARM64 ＋ WSL2 Ubuntu 24.04 aarch64 ＋ WSLg | 未公開 | [Windows／WSL2の手順](#install-wsl2) |
| Apple Silicon Mac、macOS 15以上 | 未公開 | [Macの手順](#install-macos) |

<!--
公開担当者へ：公開・再取得検証を終えたtargetだけ、上表の「未公開」を
そのtargetのReleaseページ、実在するZIPと.zip.sha256のリンクへ置き換える。
WSL2の2行は同じtargetのZIPを参照する。CPU別constraintsは手順内で選択する。
推測したasset名、/releases/latest、旧Ubuntu版への自動fallbackは使わない。
公開済みOS版・日付・対応する手順を更新し、README.mdと内容をそろえる。
-->

### 始める前に

condaが使える端末と、conda・pipでパッケージを取得できるネット接続が必要です。
Python 3.12は以下の手順で専用環境へ入れるため、OSに付属するPythonは変更しません。
WSL2では**Windows側ではなく、WSL2のUbuntu端末でcondaを使える状態**にしてください。

各OSの手順では、画面表示に必要な環境も確認します。
Linux／WSL2で不足するシステムライブラリの導入には管理者権限が必要になる場合があります。

### 導入の流れ

**① OSに対応するZIPと`.zip.sha256`を取得 → ② 内容を検証・展開 →
③ Python 3.12の専用conda環境へ導入 → ④ `gwexpy-studio`で起動**

公開後、上表から選んだ**同じReleaseのZIPと`.zip.sha256`の2ファイル**を、空のフォルダーに保存します。
インストールに使うのは試用ZIPです。ソースコードのZIPや`Code → Download ZIP`は使いません。

そのフォルダーを作業場所にした端末を開き、以下から自分のOSの手順だけを展開して、
コードブロックを上から順に実行してください。途中でエラーになった場合は、後続へ進みません。
確認処理も導入手順の一部です。最後の`pip install`だけを抜き出して実行しないでください。

<a name="install-ubuntu"></a>

### Ubuntu 24.04 x86_64

<details>
<summary>インストール手順を開く（このページ内）</summary>

UbuntuのデスクトップでBash端末を開きます。conda、`unzip`、`sha256sum`が必要です。WSL2はこの手順ではなく、Windows／WSL2の手順を使います。

**1. ZIPのチェックサム確認と展開**

```bash
set -euo pipefail
shopt -s nullglob
archives=(gwexpy-studio-trial-ubuntu24-x86_64-*.zip)
sidecars=(gwexpy-studio-trial-ubuntu24-x86_64-*.zip.sha256)
test "${#archives[@]}" -eq 1
test "${#sidecars[@]}" -eq 1
archive="${archives[0]}"
sidecar="${sidecars[0]}"
test "${archive}.sha256" = "$sidecar"
sha256sum -c "$sidecar"
unzip "$archive"
bundle="${archive%.zip}"
cd "$bundle"
sha256sum -c SHA256SUMS
```

チェックサムがすべて`OK`になったことを確認し、以後も同じ端末で続けます。現在の作業場所は展開したフォルダーです。

**2. OS・CPU・デスクトップの確認**

```bash
set -euo pipefail
if grep -qi microsoft /proc/sys/kernel/osrelease; then
  echo 'WSL is not supported by ubuntu24-x86_64' >&2
  exit 1
fi
. /etc/os-release
test "${ID:-}" = ubuntu
test "${VERSION_ID:-}" = 24.04
test "$(uname -m)" = x86_64
test -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}"
printf 'ID=%s VERSION_ID=%s arch=%s\n' "$ID" "$VERSION_ID" "$(uname -m)"
```

**3. Python 3.12の専用conda環境を作成**

同名の環境が既にある場合は上書きせず停止します。既に導入済みなら、下の「次回の起動」を使います。

```bash
set -euo pipefail
env_name=gwexpy-studio-ubuntu24
if ! env_list="$(conda env list)"; then
  echo 'conda env list failed; stop' >&2
  exit 1
fi
if printf '%s\n' "$env_list" | awk -v name="$env_name" '$1 == name { found=1 } END { exit found ? 0 : 1 }'; then
  echo "conda environment already exists: $env_name" >&2
  exit 1
fi
conda create -n "$env_name" python=3.12 pip
conda activate "$env_name"
export PYTHONNOUSERSITE=1
unset PYTHONPATH
python - <<'PYTHON'
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine() == "x86_64"
PYTHON
python --version
conda --version
python -m pip --version
```

**4. 画面表示用ライブラリの確認・不足時の導入**

不足がなければシステムパッケージは導入しません。不足がある場合は`sudo`で導入します。管理者権限がない場合は、管理者へ依頼してください。

```bash
probe_runtime() {
  python - <<'PYTHON'
import ctypes

libraries = (
    "libEGL.so.1", "libGL.so.1", "libfontconfig.so.1", "libglib-2.0.so.0",
    "libdbus-1.so.3", "libxkbcommon.so.0", "libzstd.so.1", "libX11-xcb.so.1",
    "libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0",
    "libxcb-keysyms.so.1", "libxcb-randr.so.0", "libxcb-render-util.so.0",
    "libxcb-shape.so.0", "libxcb-shm.so.0", "libxcb-sync.so.1",
    "libxcb-xfixes.so.0", "libxcb-xkb.so.1", "libxkbcommon-x11.so.0",
    "libSM.so.6", "libICE.so.6", "libwayland-client.so.0",
    "libwayland-cursor.so.0",
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
PYTHON
}

if probe_runtime; then
  :
else
  packages=(
    libegl1 libgl1 libfontconfig1 libglib2.0-0t64 libdbus-1-3 libxkbcommon0
    libzstd1 libx11-xcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0
    libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0
    libxcb-shm0 libxcb-sync1 libxcb-xfixes0 libxcb-xkb1 libxkbcommon-x11-0
    libsm6 libice6 libwayland-client0 libwayland-cursor0
  )
  unavailable=()
  sudo apt-get update
  for package in "${packages[@]}"; do
    if ! policy="$(LC_ALL=C apt-cache policy "$package")"; then
      echo "apt-cache policy failed for $package" >&2
      exit 1
    fi
    candidate="$(printf '%s\n' "$policy" | awk '/Candidate:/ { print $2; exit }')"
    if test -z "$candidate" || test "$candidate" = '(none)'; then
      unavailable+=("$package")
    fi
  done
  if ((${#unavailable[@]})); then
    printf 'No package candidate: %s\n' "${unavailable[*]}" >&2
    echo 'Ask an administrator to enable the official Ubuntu Universe repository; this guide does not change repositories.' >&2
    exit 1
  fi
  sudo apt-get install --no-install-recommends -y "${packages[@]}"
  probe_runtime
fi
```

`Qt display runtime: OK`が表示されたら次へ進みます。不足が残っている場合は停止します。

**5. 同梱wheelをインストール**

```bash
set -euo pipefail
shopt -s nullglob
constraint="$PWD/constraints-ubuntu24-x86_64.txt"
wheels=(./gwexpy_studio-*.whl)
test -f "$constraint"
test "${#wheels[@]}" -eq 1
wheel="${wheels[0]}"
python -m pip install --only-binary=:all: -c "$constraint" "$wheel"
```

**起動**

```bash
gwexpy-studio
```

Welcome画面が表示されたら、**Try Sample → Load → Crop → ASD**で動作を確認します。保存して開き直す手順は、この下の「基本Workflow」を参照してください。

**次回の起動**

```bash
conda activate gwexpy-studio-ubuntu24
export PYTHONNOUSERSITE=1
unset PYTHONPATH
gwexpy-studio
```

[既存の日本語Quick Start](docs/trial/ubuntu/Quick-Start.ja.md)

</details>

<a name="install-debian"></a>

### Debian 13 x86_64

<details>
<summary>インストール手順を開く（このページ内）</summary>

DebianのデスクトップでBash端末を開きます。conda、`unzip`、`sha256sum`が必要です。WSL2や他のLinux用の配布物を流用しません。

**1. ZIPのチェックサム確認と展開**

```bash
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
```

チェックサムがすべて`OK`になったことを確認し、以後も同じ端末で続けます。現在の作業場所は展開したフォルダーです。

**2. OS・CPU・デスクトップの確認**

```bash
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
```

**3. Python 3.12の専用conda環境を作成**

同名の環境が既にある場合は上書きせず停止します。既に導入済みなら、下の「次回の起動」を使います。

```bash
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
python - <<'PYTHON'
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine() == "x86_64"
PYTHON
python --version
conda --version
python -m pip --version
```

**4. 画面表示用ライブラリの確認・不足時の導入**

不足がなければシステムパッケージは導入しません。不足がある場合は`sudo`で導入します。管理者権限がない場合は、管理者へ依頼してください。

```bash
probe_runtime() {
  python - <<'PYTHON'
import ctypes

libraries = (
    "libEGL.so.1", "libGL.so.1", "libfontconfig.so.1", "libglib-2.0.so.0",
    "libdbus-1.so.3", "libxkbcommon.so.0", "libzstd.so.1", "libX11-xcb.so.1",
    "libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0",
    "libxcb-keysyms.so.1", "libxcb-randr.so.0", "libxcb-render-util.so.0",
    "libxcb-shape.so.0", "libxcb-shm.so.0", "libxcb-sync.so.1",
    "libxcb-xfixes.so.0", "libxcb-xkb.so.1", "libxkbcommon-x11.so.0",
    "libSM.so.6", "libICE.so.6", "libwayland-client.so.0",
    "libwayland-cursor.so.0",
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
PYTHON
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
```

`Qt display runtime: OK`が表示されたら次へ進みます。不足が残っている場合は停止します。

**5. 同梱wheelをインストール**

```bash
set -euo pipefail
shopt -s nullglob
wheels=(gwexpy_studio-*.whl)
test "${#wheels[@]}" -eq 1
test -f constraints-debian13-x86_64.txt
python -m pip install --only-binary=:all: \
  -c constraints-debian13-x86_64.txt \
  "./${wheels[0]}"
```

**起動**

```bash
gwexpy-studio
```

Welcome画面が表示されたら、**Try Sample → Load → Crop → ASD**で動作を確認します。保存して開き直す手順は、この下の「基本Workflow」を参照してください。

**次回の起動**

```bash
conda activate gwexpy-studio-debian13
export PYTHONNOUSERSITE=1
unset PYTHONPATH
gwexpy-studio
```

[既存の日本語Quick Start](docs/trial/debian/Quick-Start.ja.md)

</details>

<a name="install-wsl2"></a>

### Windows 11 / WSL2 Ubuntu 24.04

<details>
<summary>インストール手順を開く（このページ内）</summary>

**WindowsのPowerShellではなく、WSL2 UbuntuのBash端末**で実行します。conda、`unzip`、`sha256sum`、WSLgが必要です。Windows側とWSL2側でCPU系列をそろえます。x86_64とARM64は同じ手順で、環境に対応するconstraintsを選びます。

**1. ZIPのチェックサム確認と展開**

```bash
set -euo pipefail
shopt -s nullglob
archives=(gwexpy-studio-trial-wsl2-ubuntu24-*.zip)
sidecars=(gwexpy-studio-trial-wsl2-ubuntu24-*.zip.sha256)
test "${#archives[@]}" -eq 1
test "${#sidecars[@]}" -eq 1
archive="${archives[0]}"
test "${sidecars[0]%.sha256}" = "$archive"
sha256sum -c "${sidecars[0]}"
unzip "$archive"
bundle="${archive%.zip}"
cd "$bundle"
sha256sum -c SHA256SUMS
```

チェックサムがすべて`OK`になったことを確認し、以後も同じ端末で続けます。現在の作業場所は展開したフォルダーです。

**2. OS・CPU・デスクトップの確認**

```bash
set -euo pipefail
grep -qi microsoft /proc/sys/kernel/osrelease
test -n "${WSL_INTEROP:-}"
test -d /mnt/wslg
test -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}"
grep '^ID=ubuntu$' /etc/os-release
grep '^VERSION_ID="24.04"$' /etc/os-release
guest_architecture="$(uname -m)"
case "$guest_architecture" in x86_64|aarch64) ;; *) exit 1 ;; esac
host_values="$(powershell.exe -NoLogo -NoProfile -NonInteractive -Command \
  '[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); $env:PROCESSOR_ARCHITECTURE; [Environment]::OSVersion.Version.Build; (Get-CimInstance Win32_OperatingSystem).ProductType' | tr -d '\r')"
printf '%s\n' "$host_values"
host_architecture="$(printf '%s\n' "$host_values" | sed -n '1p')"
windows_build="$(printf '%s\n' "$host_values" | sed -n '2p')"
product_type="$(printf '%s\n' "$host_values" | sed -n '3p')"
host_architecture="${host_architecture//[[:space:]]/}"
windows_build="${windows_build//[[:space:]]/}"
product_type="${product_type//[[:space:]]/}"
test -n "$host_architecture"
test -n "$windows_build"
test -n "$product_type"
case "$windows_build" in ''|*[!0-9]*) exit 1 ;; esac
test "$windows_build" -ge 22000
test "$product_type" = 1
host_architecture="${host_architecture^^}"
case "$host_architecture:$guest_architecture" in
  AMD64:x86_64|ARM64:aarch64) ;;
  *) exit 1 ;;
esac
```

**3. Python 3.12の専用conda環境を作成**

同名の環境が既にある場合は上書きせず停止します。既に導入済みなら、下の「次回の起動」を使います。

```bash
set -euo pipefail
env_name=gwexpy-studio-wsl2
if ! env_list="$(conda env list)"; then
  echo 'conda env list failed; stop' >&2
  exit 1
fi
if printf '%s\n' "$env_list" | awk -v name="$env_name" '$1 == name { found=1 } END { exit found ? 0 : 1 }'; then
  echo "conda environment already exists: $env_name" >&2
  exit 1
fi
conda create -n "$env_name" python=3.12 pip
conda activate "$env_name"
guest_architecture="$(uname -m)"
case "$guest_architecture" in x86_64|aarch64) ;; *) exit 1 ;; esac
export PYTHONNOUSERSITE=1
unset PYTHONPATH
GUEST_ARCHITECTURE="$guest_architecture" python - <<'PYTHON'
import os
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine().lower() == os.environ["GUEST_ARCHITECTURE"]
PYTHON
python --version
conda --version
python -m pip --version
```

**4. 画面表示用ライブラリの確認・不足時の導入**

不足がなければシステムパッケージは導入しません。不足がある場合は`sudo`で導入します。管理者権限がない場合は、管理者へ依頼してください。

```bash
probe_runtime() {
  python - <<'PYTHON'
import ctypes

libraries = (
    "libEGL.so.1", "libGL.so.1", "libfontconfig.so.1", "libglib-2.0.so.0",
    "libdbus-1.so.3", "libxkbcommon.so.0", "libzstd.so.1", "libX11-xcb.so.1",
    "libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0",
    "libxcb-keysyms.so.1", "libxcb-randr.so.0", "libxcb-render-util.so.0",
    "libxcb-shape.so.0", "libxcb-shm.so.0", "libxcb-sync.so.1",
    "libxcb-xfixes.so.0", "libxcb-xkb.so.1", "libxkbcommon-x11.so.0",
    "libSM.so.6", "libICE.so.6", "libwayland-client.so.0",
    "libwayland-cursor.so.0",
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
PYTHON
}

if probe_runtime; then
  :
else
  packages=(
    libegl1 libgl1 libfontconfig1 libglib2.0-0t64 libdbus-1-3 libxkbcommon0
    libzstd1 libx11-xcb1 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1
    libxcb-randr0 libxcb-shape0 libxcb-sync1 libxcb-xfixes0 libxkbcommon-x11-0
    libsm6 libwayland-cursor0
  )
  unavailable=()
  sudo apt-get update
  for package in "${packages[@]}"; do
    if ! policy="$(LC_ALL=C apt-cache policy "$package")"; then
      echo "apt-cache policy failed for $package" >&2
      exit 1
    fi
    candidate="$(printf '%s\n' "$policy" | awk '/Candidate:/ { print $2; exit }')"
    if test -z "$candidate" || test "$candidate" = '(none)'; then
      unavailable+=("$package")
    fi
  done
  if ((${#unavailable[@]})); then
    printf 'No package candidate: %s\n' "${unavailable[*]}" >&2
    echo 'Ask an administrator to enable the official Ubuntu Universe repository; this guide does not change repositories.' >&2
    exit 1
  fi
  sudo apt-get install --no-install-recommends -y "${packages[@]}"
  probe_runtime
fi
```

`Qt display runtime: OK`が表示されたら次へ進みます。不足が残っている場合は停止します。

**5. 同梱wheelをインストール**

```bash
architecture="$(uname -m)"
constraints="constraints-ubuntu24-${architecture}.txt"
test -f "$constraints"
shopt -s nullglob
wheels=(gwexpy_studio-*.whl)
test "${#wheels[@]}" -eq 1
test -f "${wheels[0]}"
python -m pip install --only-binary=:all: -c "$constraints" "${wheels[0]}"
```

**起動**

```bash
gwexpy-studio
```

Welcome画面が表示されたら、**Try Sample → Load → Crop → ASD**で動作を確認します。保存して開き直す手順は、この下の「基本Workflow」を参照してください。

**次回の起動**

```bash
conda activate gwexpy-studio-wsl2
export PYTHONNOUSERSITE=1
unset PYTHONPATH
gwexpy-studio
```

[既存の日本語Quick Start](docs/trial/wsl2/Quick-Start.ja.md)

</details>

<a name="install-macos"></a>

### Apple Silicon / macOS 15+

<details>
<summary>インストール手順を開く（このページ内）</summary>

Macの「ターミナル」を開きます。Apple Silicon用のconda、`unzip`、`shasum`が必要です。最初のブロックはcondaが使える元のシェル（通常はzsh）で実行し、それ以降は起動したBashで実行します。Intel Mac用ではありません。

**1. condaの設定を引き継いでBashを起動**

```zsh
if ! CONDA_BASE="$(conda info --base)"; then
  echo "conda info --base failed; stop before starting bash" >&2
  exit 1
fi
export CONDA_BASE
export CONDA_EXE="$CONDA_BASE/bin/conda"
export CONDA_SH="$CONDA_BASE/etc/profile.d/conda.sh"
if ! test -x "$CONDA_EXE"; then
  echo "conda executable was not found; stop before starting bash" >&2
  exit 1
fi
if ! test -r "$CONDA_SH"; then
  echo "conda initialization script was not found; stop before starting bash" >&2
  exit 1
fi
/bin/bash --noprofile --norc
```

**2. BashでOS・CPU・ZIPを確認して展開**

```bash
set -e
if ! source "$CONDA_SH"; then
  echo "conda initialization failed" >&2
  exit 1
fi
set -u
set -o pipefail
shopt -s nullglob

test "$(uname -m)" = "arm64"
macos_major="$(sw_vers -productVersion | cut -d. -f1)"
test "$macos_major" -ge 15
sw_vers

zips=(gwexpy-studio-trial-macos15-arm64-*.zip)
sidecars=(gwexpy-studio-trial-macos15-arm64-*.zip.sha256)
test "${#zips[@]}" -eq 1
test "${#sidecars[@]}" -eq 1
archive="${zips[0]}"
sidecar="${sidecars[0]}"
test "${sidecar%.sha256}" = "$archive"
shasum -a 256 -c "$sidecar"
unzip "$archive"
bundle="${archive%.zip}"
test -d "$bundle"
cd "$bundle"
shasum -a 256 -c SHA256SUMS
```

チェックサムがすべて`OK`になったことを確認し、以後も同じ端末で続けます。現在の作業場所は展開したフォルダーです。

**3. 専用conda環境を作成して同梱wheelをインストール**

同名の環境が既にある場合は上書きせず停止します。既に導入済みなら、下の「次回の起動」を使います。

```bash
set -euo pipefail
env_name=gwexpy-studio-macos
if ! env_list="$(conda env list)"; then
  echo "conda env list failed; stop" >&2
  exit 1
fi
if printf '%s\n' "$env_list" | awk -v name="$env_name" '$1 == name { found=1 } END { exit found ? 0 : 1 }'; then
  echo "conda environment already exists: $env_name; do not overwrite it" >&2
  exit 2
fi

conda create -n "$env_name" python=3.12 pip
conda activate "$env_name"
export PYTHONNOUSERSITE=1
unset PYTHONPATH
python - <<'PYTHON'
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine() == "arm64"
PYTHON
python --version
conda --version
python -m pip --version

constraints_path=constraints-macos15-arm64.txt
wheel=(gwexpy_studio-*.whl)
test -f "$constraints_path"
test "${#wheel[@]}" -eq 1
wheel_path="${wheel[0]}"
test -f "$wheel_path"
python -m pip install --only-binary=:all: \
  -c "$constraints_path" \
  "$wheel_path"
```

**起動**

```bash
gwexpy-studio
```

Welcome画面が表示されたら、**Try Sample → Load → Crop → ASD**で動作を確認します。保存して開き直す手順は、この下の「基本Workflow」を参照してください。

**次回の起動**

新しい端末では、このMac手順の最初のブロックでcondaを引き継いだBashを起動してから実行します。環境作成やインストールを繰り返す必要はありません。

```bash
if ! source "$CONDA_SH"; then exit 1; fi
if ! conda activate gwexpy-studio-macos; then exit 1; fi
export PYTHONNOUSERSITE=1
unset PYTHONPATH
gwexpy-studio
```

[既存の日本語Quick Start](docs/trial/macos/Quick-Start.ja.md)

</details>

### インストールで止まった場合

エラーになった段階と端末のメッセージを控えてください。チェックサムや環境の確認に失敗したまま、後続コマンドを実行しないでください。報告方法は配布ZIP内の`Feedback.ja.md`に記載します。

### 旧Ubuntu reference版について

[2026年9月9日の旧Ubuntu reference版](https://github.com/tatsuki-washimi/gwexpy-studio/releases/tag/trial-0.1.0a1-p.6570427-r6-a1)は、上記の新しい4環境向け試用版とは別です。旧版には旧版に同梱された手順だけを使用します。

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
