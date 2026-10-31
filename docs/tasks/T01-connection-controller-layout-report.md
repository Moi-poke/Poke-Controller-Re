# T01 完了報告: シリアル・BCON・コントローラタブのレイアウト改善

## 結論

3 タブを「状態 → 主要操作 → 詳細（折りたたみ 1 段）」の順へ揃えた。
狭い可視幅（約 341〜362px）で主要操作が欠けず、横スクロールを要求しない。
BCON は可視幅 560px 以上で左右 2 列、未満で 1 列。枠線と大きな余白を減らし、
背景色は 1 色に揃えた。送信先と手動入力は下端の状態バーに全タブ共通で出し、
縦に長い欄はマウスホイールでスクロールできる。ジャイロの既定は HID 直結。
画面の見た目は probe と実機の実画面で確認した。実機では Pico 2 W（COM4）へ
BCON 1 Mbps で接続し、
`HELLO` 確認と `STATUS`（無線）受信、実 Pro Controller の HID 検出、
キャプチャボードの映像まで確認した。追加でカメラ・オーディオ・コマンド・
履歴の 4 タブも、同じ「枠線を減らす・可視幅へ合わせる・状態は状態バー」の
方針で直した（表の列は幅に応じて隠し、空状態は理由と次の行動を文章で出す）。
ペアリング・ジャイロ送出・入力の実送信・LED/振動は未確認である。

## 調査（imozuru）

`~/research/desktop-settings-layout/` に原文・map・log を保存した
（`map.md` / `log.md` / `sources/` 22 本）。主な根拠と採用した規則:

| 採用した規則 | 原文 |
|---|---|
| 1 列・上から下・セクション見出し、設定は 4〜5 個まで | Microsoft「Guidelines for app settings」`sources/ms-2026-guidelines-app-settings.md:82,124,136` |
| 詳細は 1 階層の展開（入れ子は 1 段まで） | 同 `:94`、NN/g「Progressive Disclosure」`sources/nng-progressive-disclosure.md` |
| ラベルは入力欄の上（視線移動 50ms。左揃えは 500ms） | Penzo 2006 `sources/penzo-2006-label-placement.md:53,73`、Bargas-Avila 2010 `sources/bargas-avila-2010-20-guidelines.txt:238`、デジタル庁 `sources/dds-input-text.md` |
| 狭い幅の切替は 640px 前後を目安に、可視幅で決める | Microsoft breakpoints `sources/ms-2026-breakpoints.md:42,44`、GNOME `sources/gnome-adaptive.md:21` |
| 状態は常時・文言で（色だけに頼らない） | NN/g `sources/nng-visibility-status.md`、SmartHR `sources/smarthr-status-label.md` |
| グループ化は余白と見出し、入れ子を深くしない | SmartHR `sources/smarthr-visual-grouping.md` |
| 狭い幅では右から崩れる → 重要情報を上・左へ | 池田拓司 `sources/ikeda-2018-unsung-10.md` |

## タブごとの変更内容と理由

### シリアル（`SerialController/ui/serial_panel.py`）

- 外枠の見出し付き箱をやめ、枠線のない 1 列にした（背景の色分けと箱の
  入れ子を減らす）。接続状態はタブ内に常時表示せず、下端の状態バーへ出した。
  タブには通信方式・ポート・「接続」「切断」を置き、失敗理由・選択と実際の
  差異・ポート未検出はその近くに出して、空のときは行ごと隠す
  （`_build_serial_frame` :157、`_render_connection_notice` :1490 付近）。
- 通信方式とポートは、可視幅 470px 以上で左右、未満で縦に並べる。
  縦を詰めつつ、狭い幅で入力欄を潰さない（`_reflow_serial_setup` :413）。
- 通信方式の表示名を「マイコン直結（USB シリアル）」「Pico UART（旧方式）」
  「Pico 中継（USB）」「Pico 中継（別プロセス）」にした。内部キーは保存・
  比較に使い、未知の方式はキーをそのまま候補へ残す（`ui/transport_names.py`）。
