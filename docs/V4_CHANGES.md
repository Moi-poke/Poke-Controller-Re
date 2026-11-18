# Modified版 v4.0.0 変更点（旧リポジトリの `origin/master` 基準）

旧 [Poke-Controller Modified](https://github.com/Moi-poke/Poke-Controller-Modified) の `release/v4.0` が、同リポジトリの主系（`origin/master`）から変わった点を記録した文書である。Re: はこのブランチの履歴を引き継いでおり、現在の開発ブランチは `main` である。利用者向けに分野別でまとめたものであり、開発基盤の話題は末尾に短く置いている。作成時点の基準差分は 73 件である（旧 clone での `git log origin/master..release/v4.0` による）。

## 新機能

### Blocklyエディタ（メニュー→コマンド→「Blocklyエディタ...」）

- ブラウザで操作を組み立て、Python コマンドとして保存・再編集・削除できる。一覧への反映は約 1 秒であり、窓を閉じる必要はない（詳細は [docs/BLOCKLY_EDITOR.md](BLOCKLY_EDITOR.md) を参照）。
- **画像認識ブロック**: テンプレ選択（候補一覧＋プレビュー）・範囲指定・グレー／カラー切替（既定はカラー）・しきい値指定を備える。vision 系を混ぜると自動で画像認識コマンドになる。
- **キャプチャ切出し**: カメラ画像からドラッグで範囲を切り出してテンプレ保存できる。
- **一致度シミュレーション**: 実行前に最新キャプチャ内で一致度％・件数・場所を確認できる。
- **色フィルタの試し打ち**: HSV 範囲と割合表示を使い、静止画で調整してブロックへ反映できる。

### スクリプト実行時のエラー回復

- 画像テンプレートが見つからない、またはカメラが一時的に切れたといった直せるエラーでは、コマンドを止めずに「再試行 / 中止」を尋ねる。テンプレートは画像を置いて再試行でき、カメラは接続し直して再試行できる。
- 画像・カメラの読み取りは操作単位でやり直す。それ以外の回復可能なエラーは、直したあとコマンドを最初から再実行するかを選べる。
- 構文ミス・名前の間違い・型の誤りなど、コードを直さないと直らないエラーは従来どおり即停止し、traceback を残す（詳細は [docs/ARCHITECTURE.md](ARCHITECTURE.md) の「実行時エラーの回復」を参照）。

### 音声対応（Audio欄）

- キャプチャボード音声の取込、特定音の検知トリガー（`waitTone` / `waitSound`）、ゲーム音のモニター再生、および押下→検知の遅延計測に対応した（詳細は [docs/AUDIO.md](AUDIO.md) を参照）。
- 開けるデバイスだけを番号付きで一覧化する（同名重複に強く、推定遅延つきである）。

### シリアルモニタ（メニュー→「シリアルモニタ」）

- 通信ログの時系列表示、フィルタ切替、全量ログ保存に対応しており、監視しても応答の横取りはしない（詳細は [docs/SERIAL_MONITOR.md](SERIAL_MONITOR.md) を参照）。

### 自作スクリプトのzip配布

- `pokecon.json`＋本体＋画像を梱包して配布でき、メニューからの導入・削除に加えて CLI もある（詳細は [docs/PACK_FORMAT_v0.md](PACK_FORMAT_v0.md) を参照）。

### 表示フィルタ（カメラ行→「表示フィルタ」＋「調整...」）

- メイン画面の表示だけに効くフィルタであり、認識・保存には影響しない。OBS の色補正に相当する調整（ガンマ・コントラスト・輝度・彩度・色相シフト）と HSV 色抽出（対象外グレー／白黒マスク）を備える。設定は ini へ保存される一方、ON/OFF は起動ごとに OFF になる。画像認識は常にフィルタのかかっていない生画像で行われる。

### WakeSetupの拡充（Switch2のwake設定＋リプレイ）

- 疎通（P）・無線 ON/OFF（W）・表示切替（D/M）・破棄（X）・鍵削除（K）の各ボタンと USB 状態表示を備える。

### 並列起動・入力ログ・キーコンフィグ

- ランチャー／`--profile` による複数台の並列起動に対応した（設定・ロックを分離する）。
- ボタン入力のログ表示およびキー割り当ての変更画面を備える。

### コマンド選択まわりの刷新

- 使用履歴、タグ編集、検索パレット（`Ctrl+K`）、実行中の一時停止／停止制御を備える。

### Pico連携（別repo pico-wakeConがファーム側）

- 有線（USB シリアル変換器）・無線（Pico の USB 直結＋Bluetooth）の 2 経路に対応する。通信方式は登録制（`legacy_text`／`pico_uart`）であり、入力調停（`human` / `script` / `off`）を備える。

## 改善

- 同型キャプチャボードの識別と、カメラ無効（Disable）の選択に対応した。

## 互換性・移行注意

- Python 3.12 以降が必要である（`3.12.10` 固定）。`requirements.txt` は廃止し、`uv` 管理に一本化した。
- 自作スクリプト（`Commands.*`）と `settings.ini` はそのまま使える。新設セクション（`[Transport]`・`[Arbitration]`・`[Audio]`・`[PreviewFilter]` 等）は起動時に自動補完され、移動したモジュールには互換 shim がある。
- Blockly の旧保存物を開き直すと、テンプレ照合がカラー（`use_gray=False`）で生成される（保存済み `.py` は再保存まで不変である）。
- `video_capture_wrapper.py` は [`pokecon/domain/Camera.py`](../SerialController/pokecon/domain/Camera.py) に統合した（使い方は同じである）。
- LINE 通知は削除した（LINE Notify サービス終了のため）。通知には Discord 連携を使用する。

## 開発基盤（開発者向け）

- `task` ランナー・`ruff`・`mypy`・単体テストを整備した（`task ci` で一括実行できる）。CI も備えている。
- **層分け**: [`pokecon/domain/`](../SerialController/pokecon/domain)（GUI 非依存の純粋関数）・[`pokecon/runtime/`](../SerialController/pokecon/runtime)（手順）・[`pokecon/ui/`](../SerialController/pokecon/ui)（画面）に分割し、[`Window.py`](../SerialController/Window.py) は組立専用とした。層の境界は自動検査（`task bounds`）で確認する。
- **公開 API**: 利用者スクリプト向け公開 API（`Commands.*`）は凍結し、自動検査（`task userapi`）を行う（詳細は [docs/ARCHITECTURE.md](ARCHITECTURE.md)・[AGENTS.md](../AGENTS.md) を参照）。
