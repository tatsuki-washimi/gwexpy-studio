# ADR-0004: IPC は control plane / data plane に分離する

- Status: Accepted
- Date: 2026-08-16
- 出典: 構想メモ ADR-002 節（「IPCはControl planeとData planeを分離する」）

## Context

UI と worker（ADR-0003）の間には、小さな制御メッセージ（操作要求、進捗、エラー、
メタデータ）と、巨大な数値配列（TimeSeriesMatrix、Spectrogram、preview）の両方が
流れる。すべてを `multiprocessing.Queue` に pickle して流すと、巨大配列の
serialize/copy コストで破綻する。

## Decision

転送を2系統に分離する。

```
Control plane                         Data plane
─────────────                         ──────────
operation request / response          large ndarray
progress / warning / error            Spectrogram / Matrix / Field
metadata / object reference           preview payload
```

- **Control plane**: versioned な JSON 互換メッセージ。全メッセージに
  `protocol: <version>` と `request_id` を持たせる。実装は `Pipe` の
  `send_bytes/recv_bytes` に JSON を流す（`Connection.send()` の pickle 経路を
  使わない）。encode/decode を1モジュールに集約し、将来 socket（remote worker）へ
  置換する箇所を閉じ込める。
- **Data plane**: `multiprocessing.shared_memory`（必要に応じ mmap）。
  serialize/copy を避けて大配列を渡す。ブロックの `close()/unlink()` という
  lifetime 管理が必要になるため、ownership protocol（誰が作り、誰が解放するか）を
  明示的に設計する。
- **Apache Arrow は延期**。zero-copy・言語非依存で強力だが、gwexpy オブジェクト全体を
  Arrow 化する必要は現時点でない。tabular payload（SegmentTable 等）、remote worker、
  Rust worker が視野に入った時点で再検討する。
  `NumPy-like payload → shared_memory / gwexpy semantics → metadata・Operation model`
  の分担で当面は足りる。
- **pickle の位置づけ**: project 形式には一切使わない（ADR-0007）。spawn が
  `Process(args=...)` のハンドル受け渡しに内部で pickle を使うのは multiprocessing の
  標準機構であり、「データやドメイン状態の直列化に pickle を使わない」という設計原則
  とは別レイヤとして許容する。

## Consequences

- shared_memory の orphan block（解放漏れ）が新たな故障モードになる。ownership を
  一方向（worker が作り、UI が copy-out して ack、worker が unlink）に固定し、
  テストで「解放後の reattach が失敗すること」を検証する。
- メッセージが JSON 互換に制約されるため、numpy スカラや Quantity は境界で
  プリミティブへ変換する規約が必要（ADR-0008 の単位正規化と整合）。
- protocol version を最初から持つので、UI と worker の版ずれを検出できる。

## 検討した代替案

- **すべて Queue + pickle**: 実装は最少だが、巨大配列で copy が二重三重に走る。
  また pickle 経路はメッセージ形を強制できず、remote 化の置換点も曖昧になる。
- **最初から Arrow**: 依存とフォーマット変換層が増える割に、単一マシン・
  NumPy 配列中心の alpha では利得がない。
- **gRPC / ZeroMQ**: remote 実行と同時に検討すべきもので、ローカル 1:1 の
  現段階では過剰。abstract worker interface を保つことで将来差し替え可能にする。
