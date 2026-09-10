# GWexpy Studio WSL2試用フィードバック

対象は`wsl2-ubuntu24`です。分かる範囲で記入してください。
P0/P1は参加者ではなく試験担当者が事実記録から分類します。

## 参加条件と環境

- GWexpy Studioを今回より前に手動操作したことがない（はい / いいえ）:
- Windows 11のversion:
- Windows architecture（AMD64 / ARM64）:
- WSL guest OSとversion:
- guest architecture（x86_64 / aarch64）:
- WSL kernel:
- Python / conda / pipのversion:
- ZIP名と外側checksum（成功 / 失敗）:
- 内部`SHA256SUMS`（成功 / 失敗）:
- `libEGL.so.1` / `libGL.so.1` preflight（成功 / 失敗）:

## 導入と起動

- `--only-binary=:all:`と同梱constraintsを変更せずinstallできた（はい / いいえ）:
- Install unassisted（はい / いいえ）:
- Launch unassisted（はい / いいえ）:
- 環境作成・導入時間:
- Welcomeが表示された（はい / いいえ）:

## Workflow

Welcome表示から**Try Sample → Load → Crop → ASD → Save → Close → Open**完了までを
計測してください。

- Workflow時間:
- Try Sample / Load / Crop / ASDの結果:
- Save → Close → Openの結果:
- Workflow完了（はい / いいえ）:
- 完了または停止した操作段階:
- Review表示（あり / なし）と、表示時に継続できたか:
- Restore表示（あり / なし）と、表示時に継続できたか:
- About画面のBuild ID:

## 操作性と問題

- clipboard、DPI、scaling、Linux側path、`/mnt/c`の操作で気付いた点:
- 分かりにくかった表示、用語、操作:
- エラーメッセージと、その直前の操作:
- 今後使いたい解析機能:
- 自由記述:

install、launch、project reopen、recoveryで進行不能になった場合は、回避策を
試さず、その時点の画面、端末出力、操作段階を記録して終了してください。

案内されたメールまたはチャットへ返信してください。GitHubアカウントは不要です。
機密の測定データ、認証情報、username、hostname、端末固有path、無関係なlogは
送らないでください。
