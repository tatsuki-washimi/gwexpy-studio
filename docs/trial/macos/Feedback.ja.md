# GWexpy Studio macOS試用フィードバック

対象は`macos15-arm64`です。
分かる範囲で、画面と端末に表示された事実を記入してください。

## Macと配布物

- Mac機種とApple Silicon:
- macOS version（`sw_vers`の結果）:
- architecture（`arm64`）:
- Python / conda / pipのversion:
- ZIP名:
- ZIPの外側`.zip.sha256`（成功 / 失敗）:
- 内部`SHA256SUMS`（成功 / 失敗）:
- Build ID（Aboutを開いて記録）:

## 導入と起動

- `python=3.12`と`pip`の専用conda環境を作成できた（はい / いいえ）:
- `PYTHONNOUSERSITE=1`を設定し、`PYTHONPATH`をunsetして導入した（はい / いいえ）:
- `--only-binary=:all:`と同梱`constraints-macos15-arm64.txt`を変更せずinstallできた（はい / いいえ）:
- 起動できた（はい / いいえ）:
- Welcomeが表示された（はい / いいえ）:

## Workflow

- `Try Sample → Load → Crop → ASD`の結果:
- この配布で確認した時系列CSVの読込結果:
- 使用不可と画面表示された種類:
- Save → Close → Openの結果:
- ファイル選択画面の結果:
- 日本語と空白を含む保存場所の結果:
- Review表示（あり / なし）。表示時に継続できたか:
- Restore表示（あり / なし）。表示時に継続できたか:
- Workflowの完了または停止した操作段階:

## 操作性と問題

- clipboard、表示倍率で気付いた点:
- 分かりにくかった表示、用語、操作:
- エラーメッセージと、その直前の操作:
- 再現手順:
- 今後使いたい解析機能:
- 自由記述:

install、起動、再オープン、復旧で進行できない場合は、エラー表示と直前の操作を案内者へ連絡してください。
送る前に、ユーザー名、ホスト名、個人の保存場所、認証情報、機密の測定データを伏せてください。

記入後は、案内されたメールまたはチャットへ返信してください。
GitHubアカウントは不要です。
