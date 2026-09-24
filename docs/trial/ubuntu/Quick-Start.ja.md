# GWexpy Studio Ubuntu試用版クイックスタート

この手順は、通常のグラフィカルデスクトップで動作する native Ubuntu 24.04
x86_64の`ubuntu24-x86_64` prerelease専用です。
WSL2、Windows上のUbuntu、ARM64、他のLinuxディストリビューションでは使用しません。

condaを使い、OSのPythonとは分けて導入します。
Gitやソースコードの操作、GitHub accountは不要です。

## 前提条件

Bash、conda、condaとpipが使えるネットワーク、`unzip`、`sha256sum`を用意してください。
通常のグラフィカルデスクトップセッションで実行します。
runtime確認に失敗した場合は、下記のUbuntu package導入を管理者へ依頼してください。

## ダウンロードと検証

案内されたrelease linkからUbuntu用ZIPと対応する`.zip.sha256` sidecarを、空のディレクトリへダウンロードします。
次のコマンドは、ZIPとsidecarの候補がそれぞれ一つの場合だけ進みます。
外側のchecksumとZIP内部のファイルも検証します。

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

すべてのchecksumが`OK`になることを確認します。
一つでも失敗した場合はinstallせずに終了します。

## Ubuntuの確認

install前に次を実行します。
WSLを拒否し、Ubuntuのversion、CPU、グラフィカルセッションを確認します。

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

失敗した場合は出力を記録して終了します。
WSL guestをnative Ubuntuとして扱わず、グラフィカルデスクトップなしで続行しません。

## 分離したconda環境の作成

```bash
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
python - <<'PY'
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine() == "x86_64"
PY
python --version
conda --version
python -m pip --version
```

同名の環境がすでにある場合は、変更せずに終了します。
新しい端末ではこの環境をactivateし、二つの環境設定を再実行します。

## Qt/OpenGLの前提確認とinstall

runtime確認では、native LinuxのX11とWaylandの両方に必要なshared libraryを調べます。
不足しているlibraryはまとめて表示します。

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
    print("Missing runtime libraries:", " ".join(missing))
    raise SystemExit(1)
print("Qt display runtime: OK")
PY
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

導入後もlibraryが不足すると表示された場合は、停止して案内を送った人へそのメッセージを送ってください。

同梱wheelを一つだけ選び、Ubuntu用constraintsを指定してinstallします。
constraintsを置き換えず、ソースからの導入もしません。

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

## 起動とsample workflow

```bash
gwexpy-studio
```

Welcomeから**Try Sample → Load → Crop → ASD → Save → Close → Open**を実行します。
Aboutを開き、表示されたBuild IDを記録します。
ReviewまたはRestoreが表示された場合は、そこから継続できたかを記録します。

起動、再オープン、復旧で進行できなくなった場合は、エラー表示と直前の操作を記録して案内を送った人へ連絡してください。
送る前に、ユーザー名、ホスト名、個人の保存場所、認証情報、機密の測定データを伏せてください。

この配布で確認する読込は時系列CSVです。
その他の形式や入出力は使用できない場合があるため、アプリケーションの表示を確認してください。

## feedbackの送付

`Feedback.ja.md`に記入し、release linkを案内した人またはchannelへ返信してください。
GitHub accountは不要です。
