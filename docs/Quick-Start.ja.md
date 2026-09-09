# GWexpy Studio 試用版クイックスタート

初回の試験対象はUbuntu 24.04 x86_64だけです。

開始前にMiniforgeなどのconda環境を導入してください。

Git、editable install、source checkoutは必要ありません。

## Releaseのダウンロードと確認

試験担当者から共有されたGitHub prereleaseリンクを開きます。

空のディレクトリへ、ZIPとchecksum sidecarの2ファイルだけをダウンロードしてください。

どちらのファイル名も`gwexpy-studio-trial-`で始まり、sidecarは`.zip.sha256`で終わります。

ダウンロード先のディレクトリで次を実行します。

```bash
shopt -s nullglob
sidecars=(gwexpy-studio-trial-*.zip.sha256)
test "${#sidecars[@]}" -eq 1
sha256sum -c "${sidecars[0]}"
archive="${sidecars[0]%.sha256}"
unzip "$archive"
bundle="${archive%.zip}"
cd "$bundle"
sha256sum -c SHA256SUMS
```

外側のchecksumと、内部の全checksumが`OK`になることを確認してください。

どちらかが失敗した場合はinstallせずに終了します。

## 試験端末の確認

```bash
test "$(uname -m)" = "x86_64"
grep '^VERSION_ID="24.04"$' /etc/os-release
```

どちらかが失敗した場合は、出力を保存して終了してください。

初回試験では、同梱されたaarch64用constraintsへ切り替えません。

## conda環境の作成

```bash
conda create -n gwexpy-studio python=3.12
conda activate gwexpy-studio
python --version
conda --version
pip --version
```

## Qt GL runtimeの確認

wheelをinstallする前に、UbuntuがQt GL runtimeの二つのlibraryをloadできることを確認します。

```bash
python - <<'PY'
import ctypes

for library in ("libEGL.so.1", "libGL.so.1"):
    ctypes.CDLL(library)

print("Qt GL runtime: OK")
PY
```

このcommandが失敗した場合は、次を実行してからpreflightをもう一度実行します。

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends -y libegl1 libgl1
```

`sudo`を使えない場合は、Ubuntuの管理者に`libegl1`と`libgl1`のinstallを依頼してください。

preflightが`OK`になるまで次へ進みません。

## 試用版wheelのinstall

```bash
pip install --only-binary=:all: -c constraints-ubuntu24-x86_64.txt ./gwexpy_studio-*.whl
```

`--only-binary=:all:`により、試験端末でpipがnative dependencyをbuildすることを防ぎます。

installが止まった場合は、端末出力を保存して試験を終了してください。

別のconstraintsへ変更したり、source buildを試したりしません。

## Studioの起動と操作

```bash
gwexpy-studio
```

次の手順を実行してください。

1. **Try Sample**を選ぶ。
2. sampleをCropする。
3. ASDを計算する。
4. projectを保存し、Studioを終了する。
5. Studioを再起動し、保存したprojectを開く。

Aboutダイアログを開き、Build IDが次のcommandの出力と一致することを確認します。

```bash
python - <<'PY'
import json

with open("TRIAL-MANIFEST.json", encoding="utf-8") as stream:
    print(json.load(stream)["build"]["id"])
PY
```

## フィードバックの送付

結果を`Feedback.ja.md`へ記入し、prereleaseリンクが届いたメールまたはチャットへ返信してください。

機密の測定データ、認証情報、無関係なログは送らないでください。

installまたは起動で停止した場合は、その時点の端末出力と、表示できた場合はBuild IDを保存して終了します。
