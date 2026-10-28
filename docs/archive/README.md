# archive — 実装計画とレビュー記録

完了した機能の実装計画（エージェント向け指示書）と、第三者レビューの往復を置く。
**コードの SSOT ではない**。現行実装と食い違う記述があればコードと `docs/` の設計書を正とする。

- 設計根拠（現在も参照される仕様）は [`../superpowers/specs/`](../superpowers/specs) にある。
- 新規の実装計画はここへ置かない。必要な場合は issue か PR の説明に書き、完了後ここへ移す。
- ファイル名は `<日付>-<主題>.md`。日付は着手日。

## plans

| 日付 | 計画 | 内容 |
|---|---|---|
| 2026-09-06 | [omit-queue-wakecon-gui](plans/2026-09-06-omit-queue-wakecon-gui.md) | Q/R キュー経路の削除と wakeCon 対応コマンドの GUI 化 |
| 2026-09-06 | [script-pack-foundation](plans/2026-09-06-script-pack-foundation.md) | manifest v0・テンプレ名前空間・CI |
| 2026-09-06 | [script-pack-install](plans/2026-09-06-script-pack-install.md) | 配布 zip の検証・導入・削除・梱包 |
| 2026-09-06 | [serial-monitor](plans/2026-09-06-serial-monitor.md) | 全二重シリアルモニタ |
| 2026-09-06 | [usb-cdc-console](plans/2026-09-06-usb-cdc-console.md) | Pico の HID+CDC 複合化 |
| 2026-09-07 | [blockly-editor](plans/2026-09-07-blockly-editor.md) | Blockly エディタ基盤 |
| 2026-09-08 | [blockly-vision](plans/2026-09-08-blockly-vision.md) | Blockly 画像認識ブロック Phase A |
| 2026-09-09 | [blockly-template-capture](plans/2026-09-09-blockly-template-capture.md) | テンプレ画像作成 Phase B |
| 2026-09-10 | [audio](plans/2026-09-10-audio.md) | 音声取込・FFT/類似音検知・モニター |
| 2026-09-10 | [blockly-match-sim](plans/2026-09-10-blockly-match-sim.md) | 照合シミュレーション |
| 2026-09-10 | [blockly-match-ux](plans/2026-09-10-blockly-match-ux.md) | 照合 UX 改善 |
| 2026-09-10 | [blockly-preview](plans/2026-09-10-blockly-preview.md) | vision ブロック内プレビュー |
| 2026-09-11 | [image-recognition-expansion](plans/2026-09-11-image-recognition-expansion.md) | グレー/カラー照合・HSV 色域フィルタ |
| 2026-09-12 | [live-input-scheduler](plans/2026-09-12-live-input-scheduler.md) | Pico live 経路のスケジューラ |
| 2026-09-17 | [bcon-transport](plans/2026-09-17-bcon-transport.md) | bcon（Pico 2 W バイナリ）対応 |
| 2026-09-22 | [bcon-proc-phase2](plans/2026-09-22-bcon-proc-phase2.md) | bcon 送出の別プロセス化 |
| 2026-09-25 | [windows-high-resolution-preview-clock](plans/2026-09-25-windows-high-resolution-preview-clock.md) | プレビュー描画の QPC タイマ化 |

## reviews

| 日付 | 記録 | 内容 |
|---|---|---|
| 2026-10 | [README](reviews/README.md) | レビュー記録の索引と残課題 |
| 2026-10 | [unfixed-issues-review](reviews/2026-10-unfixed-issues-review.md) | 未修正課題の洗い出しと初回依頼 |
| 2026-10 | [v4.0-review-request](reviews/2026-10-v4.0-review-request.md) | 実装済み修正 17 件のレビュー依頼 |
| 2026-10 | [v4.0-review-delta](reviews/2026-10-v4.0-review-delta.md) | レビュー 4.1〜4.16 の反映差分 |