- 接続結果は「接続」ボタンの直下（`connection_result`、空なら非表示）。
  速度・説明・送信内容は「通信の詳細設定」「診断・送信ログ」へ折りたたみ。
  失敗時だけ基本表示に「速度を探す」を出す（`_refresh_connection_controls`
  :1419）。通信速度は「次回」と「接続中」を分けて表示（`_render_baud_summary`
  :1000 付近）。
- 入力調停をコントローラタブへ移動（同じ設定変数・候補・適用処理を使用）。
- LED・振動の詳細表示を削除し、BCON タブへ集約。Switch 側の状態は
  下端の状態バー（Pico↔Switch）に出す。
- 幅は Notebook の内側ではなく、スクロール欄で見えている幅へ合わせる
  （`_fit_serial_viewport` :364）。折り返しは枠の幅から決め、幅→折り返し→
  要求幅の循環が起きないよう上限と実寸の扱いを固定した。

### BCON（`SerialController/BconSetup.py`, `ui/bcon_panel.py`）

- 状態（PC ↔ Pico / Pico ↔ Switch / 現在のモード・種別）はタブ内に常時
  表示せず、下端の状態バーへ出した（PC ↔ Pico は送信先、Pico ↔ Switch は
  別項目）。取得できない種別は「不明」と明示する。
- 「操作」「コントローラ状態」は枠線なしの見出し（`SectionFrame`）にし、
  ペアリング・色・診断・保守は枠線なしの折りたたみ（`CollapsibleSection`）に
  した。箱の入れ子を作らず、余白と見出しでまとまりを示す。初回は閉じる。
- 主要操作は「状態を更新」「Switchへ再接続」。PC 側接続のやり直し（HELLO）と
  Switch 側再接続を文言で区別した。接続失敗時はその下に「HELLO をやり直す」
  「速度を探す」も出す（`_recovery_row` :445）。
- 結果は主要操作の直下の 1 行にも出し、ログは追跡用としてスクロールさせない
  （`_append` :1080）。
- モード変更の影響（再起動・COM ポートが外れる）を操作前に表示し、
  「シリアルタブで接続し直す」を置いた（`ui/bcon_panel.py:107`）。
  自動再接続は追加していない。
- 幅 560px 以上で左右 2 列、未満で 1 列（`_TWO_COLUMN_MIN_WIDTH` :46、
  `_reflow_columns` :868）。判定は中身の要求幅ではなく、実際に見えている幅。
  閉じた詳細の空箱は出さない。
- タブの最低高さを 280px にした。内容が短いときにタブ欄が数行まで縮み、
  操作できなくなるのを防ぐ。

### コントローラ（`SerialController/ui/serial_panel.py`, `ImuPanel.py`）

- **送信先と手動入力の状態は、タブ内ではなくアプリ下端の状態バーへ移した。**
  標準レイアウトでは、どのタブを開いていても
  「送信先: COM4 / Pico 中継（USB）」「手動入力: 受付中（キーボード）」が
  1 行で見える（`ui/layout_panel.py:135`、`_update_controller_status` :814）。
  タブ内に重複して置かず、状態を 1 か所で見せる。
- 外枠の箱をやめ、キーボード → 操作方法・キー割当 → ゲームパッド →
  パッド名・再接続・読み取り状態 → 詳細（マウス、パッド調整、手動入力）の順。
- キーボードは「キー割当を確認」（既存のキーコンフィグ画面を開く。
  `_open_key_config` :757）だけを置いた。長い操作方法の説明は削り、キーの
  意味はキー割当画面で読めるようにした。新しい編集機能は追加していない。
- 操作部品をクリックした後はフォーカスをタブへ戻す（`_bind_release_focus`
  :850 付近）。ボタンやコンボにフォーカスが残ると矢印キーが二重に効くため。
  文字を打つ Entry / Text / Spinbox はそのまま。
- ゲームパッドはパッド名の幅を確保し、読み取り状態を近くに置いた。
  HidHide 未設定時は入口の直下に理由とセットアップを出す。
  「遊び」は「スティックの遊び（0〜8192）」として詳細へ移した（生値・
  保存値のまま。割合表示は単位換算を確認できないため採用しない）。
