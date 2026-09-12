# GWexpy Studio WSL2試用版クイックスタート

この手順は、Windows 11上のUbuntu 24.04 WSL2とWSLgで、Linux GUIとして
`wsl2-ubuntu24` prereleaseを使うためのものです。Windows native版ではありません。
x86_64とARM64の両方を対象にします。Windows側とWSL2側は同じCPU系列を選びます。
native UbuntuやmacOSでは使用しません。

Gitやソース操作、GitHubアカウントは不要です。WSL2のUbuntu端末でcondaを使える状態を
用意し、condaとpipが使えるネットワーク、Bash、`unzip`、`sha256sum`を準備します。
WSLgを使えることと、GL不足時に管理者へruntime導入を依頼できることも確認します。
OSのPythonや既存のPython環境は変更しないでください。

この手順でエラーや画面を送る場合は、必ずユーザー名、ホスト名、個人の保存場所、
認証情報、機密測定データを伏せ、エラー表示と直前の操作を案内者へ伝えてください。

## ダウンロードと対応確認

案内された一つのprereleaseリンクから、空のディレクトリへZIPと対応する外側checksum
ファイル（`.zip.sha256`）をダウンロードします。

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

すべて`OK`になることを確認します。失敗した場合はinstallせず、表示と直前の操作を
案内者へ送付してください。

## Windows 11、WSL2、WSLg、CPUの確認

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

`AMD64`と`x86_64`、`ARM64`と`aarch64`の組み合わせだけを使用します。64-bitという
表示だけからCPU系列を推定しません。確認に失敗
した場合は、ユーザー名、ホスト名、個人の保存場所、認証情報、機密の測定データを
伏せてから、表示と直前の操作を案内者へ送付して終了してください。

## 専用conda環境の作成

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
GUEST_ARCHITECTURE="$guest_architecture" python - <<'PY'
import os
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine().lower() == os.environ["GUEST_ARCHITECTURE"]
PY
python --version
conda --version
python -m pip --version
```

以後のコマンドはこの環境をactivateした状態で実行します。user-siteと`PYTHONPATH`
は使用しません。

## Qtの表示runtime確認とinstall

```bash
probe_runtime() {
  python - <<'PY'
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
    print("Missing display runtime libraries:", " ".join(missing))
    raise SystemExit(1)
print("Qt display runtime: OK")
PY
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
    echo '公式UbuntuのUniverseを管理者へ有効化依頼し、この手順ではリポジトリを変更せず停止します。' >&2
    exit 1
  fi
  sudo apt-get install --no-install-recommends -y "${packages[@]}"
  probe_runtime
fi
architecture="$(uname -m)"
constraints="constraints-ubuntu24-${architecture}.txt"
test -f "$constraints"
shopt -s nullglob
wheels=(gwexpy_studio-*.whl)
test "${#wheels[@]}" -eq 1
test -f "${wheels[0]}"
python -m pip install --only-binary=:all: -c "$constraints" "${wheels[0]}"
```

対応するconstraintsとwheelが一つずつ確認できない場合、またはinstallに失敗した
場合は、変更やsource buildをせず、表示と直前の操作を案内者へ送付してください。

## Workflowの実行

```bash
gwexpy-studio
```

Welcomeが表示されたら、**Try Sample → Load → Crop → ASD → Save → Close → Open**を
順に実行します。ReviewまたはRestoreが表示された場合は、表示されたことと、そこから
継続できたかを記録します。About画面のBuild IDも記録してください。

起動、project reopen、recoveryで進行不能になった場合は、回避策を試さず、画面、
端末出力、直前の操作を案内者へ送付して終了してください。

結果は`Feedback.ja.md`へ記入し、案内されたメールまたはチャットへ返信してください。
GitHubアカウントは不要です。機密の測定データ、認証情報、username、hostname、
端末固有path、無関係なlogは送らないでください。

## 既知のI/O制約

確認対象はTimeSeriesのCSV読込です。対象外のI/Oは利用不可と表示され、実行できない
場合があります。表示や操作が想定と異なる場合は、画面と直前の操作を案内者へ送付して
ください。
