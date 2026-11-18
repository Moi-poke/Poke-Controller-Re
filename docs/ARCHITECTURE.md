# ARCHITECTURE

Poke-Controller Re: の構成メモである。詳細な開発手順は [`AGENTS.md`](../AGENTS.md) を、自動化コマンドの作り方は本家 wiki を参照する。

## 層構造

```
ユーザースクリプト (Commands/PythonCommands/, Commands/McuCommands/)
        │  PythonCommand / ImageProcPythonCommand / AudioPythonCommand / McuCommand
        ▼
実行制御・操作・画像認識・音声検知 (Commands/PythonCommandBase.py ほか)
        │  press / hold / wait / isContainTemplate / waitTone / waitSound
        ▼
pokecon/domain/  …… GUI 非依存の純粋ロジック（tkinter・アプリ層の import 禁止）
        │  serial/: Sender 本体・live/posture・Arbiter（入力調停）・encoding（行書式）
        │  transport/: base（抽象）・text_serial・registry（名前から選択）・bcon*
        │  Keys: Button/Direction/Hat/Stick（行組み立ては encoding へ一本化）
        │  imu/: 3D コントローラ形状・SDF・可動グループ（numpy のみ）
        │  preview/win32_clock.py: Win32 高精度プレビュー時計・ワーカランタイム
        │  bcon_status.py / hidhide_policy.py: BCON 状態デコード / HidHide ポリシー判定
        │  Camera: 最新1枚だけを配る capture
        │  AudioCapture: 入力1本を所有し直近N秒を配る capture（音声版）
        │  audio_dsp / audio_latency: 検知・計測の純粋関数（FFT・相互相関・Tap時刻）
        │  InputLog / CommandLoader / Utility / CommandVision / CommandAudio ほか
        ▼
ファーム (Arduino Leonardo / Pico 2 W。Pico 2 WはSwitch-bconファーム、詳細は docs/BCON_TRANSPORT.md)
```

本体は [`SerialController/pokecon/`](../SerialController/pokecon) パッケージであり、`app/`（組立・設定・入口・`python_command_base.py`）・`domain/`（tkinter 非依存）・`runtime/`（手順）・`ui/`（Tk 部品）・`plugins/`（transport の manifest 読み込み）に分かれる。`SerialController/` は実行時の根（ルート）であり、`settings*.ini`・`Template/`・`db/`・`Commands/` を配置する。エントリは [`pokecon/__main__.py`](../SerialController/pokecon/__main__.py)（`python -m pokecon`）、[`SerialController/__main__.py`](../SerialController/__main__.py)（`python -m SerialController`）、[`Window.py`](../SerialController/Window.py) 直起動（[`launcher.py`](../SerialController/launcher.py) が使う従来経路）の 3 口で、いずれも `pokecon.app.Window.main()` へ集まる。

利用者スクリプトの公開面と `settings*.ini` の書式は凍結されており、その基準は公開済みの Poke-Controller Modified（`Moi-poke/Poke-Controller-Modified` の `master`）にあったかどうかである。公開面のチェックは [`pokecon/domain/user_api_allowlist.py`](../SerialController/pokecon/domain/user_api_allowlist.py) を正本とし、`Commands.CommandBase` / `Keys` / `McuCommandBase` / `PythonCommandBase` / `Sender` / `StickCommand` / `UnitCommand` の 7 経路（名前一覧は [`pokecon/domain/api_surface.py`](../SerialController/pokecon/domain/api_surface.py)、`task api` が欠落を落とす）が対象となる。かつて存在した暫定名（`WakeLink` 等）は廃止・`Commands/PythonCommandBase.py` へ集約され、`PROVISIONAL_COMMANDS_SUBS` は空になっている。

区分の検査（[`tools/gates/check_user_api.py`](../tools/gates/check_user_api.py)）では違反、保存時（[`pokecon/domain/blockly_validate.py`](../SerialController/pokecon/domain/blockly_validate.py)）では異常、配布時（[`pokecon/domain/pack_zip.py`](../SerialController/pokecon/domain/pack_zip.py)）では未知を注意に留めるという意図的な使い分けをしている。書式知識の実体は [`config.py`](../SerialController/pokecon/domain/config.py)（Tk なし）にあり、`Settings.GuiSettings` は Tk との鏡と入出力の手順だけを持つ。

GUI 層（[`Window.py`](../SerialController/Window.py)・[`GuiAssets.py`](../SerialController/pokecon/ui/GuiAssets.py)・[`Settings.py`](../SerialController/pokecon/app/Settings.py) ほか）は [`pokecon/domain/`](../SerialController/pokecon/domain) の利用者である。[`Window.py`](../SerialController/Window.py) は起動の組立・設定の出し入れ・終了処理だけを持ち、画面の部品ごとの手順は [`pokecon/ui/`](../SerialController/pokecon/ui) の Mixin（`camera_panel` / `serial_panel` / `controller_panel` / `command_panel` / `history_panel` / `log_panel` / `audio_panel` / `bcon_panel` / `layout_panel`）と専門パッケージ（`bcon/`・`blockly/`）に分かれる。`history_panel` は実行履歴の一覧・再実行のタブを持ち、`bcon_panel` は通信方式が bcon 系のときだけ「シリアル」の隣へ Bcon タブ（中身は `BconSetup`）を出し入れする。Mixin では mypy のために `self` 越しに触る属性を宣言しておく。循環参照になるため、[`pokecon/ui/`](../SerialController/pokecon/ui) から `Window` 本体の import は禁止されている。