- マウス操作・コマンド実行中の手動入力（入力調停）・ゲームパッドの調整を
  折りたたみへ。入力調停の候補は既存の許可値から引き、意味を文章で添えた
  （`_render_arbitration_hint` :1306）。
- 仮想コントローラは設定より先に場所を確保し、見えている幅・高さへ収める
  （`_fit_control_viewport` :738）。別窓化・復帰・押しっぱなし解除は既存の
  `ControllerDock` をそのまま使う。
- ジャイロは「状態」と「開始/停止」を常時、入力源などは「ジャイロ設定」へ。
  入力源の既定は **HID 直結**（`config.py` の IMU.source = `hid`）。
  既存の設定ファイルに保存済みの値は上書きしない。HidHide 未設定時は
  安全のため SDL センサーへ寄せる既存の判定を残した。
- タブの最低高さを 280px にした。

### 共通部品

- `ui/collapsible.py`（新規）: `CollapsibleSection` は枠線なしの
  Toolbutton 見出し＋中身。畳んでもウィジェットを破棄せず値を保つ。閉じている
  間は中身の操作部品を Tab 順から外す（`_apply_focus` :147）。`SectionFrame`
  は枠線なしの見出し付きまとまり。
- `ui/scroll_host.py`: **マウスホイールで縦スクロール**する。ポインタがその欄の
  上にあるときだけ動かし、Text / Listbox / Treeview では邪魔をしない
  （`_on_mousewheel` :83）。設定タブ・ログなど同じ入れ物を使う欄すべてに効く。
- `ui/transport_names.py`（新規）: 通信方式の内部キーと表示名の対応を 1 か所に
  集約（シリアルタブと状態バーで同じ名前を使う）。
- `ui/layout_panel.py` / `Menubar.py` / `Settings.py`: 下端の状態バーに
  「実行状態 / 送信先 / 手動入力 / Pico↔Switch / カメラ / 表示 fps」を
  選んで常時表示できるようにした。表示(V) → 常時表示する項目で切り替え、
  `[Window] status_items` に保存する。

### 追加: カメラ・オーディオ・コマンド・履歴

依頼（ベストプラクティスに合わせて他のタブも）を受けて、同じ方針で直した。
調査は imozuru で `~/research/desktop-list-and-audio-ui/` に保存（13 本）。

- 共通: `ui/tab_fit.py`（新規）。Notebook の内側は全タブ共通の幅で、
  可視幅より広いことがある。タブ内容を place して可視幅へ合わせ、右端の
  ボタンや表の列が見切れる問題を解消した。高さも中身／可視高さに合わせる。
- カメラ: 外枠をやめ、「使用するカメラ」の下に選択欄、再読み込みは見出し行、
  フィルタは「使う」と調整を近接。狭い幅で再読み込みが切れなくなった。
- オーディオ: 入出力をラベルの下に全幅で置き、再読み込みは入力の見出し行、
  モニターは出力の直下、音量はスライダー＋数値、レベルは数値と遅延を文字で
  表示（色だけに頼らない。OBS の技術資料 / WCAG 1.4.1）。
- コマンド: 外枠をやめ、検索を上段、タグとフォルダを次段、主操作
  （再読み込み・開始・一時停止）を 3 個に保った（320px で主コマンド 4 個まで
  という Microsoft の目安内）。検索欄は Enter / Esc で抜ける既存動作を維持。
- 履歴: 外枠をやめ、表の幅に応じて補助列（種別・結果・所要時間）を隠す。
  所要時間は右揃え、日時は左揃え。空状態は「実行履歴はまだありません。
  コマンドタブで実行すると残ります。」のように理由と次の行動を文章で出す。
  並べ替えの▼▲ はヘッダーに残した。操作ボタンは主要 3 個と削除系 2 個の
  2 段に分け、狭い幅でも切れないようにした。

