# GWexpy Studio アーキテクチャ概観

GWexpy Studioは、GWexpyのpublic APIを解析エンジンとして使うdesktop scientific workbenchです。

GUIの操作を、再現可能なscientific operationとStudioに依存しないPython codeへ対応付けます。

## 実行構成

```text
Qt main thread
  MainWindow, dock, model, plot canvas
        ↓ typed command and result
Qt bridge thread
  WorkerBridge
        ↓ JSON control plane
Scientific worker process
  GWexpy object store and operation execution
        ↓ shared-memory data plane
immutable NumPy copies for the GUI
```

Qt main threadでは科学計算を実行しません。

Worker processはGWexpy objectをprocess-local object storeに保持します。

大きなarrayはshared memoryで渡し、consumer側はimmutable copyを作成した後にhandleをreleaseします。

## Scientific state

GUI widget、GWexpy object、Matplotlib `Figure`はcanonical scientific stateではありません。

Qt非依存の`Project`と`OperationGraph`が、source、object、semantic operation、execution record、plot specificationを保持します。

```text
GUI command
  ↓
semantic operation
  ├─ GWexpy public APIによる実行
  ├─ OperationGraphとExecutionRecordへの記録
  └─ Studioに依存しないPython codeの生成
```

Scientific Undo/Redoとview Undo/Redoは別の履歴として扱います。

projectはdata-only JSONとして保存し、source dataとcomputed arrayを埋め込みません。

保存済みprojectを開くときは、source identityとrecovery snapshotを確認してからrestore候補を提示します。

## Data I/O

I/O policyはdata type、format、read/write directionごとのstatic tierで定義します。

GUI processはembedded policy manifestを読み、worker起動前にそのpolicyを有効化します。

optional backendのimportとruntime probeはGUI processでは実行しません。

worker bootstrapがregistryとbackendを検査し、effective capability snapshotをGUIへ返します。

```text
static policy
  ∩
worker runtime state
  ↓
effective capability
  ├─ Verified
  ├─ Experimental
  └─ Unavailable with a safe reason
```

このsnapshotはOpen/Save UIだけでなく、read、auto-identification、writeの実行境界でも使います。

Diagnosticsにはpathを含めず、effective capability snapshotとそのdigestを含めます。

## 配布境界

最初の外部試用はwheelを使います。

Studio wheelはpure Python artifactとして一度だけbuildし、x86_64とaarch64ではそれぞれ固定したruntime dependency closureを検証します。

canonical source identityはGit historyではなく、path、portable mode、content hashから作るmanifestで定義します。

private sourceから作るsnapshotを`S`、public commitを`P`、detached source manifestを`M`とすると、M1の条件は`manifest(S) == manifest(P) == M`です。

`P`のmanifestではlocal `.git`だけを除外します。

directory permissionやhost固有のpermission差は含めず、Gitが表現できる`0644`と`0755`へ正規化します。

trial buildではsource manifestとbuild-time deltaを分け、wheelがsource treeのbyte-for-byte copyであると誤って主張しません。

詳細なmilestoneは[../ROADMAP.md](../ROADMAP.md)に記載しています。