実行の手順は [`pokecon/runtime/`](../SerialController/pokecon/runtime)（`command_runner.py`: 起動・停止・見張り・後始末の状態機械、`serial_service.py`: Sender の所有・接続・切替管理、`manual_input_service.py`: 手動入力の寿命・調停連動、`imu_service.py`: IMU 入力源構築・変換・記録、`audio_service.py`: 取込口の所有・切替・モニター再生の手順、`blockly/`: オーサリング／部分編集／検証）へ委ねる。[`pokecon/runtime/`](../SerialController/pokecon/runtime) は tkinter を import しない。逆向きの依存は [`tools/gates/check_core.py`](../tools/gates/check_core.py)（`task bounds`）で禁止している。

## プレビュー（映像）の描画

取得と描画は別の契約であり、間に「最新1枚」しか無い（キューを持たないため遅延が積み上がらない）。

```
CameraBackend (pokecon/domain/camera_backend.py)      … core.Camera / core.CameraQueue
        │  readFrameWithSeq()（seq は上書きごとに進む）
        ▼
VideoRenderer (pokecon/domain/preview_renderer.py)    … アプリが知る唯一の描画面
        ├─ pokecon/domain/sdl_surface.py    GPU（SDL_Renderer: Windows=D3D11 / Linux=OpenGL）
        ├─ pokecon/domain/gdi_surface.py    Windows の CPU 描画（BitBlt）
        └─ pokecon/ui/photo_surface.py    Canvas + PhotoImage（最終手段）
```

描画方式は `settings.ini` の `renderer`（`auto` / `sdl` / `gdi` / `photo`）で選ぶ。`auto` は SDL を第一候補にし、組み立てられない、または接続できない場合は GDI → PhotoImage へ 1 段ずつ落ちる（起動は止めない）。映像経路に PIL / `ImageTk` / DIB は入らない（PIL を使うのはバッジの文字を焼くときだけで、文字が変わった瞬間に 1 回のみ実行する）。GPU 面は映像の解像度にテクスチャを追従させるため、1080p のカメラでもそのまま通る。

設計・実測・zero-copy の拡張点については [`superpowers/specs/2026-10-30-gpu-preview-renderer-design.md`](superpowers/specs/2026-10-30-gpu-preview-renderer-design.md) を参照する。

## pokecon/domain/ への移し方

- 同名ファイルで [`pokecon/domain/`](../SerialController/pokecon/domain) へ移動し、旧位置には再公開シムだけを残す（`from core.X import Y as Y` 形式）。`from Commands.X import ...` はそのまま動き、新規コードは `core` から読み込む。
- [`pokecon/domain/`](../SerialController/pokecon/domain) 内の相互参照は `core.*` の絶対 import にし、相対 import は禁止する。

## 送信の経路（2系統）

- **legacy（Leonardo 系）**: `S` に相当する数字行を送りっぱなしにする。
- **Pico live**: 姿勢スナップショットを 8ms スロットで取り出し、無変化の再送は 24ms に間引く（Pico の UART 10ms ポーリング＋32B FIFO に合わせ、8ms 連送のオーバーランを避けるため）。全状態の最低保持は既定 16ms である（`Transport.live_min_dwell_ms` で 8〜64ms 可変。8ms スロット量子化で実効 16ms × 2 状態 = 32ms となり、40ms 間隔の連打を滞留なく通せる上限となる）。未送出の中立は異状態への遷移で落とす（レガシー同等）。200ms の watchdog は 24ms 再送で十分賄う。

## 仮想コントローラの操作盤

- **レイアウト定義と描画**: ボタンの並び・当たり判定・論理サイズは [`pokecon/domain/pad_layout.py`](../SerialController/pokecon/domain/pad_layout.py) が持ち（`procon` / `joycon_l` / `joycon_r`）、描画は [`pokecon/ui/controller_pad.py`](../SerialController/pokecon/ui/controller_pad.py) が倍率を掛けるだけである。
- **操作盤の選択と連動**: コントローラタブの「操作盤」で `auto` / プロコン / Joy-Con (L) / Joy-Con (R) を選ぶ。`auto` は選んでいるゲームパッドの表示名（例: `Joy-Con (L)`）から左右を決め、判別できなければプロコンにする。BCON の種別切替が成功したときは、その種別（ProCon / Joy-Con L / R）へ明示的に合わせる。設定は `[General Setting] controller_layout` である。
- **切替時の解放処理**: 種類を変えるときは押しっぱなしをすべて解除してから描き直す（消えたボタンの解放が届かず Switch 側に残るのを防ぐため）。埋め込みと別窓は同じ種類を共有する。

## コマンドの発見と実行

