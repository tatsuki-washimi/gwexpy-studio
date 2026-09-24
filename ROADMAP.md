# GWexpy Studio ロードマップ

GWexpy Studioは、Python、Jupyter、GWpy、GWexpyに不慣れな研究者や学生が、
Gitを使わずGUIから解析を始められることを目指します。最初の配布形式はconda
Python 3.12へ導入するtrial wheelです。

## 現在地

M1 public sourceとM2 native Ubuntu referenceの成果物は既存の基準として保持します。
今回の候補は、Ubuntu 24.04 x86_64、Debian 13 x86_64、WSL2の2構成、Apple Silicon
macOSの1構成を同じsource commitからBuildする計画です。

現時点では、今回の正式Build、5構成のphysical qualification、公開承認、公開後の再取得検証は未完了です。
旧Ubuntu referenceは新しい4target trialとは別のschema 3 artifactとして扱います。

## Milestone

| Milestone | 完了条件 |
| --- | --- |
| M1 Public Source | public commit `P`のcanonical manifestが承認済みsnapshotと一致し、public sourceだけでbuildとtestができる。 |
| M2 Native Ubuntu Build | Ubuntu 24.04 x86_64 reference端末で初回buildとqualificationを実施し、wheel、checksum、GUI workflow、project/recovery、Build IDを検証する。 |
| M3 Platform Contract | WSL2とmacOSのschema 4 artifact、verifier、qualification kit、workflow、文書を同じ`P`へ統合する。 |
| M4 Technical Qualification | Windows 11 x86_64/ARM64のWSL2/WSLgとApple Silicon macOS 15以上の3構成が、同じ`P`から作った候補ですべて成功する。 |
| M5 Cross-platform Human Trial | WSL2利用者2名以上、Mac利用者1名以上を含む未経験者3名以上が一度だけ共通gateを試す。 |
| M6 Trial Feedback | P0/P1を修正し、必要なら新しい`P`、artifact、qualification、未経験者groupで再試験する。 |

## Platform artifact

Build対象は`ubuntu24-x86_64`、`debian13-x86_64`、`wsl2-ubuntu24`、
`macos15-arm64`の4targetです。
WSL2はx86_64とaarch64を含み、physical configurationは5つです。
各Releaseはtarget名を含むZIPとsidecarの2assetだけを持ちます。
schema 4 manifestはtarget、architecture、constraints、resolution、platform別Quick StartとFeedbackを結びます。
旧Ubuntu artifactのschema 3はread-only互換として検証を継続します。

公開groupはUbuntu、Debian、WSL2＋Macの3つです。
5構成のaudit summaryはgroup summaryとは別に保持します。

WSL2版はUbuntu 24.04 x86_64/aarch64のconstraintsを収録します。macOS版はApple
Silicon arm64とmacOS 15以上に限定します。DMG、署名、notarization、native Windows
installerはこの段階に含めません。

## Technical qualification

端末所有者は非公開のqualification kitを展開し、提供された手順で実行します。
kitは一時conda prefixとstateを作り、binary-only install、native Qt、OpenGL、
clipboard、path、sample workflow、Save → Close → Open、recovery、worker exit、
shared-memory cleanup、Python export、About Build IDを自動検証します。

WSL2ではWindows hostとUbuntu guestのarchitecture、WSL kernel、WSLg、Linux側と
Windows側の日本語・空白入りpathも検証します。macOSではCocoa Qt pluginとnative
file-dialog code pathを検証します。

所有者は自動試験と同じ環境で、native dialog、日本語と空白を含むpath、通常倍率、別の利用可能な倍率を確認します。
canonical結果にはdialogと倍率の合否だけを記録し、倍率の数値は非公開の作業メモへ残します。
未回答、EOF、中断、cleanup失敗、結果保存失敗は合格にしません。

5件のevidenceはpath、username、hostnameを含まないcanonical JSONとし、同じsource
commitであることをaudit summaryへ結びます。
公開group summaryはUbuntu 1構成、Debian 1構成、WSL2 2構成とMac 1構成をそれぞれ結びます。

## 実行工程

過去のM1からM6は、public sourceからfeedbackまでの歴史的milestoneとして残します。
今回の候補は、R0で契約と所有者受け渡しを確定し、R1で共通実装とOS別準備、R2で統合回帰と`P_trial`固定、R3で4target正式Build、R4で5構成qualification、R5で公開承認と再取得検証を行います。

## Human trial

配布後、既存のhuman trial条件で初めて手動操作する参加者へ依頼します。
kit所有者は初見参加者に数えません。

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
判定:                      Pass / Stop and fix
```

環境作成・導入時間とWelcome表示後のWorkflow時間を分けます。Review/Restoreは表示
されなかったことをfailureにせず、表示された場合の継続可否を記録します。P0/P1は
参加者ではなく試験側が分類します。
install／launch unassistedの失敗、同一root causeのP1が2名以上、その他の停止条件では募集を止め、影響するReleaseをdraftへ戻します。
再試験は新しい初見者cohort、新source、tag、Build IDで行い、qualificationとgroup summaryを作り直します。

## 後続配布

human trial後にPyPI alphaを検討します。AppImage、standalone directory、`.deb`、
`.app`、DMG、native Windows distributionはfeedbackと利用者構成を基に優先順位を
決めます。
