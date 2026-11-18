# pico-firmware（新設計・wakeCon参考）

PokeCon から Switch へ入力を届ける Pico 用ファームウェアの新設計を置く場所である。PC 側とのプロトコル互換は維持しない（破棄可）。

## 位置づけ

- 削除済みの `pico_firmware/`（旧 Q/R キュー機・superseded）とは別物である。旧フォルダは未使用のため削除しており、名前の綴り（ハイフン）で区別する。
- **参考実装**: 別リポジトリ `Moi-poke/pico-wakeCon` をプロトコル参照先とする。現行線の仕様は UART0 115200 8N1＋USB CDC、CRLF、`S <6hex>` 入力（無応答）、`N`（応答待ちなし）、`O <4hex> → color ...` 応答、200 ms watchdog、M モニタ 2 行目 `press/ok/ng`（累積・差分評価）である。
- **被験環境**: 有線モード（`usb en=1`、`cid=0`）である。有線 USB 入力は 10 ms タスク駆動である。

## 要求（優先順。実測根拠は `docs/superpowers/specs/2026-09-12-live-input-scheduler-design.md` §5）

1. UART 受信を割込み化＋リングバッファ化し、ポーリング＋32 B FIFO の溢れを根治する。目標は素シリアル 125 Hz フルレート連送で `ng +0` とすることである（現行は 12 ms 間隔で `ng +12/20 試行`）。
2. 有線 USB タスク 10 ms を可能な範囲で短縮し、HID レポート間隔を安定化して 125 Hz を目標とする。
3. BT 接続間隔との整合を確認する（BT 使用時）。
4. プロトコルは新設計としてよい。行単位テキスト＋CRLF の維持、M 相当の計数チャネル維持、watchdog 相当の維持信号（200 ms 未満）を推奨する。バイナリ化する場合は固定長＋連番＋CRC とする。
5. 受入条件は (a) 125 Hz 連送で `ng +0`、(b) burst 50 ms / 100 ms × 240 で M press 差分 240、(c) E4 遅延分布の改善とする。ビルド＋CTest＋フラッシュ＋回帰を毎回実行する。

## 運用

- このフォルダは Python 系 gate（`task ci`・pre-commit）の対象外である。C 用 CI は別ワークフローで追加する（未整備）。
- FW 安定後は別リポジトリへの分割を検討する（`finishing-a-development-branch` で裁定する）。

## 未確定

- 基板（RP2040 / Pico W / wakeCon 基板のいずれか）および主経路（有線優先か BT 優先か）は未確定である。
- **詳細引継ぎ**: 未転記である（別セッションの handoff メモは現存しない。決まったことはこの README に直接書く）。
