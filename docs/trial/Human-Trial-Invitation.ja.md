# GWexpy Studio試用のお願い

GWexpy Studioの試作版について、導入と基本操作の試験にご協力ください。
対象は、GWexpy Studioをこれまで手動操作したことがない方です。

所要時間は環境作成を含めて約30分です。回答期限は`YYYY-MM-DD`（送付日の7日後）
です。GitとGitHubアカウントは必要ありません。

お使いの環境に対応する一つのリンクだけを開いてください。

- Windows 11 + WSL2/WSLg + Ubuntu 24.04:
  `<wsl2-ubuntu24 prerelease URL>`
- Apple Silicon Mac + macOS 15以上:
  `<macos15-arm64 prerelease URL>`

ZIPとchecksumの2ファイルを取得し、ZIPに入っている日本語Quick Startへそのまま
従ってください。installには、同梱constraintsと`--only-binary=:all:`を使用します。

環境作成・導入に要した時間と、GWexpy StudioのWelcome画面が表示されてから
Try Sample → Load → Crop → ASD → Save → Close → Openを完了するまでのWorkflow時間を、
それぞれ記録してください。

install、launch、project reopen、recoveryのいずれかで進行不能になった場合は、
constraintsの変更、source build、その他の回避策を試さないでください。その時点の
画面、端末出力、操作段階を記録して終了してください。

結果はZIP内の`Feedback.ja.md`へ記入し、この案内を送ったメールまたはチャットへ
返信してください。機密の測定データ、認証情報、username、hostname、端末固有path、
無関係なlogは送らないでください。