根拠: `sources/ms-list-views.md:302,208,188`、
`sources/gnome-list-column-views.md:23,43,45`、
`sources/nng-empty-state.md:22`、`sources/ms-command-bar.md:60,115`、
`sources/obs-audio-mixer-technical.md:49,89,91`、`sources/w3c-meter.md:31-35`。

## 変更ファイルと主要箇所

| ファイル | 主要箇所 |
|---|---|
| `SerialController/ui/serial_panel.py` | `_build_serial_frame`:157, `_fit_serial_viewport`:364, `_reflow_serial_setup`:413, `_build_control_frame`:445, `_fit_control_viewport`:738, `_open_key_config`:757, `_update_controller_status`:814, `_transport_display`:1226, `_render_arbitration_hint`:1306, `_refresh_connection_controls`:1419, `_render_connection_summary`:1583 |
| `SerialController/BconSetup.py` | `_TWO_COLUMN_MIN_WIDTH`/`_TAB_MIN_HEIGHT`:46, `操作`/`_recovery_row`:428-445, 折りたたみ:469/623/677/736, `_available_width`:838, `_reflow_columns`:868, `_render_summary`:1316 |
| `SerialController/ui/bcon_panel.py` | `viewport` 渡し:78, `_show_serial_for_recovery`:107 |
| `SerialController/ui/collapsible.py` | 新規。`SectionFrame`:44, `CollapsibleSection`:62, `_apply_focus`:147 |
| `SerialController/ui/scroll_host.py` | `_on_mousewheel`:83（ホイールで縦スクロール） |
| `SerialController/ui/transport_names.py` | 新規。通信方式の表示名 |
| `SerialController/ui/tab_fit.py` | 新規。タブ内容を可視幅・高さへ合わせる |
| `SerialController/ui/camera_panel.py` | `カメラ` タブの枠線なし 1 列:220-270 |
| `SerialController/ui/audio_panel.py` | `オーディオ` タブの枠線なし 1 列:75-170 |
| `SerialController/ui/command_panel.py` | 検索/絞り込み/主操作の再配置:110-185 |
| `SerialController/ui/history_panel.py` | 表の列フィット・空状態・操作 2 段:100-210 |
| `tests/test_layout_panel_contract.py` | 状態バー項目・送信先表示 |
| `tests/test_status_view.py` | Pico↔Switch の状態バー表示 |
| `SerialController/ui/layout_panel.py` | `STATUS_ITEMS`:38, 状態バー:135-170, `_sync_status_items`/`_repack_status_items`:180-230, `_publish_status`:330 |
| `SerialController/Menubar.py` | 表示(V) → 常時表示する項目:170-187 |
| `SerialController/Settings.py` | `status_items`:120, 保存:390 |
| `SerialController/ImuPanel.py` | 状態・開始/停止の常時表示:128-137, `ジャイロ設定`:139, 既定 HID:270 付近 |
| `SerialController/config.py` | IMU.source の既定 `hid`:250-254 |
| `docs/BCON_TRANSPORT.md` | 表示名:21-22, シリアルタブ:38-47, BCON レイアウト:89-97, 操作一覧:113-148 |
| `tests/bcon_ui_support.py` | 状態 3 行・結果のスタブ |
| `tests/test_bcon_connection_views.py` | LED の所有を BCON へ、状態 3 行へ追従 |
| `tests/test_bcon_counters.py` | シリアル要約の検査を新しい契約へ |
| `tests/test_bcon_layout_contract.py` | 折りたたみ初期閉・値保持・復旧導線・状態グループ |
| `tests/test_bcon_rumble_ui.py` | 鮮度表示を `_switch_state` へ |
| `tests/test_setting_tabs_width_contract.py` | 状態→操作→詳細の順、詳細の値保持、折り返しの振動防止 |
| `tests/test_ui_no_block_on_rpc.py` | LED は BCON 側で確認 |
| `tests/test_imu_panel.py` | 設定を開いてから表示を確認 |
| `tests/test_collapsible_section.py` | 新規。折りたたみの契約 |
| `tests/test_serial_controller_status.py` | 新規。手動入力の受付状態・調停の説明 |
| `tests/test_scroll_host_wheel.py` | 新規。ホイールのスクロール契約 |
| `docs/tasks/T01-connection-controller-layout-report.md` | 本報告 |