`CommandLoader` が `PythonCommands/`・`McuCommands/` 配下を import し、`NAME` を持つ基底クラスの派生だけを拾う。1 ファイルの import が失敗しても他は巻き込まない。`reload()` で実行中に再読込できるため、コマンドファイルにモジュールレベルの副作用は置かない。

## 実行時エラーの回復

利用者のスクリプトは任意の Python であるため、例外が出た位置からフレームを再開することはできない。復帰できるのは「同じ操作をもう一度呼べる」場所だけである。そこで [`pokecon/domain/recovery.py`](../SerialController/pokecon/domain/recovery.py) が例外を型で分類し、2 層で回復する。

- **操作単位（画像テンプレートの読み込み・カメラのフレーム取得）**: 同じ読み込みをやり直す。カメラの一時切断は短い間隔で数回自動再試行し、それでも駄目なら「再試行 / 中止」を尋ねる。テンプレート欠如は人が画像を置くまで直らないため、すぐ尋ねる。
- **時間切れとの区別**: 画面に目的の画像が出ないこと（`waitTemplate` の時間切れ）は例外ではなく `False` の戻り値であり、スクリプト側の分岐で扱う。ここで回復するのは「テンプレートファイルが無い・読めない」場合である。
- **コマンド単位（上の層で拾えなかった回復可能エラー）**: 人が直したあと、コマンドを最初からやり直すか尋ねる。押しっぱなしを避けるため、尋ねる前に自分の入力は離す。再実行は最初からとなるため、すでに行った操作が繰り返されることをダイアログに明示する。

回復可能とみなすのは、`RecoverableError`（`TemplateImageError` / `CameraUnavailableError`）、pyserial の `SerialException`、`ConnectionError`、`TimeoutError` のみである。構文エラー・`NameError`・`TypeError` などのプログラマ誤りは回復不能として従来どおり即停止し、traceback を残す。自動再試行は冪等な読み取りに限り、ボタン送信のような副作用のある操作は自動では繰り返さない。

利用者スクリプトから回復可能なエラーを投げたいときは `from Commands.PythonCommandBase import RecoverableError` を使い、`hint` に「何を直せばよいか」を書く（GUI の回復ダイアログに出る）。

## 音声の取込の考え方

映像の `Camera`（最新 1 枚）に対し、音声は区間が要るため、`AudioCapture` が入力 1 本を所有して直近 N 秒のリングバッファへ書き込む。検知（`AudioMixin`）もモニター再生もこの 1 本から読み取る。デバイスをコマンドごとに開き直すと排他で競合するため、開閉の所有はここへ寄せる。

内部レートは 44.1kHz mono に統一し、デバイス自体のレート（48kHz 等）との差はリサンプルで吸収する。録音テンプレートは `Template/audio/<pack>/*.wav`、録音クリップは `AudioClips/` に置く。設定は `[Audio]` で管理する（[`config.py`](../SerialController/pokecon/domain/config.py) の既定＋補正に乗せる）。利用者向けの操作手順は [`docs/AUDIO.md`](AUDIO.md) を参照する。

## 設定と並列起動

`Settings.GuiSettings` が `settings[.profile].ini` を読み書きする。[`launcher.py`](../SerialController/launcher.py) がプロファイルを選んで別プロセスで [`Window.py`](../SerialController/Window.py) を起動し、`pokecon[.profile].lock` で二重起動を防ぐ。

## ログ

`loguru` でファイル（`log/`）と標準出力へ出力する。GUI のログ欄はキュー経由で描画し、ワーカースレッドから widget を触らない。ERROR 以上は GUI のログ欄にも流し（`LogPane.logger_sink`）、`print` と二重になる箇所は `logger.bind(gui=False)` でファイルだけに残す。ログ欄の形と各部の役割は [LOG_VIEW.md](LOG_VIEW.md) を参照する。

## 検証の考え方

`task ci`（ruff check / format --check / mypy / bounds / userapi / api / docs / test）が緑であることを確認する。実機なしで回せる純粋ロジックは [`tests/`](../tests) の pytest で確認する（例: Transport の間引き、`CommandRunner` の状態遷移、未知 capability の legacy 等価動作）。COM ポート・キャプチャボードが必要な動作は実機でのみ確認できる。

## ワーカーと GUI の受け渡し

ワーカースレッドから Tk の widget・Tk 変数・`after()` を触らない。渡したい処理は [pokecon/domain/ui_inbox.py](../SerialController/pokecon/domain/ui_inbox.py) の `UiInbox` へ積み、GUI スレッドが 16ms 周期（`Window._pump_inbox`）で取り出す。

- **必達と最新更新**: 必達（`post`）は閉じるまで届ける。表示の更新（`post_latest`）は同じ key の最新 1 件だけを保持する。
- **終了処理**: 終了時は `close()` を呼ぶ。残った必達は実行せず、`on_drop`（開き途中のカメラを閉じる等）だけをその場で走らせる。
- **CaptureAreaProxy**: `GuiAssets.CaptureAreaProxy` もこの箱へ積む（`.widget` は非常口であり、取り出した先の操作は呼び出し側の責任とする）。
