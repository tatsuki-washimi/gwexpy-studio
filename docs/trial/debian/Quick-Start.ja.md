# GWexpy Studio Debian 13試用版クイックスタート

この手順は、画面を使える通常のDebian 13、x86_64の
`debian13-x86_64` prerelease専用です。
WSL、ARM、別のDebianバージョン、別のLinuxでは使用しません。

Gitやソースコードの操作は不要です。
GitHubアカウントも不要です。
開始前にcondaを導入し、condaとpipの両方が使えるネット接続を用意して、Python 3.12の専用環境を使用します。
DebianのPythonは変更しません。

## 事前に用意するもの

配布物をダウンロードする前に、次を用意します。

- Bash、conda、`unzip`、`sha256sum`。
- condaとpipの両方が使えるネット接続。
- 画面を使えるDebian 13 x86_64のデスクトップ。
- GLの確認で不足が表示された場合に、管理者へGL用パッケージの導入を依頼できること。

別のライブラリが不足しているという表示が出た場合は、その表示を案内元へ知らせます。

## ダウンロードと確認

一つのprerelease linkから、対応する`.zip.sha256`ファイルとZIPを空のフォルダーへダウンロードします。
次の確認は、候補がそれぞれ一つだけであることと、チェックファイルがそのZIPを指すことを確認します。

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

すべてのchecksumが`OK`になることを確認します。
失敗した場合はインストールせず、この手順を終了します。
個人情報を伏せて、表示された内容と失敗したコマンドを案内元へ知らせます。

## Debian 13、x86_64、デスクトップの確認

デスクトップで開いた端末から実行します。
最初の確認で、インストール前にWSLを判定します。

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

確認に失敗した場合はインストールせず、この手順を終了します。
個人情報を伏せて、表示された内容と失敗したコマンドを案内元へ知らせます。

## 専用環境の作成

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

同名の環境がすでにある場合や、PythonのバージョンまたはCPUが一致しない場合は、wheelをインストールせずに終了します。
試用中は環境を有効にしたままにします。
別の端末を開いた場合は、毎回この環境を有効にし、`PYTHONNOUSERSITE=1`を設定し、`PYTHONPATH`を解除します。

## Qt GLの確認とインストール

この確認は、Qtの画面表示に必要な共有ライブラリを読み込みます。
不足があれば、Debianのパッケージ一覧を更新し、記載したパッケージを導入して、同じ確認をもう一度実行します。

```bash
set -euo pipefail
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

配布物に含まれるconstraintsファイルを使ってwheelをインストールします。
次の確認は、配布物の中にwheelファイルが一つだけあることを確認してからインストールします。

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

インストールまたは起動に失敗した場合は、表示された内容と失敗した段階を案内元へ知らせます。

## 操作

```bash
gwexpy-studio
```

Welcome表示後に、**Try Sample → Load → Crop → ASD → Save → Close → Open**を実行します。
SaveとOpenではアプリケーションのファイル選択画面を使います。
Aboutを開き、Build IDを記録します。

起動、プロジェクトの再オープン、復旧で進行できなくなった場合は、表示された内容と直前の操作を案内元へ知らせます。

この配布では時系列CSVの読み込みを確認します。
利用できない操作は画面に表示されます。

`Feedback.ja.md`へ記入し、案内されたメールまたはチャットへ返信してください。
送る前に、ユーザー名、ホスト名、個人の保存場所、認証情報、機密の測定データを伏せ、エラー表示と直前の操作を案内者へ伝えてください。
