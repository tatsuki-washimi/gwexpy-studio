# GWexpy Studio macOS試用版クイックスタート

この手順は、Apple Silicon MacのmacOS 15以上で使う`macos15-arm64` prerelease専用です。
Intel Mac、WSL2、Linuxでは使用しません。

Gitやソースコードの操作、GitHubアカウントは不要です。
この手順では、OSに付属するPythonやユーザーsiteを使いません。
利用できる配布物と確認済みOS版は、配布担当者の案内で確認してください。

## 前提条件

Apple Silicon、macOS 15以上、通常のデスクトップ環境で実行します。
元のshellで利用できるconda、condaとpipが使えるネットワーク、`unzip`、`shasum`、bashを用意してください。

## ダウンロードと検証

案内された一つのprereleaseリンクから、ZIPと対応する`.zip.sha256`を空のディレクトリへダウンロードしてください。
Mac標準zshではbash配列をそのまま実行できないため、以下の確認と導入はbashで行います。

まず、condaを利用できる元のshell（通常はcondaを初期化済みのzsh）で、condaの実体と初期化スクリプトの場所を取得します。
base pathは手入力しません。

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

起動したbashで、次を実行してください。

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

ZIP、sidecar、内部`SHA256SUMS`のすべてが`OK`になることを確認します。
一意に選べない場合や検証に失敗した場合は、installせずに案内者へ連絡してください。

## 分離したconda環境への導入

次のコマンドは、condaの環境名が既に存在する場合に既存環境を上書きしません。
その場合は、既存環境を削除・変更せずに案内者へ連絡してください。

```bash
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
python - <<'PY'
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine() == "arm64"
PY
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

wheelは候補を配列で受け、候補が一つの場合だけそのファイルを導入します。
conda環境がactivateされたbashを、試験中の作業shellとして使ってください。
新しい端末では、元のshellでcondaの初期化を確認してbashを起動し、次の短いblockを実行します。

```bash
if ! source "$CONDA_SH"; then exit 1; fi
if ! conda activate gwexpy-studio-macos; then exit 1; fi
export PYTHONNOUSERSITE=1
unset PYTHONPATH
```

installまたは起動に失敗した場合は、手順を変更せず案内者へ連絡してください。

## Workflowの確認

```bash
gwexpy-studio
```

画面が表示されたら、サンプルworkflowを実行します。

`Try Sample → Load → Crop → ASD → Save → Close → Open`

SaveとOpenではファイル選択画面を使い、日本語と空白を含む保存場所も確認します。
表示倍率に問題があれば、再現した操作と表示を記録します。
この配布で確認する読込は時系列CSVです。
使用できない種類は画面に使用不可と表示されます。
ReviewまたはRestoreが表示された場合だけ、表示されたか、そこから継続できたかを記録します。
Aboutを開き、Build IDを記録します。

正常なSave → Close → Openの結果を記録します。
起動、再オープン、復旧で進行できない場合は、エラー表示と直前の操作を案内者へ連絡してください。
送る前に、ユーザー名、ホスト名、個人の保存場所、認証情報、機密の測定データを伏せてください。

## フィードバック

`Feedback.ja.md`へ事実を記入し、案内されたメールまたはチャットへ返信してください。
GitHubアカウントは不要です。