## 画面確認

- 撮影: `.tmp/gui-probe/20261031-145431/`（変更後・最終）、
  `.tmp/gui-probe/before-20261031-134837/`（基準コミット）。
- DPI aware・表示倍率 125%（`winfo_fpixels("1i")` = 120 / 96）。
- レイアウト確認の probe は実ポート・カメラ・実機を掴まない
  （`WindowUtils.listComPorts = []`、`camera_id = 99`、
  `is_show_realtime = False`、専用 `settings.probe.ini`）。
  実機の確認は別プロファイル `t01real` を使った下記の実機確認で行った。
- 記録したタブ可視幅（Canvas 実寸）:

| 状態 | シリアル | BCON | コントローラ |
|---|---|---|---|
| 狭い（目標 360） | 362px | 362px | 341px |
| 広い（目標 640） | 642px | 642px | 621px |

- 画像:
  - `serial-tab360.png` / `serial-tab640.png` / `serial-tab360-open.png`
  - `bcon-tab360.png` / `bcon-tab640.png` / `bcon-tab360-open.png`
  - `controller-tab360.png` / `controller-tab640.png` /
    `controller-tab360-open.png` / `controller-bottom-640.png`
- 確認した状態: 未接続（ポートなし・BCON 選択）。接続処理中／コマンド実行中
  は疑似状態を `_apply_connection_snapshot` へ流して自動試験で確認。
  接続済みは後述の実機で確認した。
- 折りたたみ: 初期は閉、開いても値・選択・ログが残る（自動試験と画像）。
  BCON は 642px で 2 列、362px で 1 列。狭い側の要求幅は残らない。
- Tab 順（`tk_focusNext`）: シリアルは通信方式 → 一覧更新 → BCON設定へ →
  通信の詳細設定 → 診断・送信ログ（ポート未検出のため、ポート・接続・切断は
  無効でスキップ）。BCON は 接続モード → シリアルタブ導線 → 本体色 →
  通信診断 → 保守。コントローラは キー割当 → ゲームパッド → パッド →
  再接続 → マウス → パッド調整 → 手動入力 → 別ウィンドウ → ジャイロ。
  閉じた詳細の中へは移動しない。

## 実機確認（2026-10-31）

専用プロファイル `t01real`（最終確認のため起動中。アプリを閉じたあと削除可）で、
既存アプリ・既存設定を触らず、
使用中の実機を奪わずに接続した。確認した機器:

| 機器 | 確認内容 |
|---|---|
| Raspberry Pi Pico 2 W（VID_2E8A/PID_000A、COM4=データ / COM5=コンソール） | BCON 1,000,000bps で接続。`HELLO_ACK` 採用 4 / FW v0.12 / 結果 0 |
| Switch（Pico ↔ Switch） | `STATUS` flags 0xC3、方式 = 無線、受信 0.58 秒前 |
| Pro Controller（HID 直結） | コントローラタブのパッド一覧に「0: Pro Controller (HID直結)」 |
| Live Gamer EXTREME 3 | キャプチャボードとして開き、Switch の HOME 画面を表示 |

- シリアルタブ: 「接続済み：COM4 / Pico 中継（USB）」、Switch側「無線」、
  切断ボタン有効、ポートに実機名を表示。
- BCON タブ: PC ↔ Pico「接続済み（COM4 / 1000000bps）」、
  Pico ↔ Switch「無線 / STATUS: 1秒前」、現在のモード「無線 / 種別: 不明」、
  結果行に接続完了を表示。詳細は閉じたまま。
- コントローラタブ: パッド名を横幅いっぱいに表示。送信先・手動入力は
  タブ内ではなく下端の状態バーに「送信先: COM4 / Pico 中継（USB）」
  「手動入力: 停止（入力なし）」として常時表示。
