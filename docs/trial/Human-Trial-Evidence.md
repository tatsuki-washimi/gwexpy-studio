# Cross-platform human-trial evidence

この文書は、`wsl2-ubuntu24`と`macos15-arm64`のtechnical qualificationがすべて
成功した後に行う、1回のhuman trialの匿名記録様式です。raw feedbackや端末固有の
path、username、hostnameはpublic repositoryへ保存しません。

## 試行表

| 試行 | 初回手動操作 | Platform / OS / Arch | Install unassisted | Launch unassisted | 環境作成・導入時間 | Workflow時間 | Workflow完了 | 完了/停止段階 | Save → Close → Open | Review表示/継続 | Restore表示/継続 | P0 | P1 | Build ID |
| --- | --- | --- | --- | --- | ---: | ---: | --- | --- | --- | --- | --- | ---: | ---: | --- |
| T01 | | | | | | | | | | | | | | |
| T02 | | | | | | | | | | | | | | |
| T03 | | | | | | | | | | | | | | |

Workflow時間はWelcome画面の表示から、Try Sample → Load → Crop → ASD → Save →
Close → Openが完了するまでです。5分で打ち切らず実測値を記録し、判定時に
`<= 300 seconds`を評価します。ReviewまたはRestoreが表示されなかったこと自体は
failureにしません。表示された場合だけ継続可否を記録します。

## Issue台帳

| Issue ID | 試行ID | platform | 操作段階 | 症状 | severity | root cause ID | workaroundを試したか |
| --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | No |

参加者はseverityを分類しません。試験担当者が事実記録からP0/P1を分類します。
進行不能時のworkaroundは原則`No`です。

## 判定

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
M3判定:                      Pass / Stop and fix
```

`scripts/evaluate_human_trial.py`へ匿名化したJSONを渡すと、同じ条件をcanonical
JSONとして評価できます。Stop条件に達した場合は募集を止め、影響するReleaseを
draftへ戻します。修正版は新しいsource commit、tag、Build IDで作り直します。
