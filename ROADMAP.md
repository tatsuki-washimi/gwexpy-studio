# GWexpy Studio ロードマップ

GWexpy Studioは、Python、Jupyter、GWpy、GWexpyに不慣れな利用者がGUIから解析に触れ、徐々にPython APIへ進めることを目指します。

もう一つの目的は、測定直後などにprogramを書かずにデータをすばやく確認し、基本解析を行えることです。

最初の利用者は、condaやpipは使えるがGitやsoftware developmentには慣れていない物理系の研究者と学生です。

そのため、最初の配布はGit不要のcondaとpipによるtrial wheelとします。

AppImage、DMG、native Windows installerは、その導線を実利用者で検証した後に進めます。

## 現在地

private developmentでは、GUI基本操作、GWexpy native I/O、arithmetic、filter、PSD、CSD、coherence、transfer function、Bode表示、project save/reopen、scientific Undo/Redo、view Undo/Redo、crash recovery、provenance、Python exportを実装しています。

最初の一般利用者向け配布とhuman trialはまだ実施していません。

## M1からM4

| Milestone | 完了条件 |
| --- | --- |
| M1 Public Source | public commit `P`のcanonical manifestが承認済みsnapshot `S/M`と一致し、public sourceだけでbuild/testできる。 |
| M2 Trial Wheel | 同じStudio wheelをLinux x86_64とaarch64の固定runtime closureでinstallし、core workflowをtechnical qualificationできる。 |
| M3 Ubuntu Trial | Ubuntu 24.04 x86_64で、開発者以外の3人がGitなしの導入と5分workflowを試す。 |
| M4 WSL2 Trial | Windows 11上のx86_64 WSL2とARM64 WSL2を実機qualificationし、両architectureを含む3人が試す。 |

### M1 Public Source

private release candidateからpublicに出せるcanonical snapshot `S`を作ります。

source identityはGit commit historyではなく、path、portable mode、content hashのmanifestで定義します。

public commit `P`は、local `.git`だけを除外して`manifest(S) == manifest(P)`を満たす必要があります。

public snapshotにはproduct source、public tests、fixtures、schemas、assets、packaging metadata、source identity tooling、docs、CIを含めます。

trial wheelのbuild/publish workflowはM1 public sourceに含め、M2で初回buildとqualificationを実行します。

private audit evidence、internal note、private workflow data、harness、local build productは含めません。

### M2 Trial Wheel

trial wheelはpure Python artifactとして一度だけbuildします。

x86_64とaarch64では、architecture別にruntime dependency closureを固定して検証します。

trial assetにはwheel、architecture別constraints、resolution report、source manifest、trial manifest、checksum、Quick Startを含めます。

wheelはclean environmentでinstallでき、source tree外から`gwexpy-studio`を起動できなければなりません。

Welcome、Try Sample、Crop、ASD、project save/reopen、recovery、worker cleanupが両architectureのblocking gateです。

Build workflowは`contents: read`だけを持ちます。

Publish workflowは`actions: read`と`contents: write`に分離し、protected environmentのhuman approval後にGitHub prereleaseを作ります。

publish時はdefault branch HEADが`P`であることをapproval前後に確認します。

### M3 Ubuntu 24.04 Trial

reference machineでtechnical qualificationを行った後、開発者以外の3人にtrial wheelを配ります。

conda environment作成とinstall/launchは3人全員が説明なしで完了する必要があります。

Welcome表示後の5分workflowは、3人中2人以上の完了を条件にします。

P0 issueとproject/recoveryに関するP1 issueは残せません。

同じP1 issueが2人以上に起きた時点でtrialを止め、修正します。

### M4 Windows 11、WSL2、WSLg Trial

Windows 11 x86_64上のUbuntu 24.04 x86_64と、Windows 11 ARM64上のUbuntu 24.04 aarch64を別々にtechnical qualificationします。

WSLg、display scaling、clipboard、worker lifecycle、shared memory、`/mnt/c`、日本語path、spaceを含むpath、save/reopen、recovery、Python exportを確認します。

human trialは3人合計とし、x86_64とARM64をそれぞれ少なくとも1人含めます。

ARM ChromebookはLinux aarch64の補助smoke testです。

macOSはM6以降にApple Siliconを優先して外部testerが確認します。

## M5以降

M5ではUbuntuとWSL2のfeedbackをinstallation、UX、scientific workflowに分類して修正します。

必要性が高ければ、HistoryのParameters、Result、Show Pythonを優先します。

M6ではDebian 13とmacOSをwheelとcondaでtechnical trialします。

M7では、M3とM4のhuman trial、clean wheel install、project/recovery、Quick Start、build identityが揃った時点でPyPI alphaを検討します。

M8ではtrial feedbackに基づき、AppImage、standalone directory、`.deb`、`.app`、DMG、native Windows distributionを優先度順に進めます。

その後はShow Python、before/after比較、overlay、Preview/Apply、mouse crop、CSV/HDF5 onboarding、multi-channel analysis、plot styling、Marimo export、portable projectを強化します。

## 最初の成功条件

Gitを知らない研究者または学生が、condaとpipだけでStudioを起動し、5分以内にデータを見て基本解析を行い、作業を保存できることを最初の成功条件とします。
