# GWexpy Studio試用のお願い

GWexpy Studioの配布版について、導入と基本操作の試験にご協力ください。
対象は、今回がGWexpy Studioを初めて手動操作する方です。

Git、GitHubアカウント、source checkoutは必要ありません。
condaでPython 3.12の専用環境を作り、同梱wheelを導入します。

所要時間は環境作成を含めて約30分です。
回答期限は`YYYY-MM-DD`（送付日の7日後）です。

お使いの環境に対応する一つのリンクだけを開いてください。
実物のURL、実際に試験したOS版、回答期限は、配布時に外部の案内一覧へ記入します。

- native Ubuntu 24.04 x86_64: `<ubuntu24-x86_64 prerelease URL>`
- native Debian 13 x86_64: `<debian13-x86_64 prerelease URL>`
- Windows 11 + WSL2/WSLg: `<wsl2-ubuntu24 prerelease URL>`
- Apple Silicon Mac: `<macos15-arm64 prerelease URL>`

ZIPと外側checksumの2ファイルを取得し、ZIPに入っているtarget別の日本語Quick Startへそのまま従ってください。
Quick Startの対象OS、CPU、native／WSLの別を満たさない場合は導入を続けません。

今回はhuman trialの記録として、環境作成・導入時間と、Welcome画面が表示されてから
Try Sample → Load → Crop → ASD → Save → Close → Openを完了するまでのWorkflow時間を、
それぞれ記録します。

ReviewまたはRestoreが表示されなかったことはfailureではありません。
表示された場合だけ、そこから継続できたかを記録します。

install、launch、project reopen、recoveryのいずれかで進行不能になった場合は、
constraintsの変更、source build、その他の回避策を試さないでください。
その時点の画面、端末出力、操作段階を記録して終了してください。

結果はZIP内の`Feedback.ja.md`へ記入し、この案内を送ったメールまたはチャットへ返信してください。
機密の測定データ、認証情報、username、hostname、端末固有path、無関係なlogは送らないでください。