- LED・振動は `PLAYER_INFO` / RUMBLE 未受信のため「なし／未受信」のまま。
  実機の点灯・振動は確認していない。
- 終了時に `inactivateSerial` で切断し、スナップショットが `disconnected` に
  なったことを確認。プロセス・ロックは残っていない。
- 画像（最終レイアウト・実機）: `.tmp/gui-probe/live-v5-serial.png`、
  `live-v5-serial2.png`（実機・起動中）。BCON/コントローラの最終レイアウトは
  `.tmp/gui-probe/20261031-153754/bcon-tab640.png`、
  `controller-tab360.png`。他のタブは `.tmp/gui-probe/20261031-164558/`
  の `camera-tab360.png`、`audio-tab360.png`、`command-tab360.png`、
  `history-tab360.png`。
  旧レイアウトでの実機画像は `.tmp/gui-probe/real-20261031-140257/`。
- 実機アプリは撮影後も起動したまま（タイトル
  `[t01real] COM4(接続済み) - Poke-Controller Re: v4.0.2`、Switch の HOME、
  プレビュー 60fps、下端に送信先/手動入力）。
- マウスホイール: `tests/test_scroll_host_wheel.py` で、ホイールが欄を
  スクロールし、Text の上では Text のスクロールを妨げないことを確認。
- 補足: 確認用ラッパーは 150 秒で timeout 終了したが、レポート保存と切断は
  その前に完了していた（ポート・ロックは解放済み）。カメラは最初の 2 枚が
  "No Image"、コントローラタブ撮影時に Switch の HOME 画面が出た（取込遅延）。
  音声は PyAudio 未導入のため使っていない。

## 実行した検証

- `uv run --frozen python -m py_compile SerialController/ui/serial_panel.py
  SerialController/ui/bcon_panel.py SerialController/ui/collapsible.py
  SerialController/ui/scroll_host.py SerialController/ui/transport_names.py
  SerialController/ui/layout_panel.py SerialController/BconSetup.py
  SerialController/ImuPanel.py SerialController/config.py` — OK
- 対象テスト（1 コマンドで実行）:
  `uv run --frozen pytest tests/test_bcon_connection_views.py
  tests/test_bcon_layout_contract.py tests/test_bcon_tab_contract.py
  tests/test_bcon_baud_ui.py tests/test_bcon_rumble_ui.py
  tests/test_controller_dock_contract.py tests/test_hidhide_gamepad_gate.py
  tests/test_gamepad_list_refresh.py tests/test_imu_panel.py
  tests/test_setting_tabs_width_contract.py tests/test_layout_panel_contract.py
  tests/test_pane_layout_contract.py tests/test_collapsible_section.py
  tests/test_serial_controller_status.py tests/test_scroll_host_wheel.py
  tests/test_ui_no_block_on_rpc.py -q`
  — 194 passed
- 他のタブの契約: `tests/test_camera_tab_contract.py`
  `tests/test_command_panel_layout_contract.py`（39 passed）と
  `tests/test_history_panel_contract.py`（14 passed）も成功。
- `task ci`（lint / format --check / mypy / bounds / userapi / docs / test）
  — 成功。ruff「All checks passed / 475 files already formatted」、
  mypy「no issues found in 475 source files」、bounds・userapi・docs OK、
  テスト「2690 passed, 28 skipped in 114.24s」。
  （skip は表示の無い環境など既存の条件付き skip。）
- 実機確認: `.venv/Scripts/python.exe .tmp/gui-probe/show_real.py`
  （プロファイル `t01real`）— 接続 `ready` / COM4 / 1,000,000bps /
  `HELLO_ACK` 4・v0.12・結果 0 / `STATUS` 0xC3（無線）/ 実 Pro Controller
  検出 / キャプチャ映像を確認。終了時 `disconnected`。レポートは
  `.tmp/gui-probe/real-20261031-140257/report.json`。
  （ラッパーは保存後に timeout 終了したが、切断・ロック解放は完了済み。）

## 未確認・残っている問題

