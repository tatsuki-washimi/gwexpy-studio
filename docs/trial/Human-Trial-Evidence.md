# Cross-platform human-trial evidence

この記録は、配布後に実施するhuman trial用です。
通常のQuick Startから初見条件と計測を外し、ここで初見性と時間を記録します。
既存の人数、閾値、停止条件は変更しません。

## 対象条件

- WSL2利用者を2名以上含める。
- Mac利用者を1名以上含める。
- 合計を3名以上とする。
- qualification kitを操作した所有者は初見参加者に数えない。
- UbuntuとDebianの追加人数閾値は未確定と記録し、承認なしに補わない。

## 試行表

| 試行 | Target | 実測OS / Arch | Build ID | 初回手動操作 | Install時間 | Install unassisted | Launch unassisted | Save → Close → Open | Project reopen | Workflow時間 | Workflow完了 | 完了／停止段階 | Review表示／継続 | Restore表示／継続 |
| --- | --- | --- | --- | --- | ---: | --- | --- | --- | --- | ---: | --- | --- | --- | --- |
| T01 | | | | | | | | | | | | |
| T02 | | | | | | | | | | | | |
| T03 | | | | | | | | | | | | |

入力記録では、表の値を匿名化した判定入力へ対応づけます。
`trial_id`、`target_id`、`build_id`、`first_manual_use`、`install_seconds`、
`install_unassisted`、`launch_unassisted`、`project_reopen`、
`review_displayed`、`review_continued`、`restore_displayed`、
`restore_continued`、`workflow_completed`、`workflow_seconds`を欠落させません。

Workflow時間はWelcome画面の表示から、Try Sample → Load → Crop → ASD → Save → Close → Openが完了するまでです。
5分で打ち切らず実測値を記録し、判定時に`<= 300 seconds`を評価します。
ReviewまたはRestoreが表示されなかったことはfailureではありません。
表示された場合だけ継続可否を記録します。

## Issue台帳

| Issue ID | 試行ID | Target | 操作段階 | 症状 | severity | root cause ID | workaroundを試したか |
| --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | No |

Issue入力では、`issue_id`、`trial_id`、`platform`、`root_cause_id`、`severity`、
`stage`、`symptom`、`workaround_attempted`を記録します。
`platform`は対応する試行の`target_id`と一致させ、workaroundは原則`No`とします。

## 判定

```text
参加者:                      N >= 3
WSL2参加者:                  2名以上
Mac参加者:                   1名以上
初回の手動操作:              N / N
Install unassisted:          N / N
Launch unassisted:           N / N
Workflow <= 5 min:           ceil(2N / 3)名以上
Save → Close → Open:         N / N
P0:                          0件
Project / Recovery P1:       0件
同一root causeのP1が2名以上: No
Ubuntu / Debian追加人数閾値: 未確定
判定:                        Pass / Stop and fix
```

判定は参加者が行いません。
試験担当者が事実記録からP0またはP1を分類し、既存のhuman-trial evaluatorが受け付ける入力項目と照合します。

installまたはlaunchのunassisted失敗、Save → Close → Openの失敗、5分以内に完了した人数が`ceil(2N / 3)`未満、P0、project／recovery P1、同一root causeのP1が2名以上のいずれかがあれば`Stop and fix`です。
停止条件に達した場合は募集を止め、影響するReleaseをdraftへ戻します。
次回は新しい初見者cohort、新しいsource commit、tag、Build IDで再Buildし、qualificationとgroup summaryをやり直します。
