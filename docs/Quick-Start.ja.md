# GWexpy Studio 試用版クイックスタート

この試用版は Ubuntu 24.04 Linux 用です。

WSL2 上の Ubuntu 24.04 でも同じ手順を使います。

開始前に Miniforge などの conda 環境を導入してください。

試用版 bundle を展開し、そのディレクトリで端末を開きます。

bundle には wheel と対応する dependency constraints が入っています。

## ダウンロードの確認

install の前に次を実行します。

```bash
sha256sum -c SHA256SUMS
```

すべての行が `OK` になることを確認してください。

一つでも失敗した場合は install しないでください。

## conda 環境の作成

```bash
conda create -n gwexpy-studio python=3.12
conda activate gwexpy-studio
```

## 試用版 wheel の install

Linux architecture を確認します。

```bash
uname -m
```

表示が `x86_64` の場合は次を実行します。

```bash
pip install --only-binary=:all: -c constraints-ubuntu24-x86_64.txt ./gwexpy_studio-*.whl
```

表示が `aarch64` の場合は次を実行します。

```bash
pip install --only-binary=:all: -c constraints-ubuntu24-aarch64.txt ./gwexpy_studio-*.whl
```

`--only-binary=:all:` により、試用端末で pip が native dependency を build することを防ぎます。

## Studio の起動

```bash
gwexpy-studio
```

Welcome 画面で **Try Sample** を選びます。

sample を Crop し、ASD を計算して project を保存してください。

Studio を閉じてから再度 `gwexpy-studio` を実行し、保存した project を開きます。

この試用では Git、editable install、source checkout は必要ありません。

## install が止まった場合

端末出力、trial bundle、About ダイアログに表示される Build ID を残してください。

別の constraints file に置き換えたり、`--only-binary=:all:` なしで install をやり直したりしないでください。