- 実機で確認したのは、PC ↔ Pico の接続（HELLO）、`STATUS` の受信、
  Pro Controller の HID 検出、キャプチャ映像まで。**ペアリング、ジャイロ
  送出の開始、実際のボタン/スティック送信、LED・振動、種別（Pro Con/
  Joy-Con）は未確認**。種別は通信の写しに無いため画面は「不明」のまま。
- 接続処理中・コマンド実行中・失敗の表示は、確認済みスナップショットを
  流した自動試験で確認した（接続済みは実機でも確認）。
- タブ欄全体の横スクロールバーは、対象外の「履歴」タブの要求幅 677px が
  原因で、狭い可視幅では表示され続ける（既存）。対象 3 タブの内容は
  可視幅に収まり、操作に横スクロールを要求しない。履歴タブの変更は
  本指示の編集範囲外のため手を入れていない。
- 実画面のホバー・ツールチップ・フォーカス枠の見え方は画像で判断して
  いない（キーボード到達は `tk_focusNext` で確認）。

## 指示書から調整した点と根拠

- 通信方式の表示名を追加・改訂した。特に「BCON（USB 直結）」は、PC↔Pico と
  Pico↔Switch のどちらを指すか分からないため「Pico 中継（USB）」に変えた。
  内部キーは保存・比較に使い、未知の方式はキーをそのまま候補へ残す。
  `TRANSPORT_ALIASES` は従来どおり解決に使う。
- 送信先・手動入力の状態は、コントローラタブ内だけに置かず、アプリ下端の
  状態バーへ移した（全タブ共通・常時表示）。タブ内の重複表示は削った。
  状態は 1 か所で見せる、という一般的な状態表示の原則に合わせた。
- マウスホイールのスクロールは `ui/scroll_host.py` に入れた。編集範囲の
  指示書では `ui/layout_panel.py` までだったが、タブ欄のスクロールは
  `ScrollHost` が持つため、ここを直さないとホイールが効かない。
  対象 3 タブだけでなく、同じ入れ物を使うログ等にも効く。
- 折りたたみの見出しは、枠線付きボタンではなく枠線なしの Toolbutton にした。
  大きなボタン面が並ぶと背景色がまだらに見え、余白も増えるため。
- タブの最低高さを 280px にした。内容が短いとタブ欄が数行まで縮み、
  スクロールしても操作しづらくなるため。
- 常時表示する状態はタブ内に置かず、下端の状態バーへ集約した。どの項目を
  出すかは表示(V) → 常時表示する項目で選べるようにした（新しい永続設定
  `[Window] status_items`。指示書の「新しい永続設定を追加しない」から、
  利用者の明示的な要望により変更）。
- キーボード操作の説明文は削除した。キーの意味はキー割当画面で読める。
  操作部品をクリックした後にフォーカスをタブへ戻す処理を足し、ボタンや
  コンボにフォーカスが残って矢印キーが二重に効く状態を防いだ。
- ジャイロの入力源の既定を HID 直結に変えた（`config.py` の `IMU.source`）。
  既存の設定ファイルに保存済みの値は尊重し、上書きしない。HidHide が
  未設定のときは既存の安全判定で SDL センサーへ寄せる。
- シリアルの「更新」は「一覧更新」と表示した（接続のやり直しと区別する）。
- BCON の 2 列切替は中身の要求幅ではなく可視幅 560px の固定境界にした。
  折り返し後の要求幅で判定すると、広げても 1 列のまま戻れないため。
- 折りたたみの見出しは「記号＋全文」のボタンにした（色やツールチップに
  頼らず、閉じていることと対象が読める）。閉じた中身は Tab 順から外す。
- BCON の接続失敗時は、診断を自動で開かず、基本表示に復旧ボタンを出した
  （初回表示で診断を閉じる規則と、失敗時に導線を出す規則の両立）。
- 本体色の 4 欄はタブでも 2×2 のまま（狭い列で横 1 列は最も広くなる）。
- コントローラの「遊び」は生値のまま「スティックの遊び（0〜8192）」と
  表記した。割合表示への換算は保存値・送信値の意味を変えるため見送った。
