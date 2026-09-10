# GWexpy Studio ロードマップ

GWexpy Studioは、Python、Jupyter、GWpy、GWexpyに不慣れな研究者や学生が、
Gitを使わずGUIから解析を始められることを目指します。最初の配布形式はconda
Python 3.12へ導入するtrial wheelです。

## 現在地

M1 public sourceとM2 native Ubuntu trial wheelは完了しています。現在公開済みの
ReleaseはUbuntu 24.04 x86_64のreference validation成果であり、WSL2とmacOSへは
配布しません。

trial build/publish workflowはM1 public sourceに含め、M2で初回buildとqualificationを
実行しました。次はplatform別配布契約を同じpublic sourceへ追加します。

## Milestone

| Milestone | 完了条件 |
| --- | --- |
| M1 Public Source | public commit `P`のcanonical manifestが承認済みsnapshotと一致し、public sourceだけでbuildとtestができる。 |
| M2 Native Ubuntu Build | Ubuntu 24.04 x86_64 reference端末でwheel、checksum、GUI workflow、project/recovery、Build IDを検証する。 |
| M3 Platform Contract | WSL2とmacOSのschema 4 artifact、verifier、qualification kit、workflow、文書を同じ`P`へ統合する。 |
| M4 Technical Qualification | Windows 11 x86_64/ARM64のWSL2/WSLgとApple Silicon macOS 15以上の3構成が、同じ`P`から作った候補ですべて成功する。 |
| M5 Cross-platform Human Trial | WSL2利用者2名以上、Mac利用者1名以上を含む未経験者3名以上が一度だけ共通gateを試す。 |
| M6 Trial Feedback | P0/P1を修正し、必要なら新しい`P`、artifact、qualification、未経験者groupで再試験する。 |

## Platform artifact

Build workflowは`wsl2-ubuntu24`と`macos15-arm64`を別runで作ります。各Releaseは
target名を含むZIPとsidecarの2assetだけを持ちます。schema 4 manifestはtarget、
architecture、constraints、resolution、platform別Quick StartとFeedbackを結びます。
既存Ubuntu artifactのschema 3はread-only互換として検証を継続します。

WSL2版はUbuntu 24.04 x86_64/aarch64のconstraintsを収録します。macOS版はApple
Silicon arm64とmacOS 15以上に限定します。DMG、署名、notarization、native Windows
installerはこの段階に含めません。

## Technical qualification

端末所有者は非公開のqualification kitを展開し、1 commandだけを実行します。
kitは一時conda prefixとstateを作り、binary-only install、native Qt、OpenGL、
clipboard、path、sample workflow、Save → Close → Open、recovery、worker exit、
shared-memory cleanup、Python export、About Build IDを自動検証します。

WSL2ではWindows hostとUbuntu guestのarchitecture、WSL kernel、WSLg、Linux側と
Windows側の日本語・空白入りpathも検証します。macOSではCocoa Qt pluginとnative
file-dialog code pathを検証します。実際のfile pickerの操作性はhuman trialで確認します。

3件のevidenceはpath、username、hostnameを含まないcanonical JSONとし、同じsource
commitであることを一つの`QUALIFICATION-SUMMARY.json`へ結びます。一件でも失敗した
場合はどちらのplatform Releaseも公開しません。

## Human trial

両platformのtechnical qualificationとprerelease公開が完了した後、初めて手動操作する
参加者へ同時に依頼します。条件は次のとおりです。

```text
参加者:                      N >= 3
WSL2参加者:                  2名以上
macOS参加者:                 1名以上
初回の手動操作:              N / N
Install unassisted:          N / N
Launch unassisted:           N / N
Workflow <= 5 min:           ceil(2N / 3)名以上
Save → Close → Open:         N / N
P0:                          0件
Project / Recovery P1:       0件
同一root causeのP1が2名以上: No
```

環境作成・導入時間とWelcome表示後のWorkflow時間を分けます。Review/Restoreは表示
されなかったことをfailureにせず、表示された場合の継続可否を記録します。P0/P1は
参加者ではなく試験側が分類します。

## 後続配布

human trial後にPyPI alphaを検討します。AppImage、standalone directory、`.deb`、
`.app`、DMG、native Windows distributionはfeedbackと利用者構成を基に優先順位を
決めます。
