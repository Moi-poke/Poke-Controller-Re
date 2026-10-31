# IMU（ジャイロ）経路

`AGENTS.md` から分離したサブシステム固有の注意。IMU・HID・ジャイロ・0x30/0x40 に触るときだけ読む。

- PC→Pico は `T_IMU_SAMPLE`(0x04/LEN12, 6×int16-LE = ax,ay,az,gx,gy,gz) を5ms間隔。PCの0x30は1レポートに5ms間隔の3サンプルを持つので、リーダーは3スロットを順に供給し（`read_next()`）、FWが直近3件を保持して0x30のスロット0..2へ古い順に載せる。**0x30の送出周期は15ms固定**（これより速いとコンソールがジャイロを過積分する。`jyapayne/switch-pico` の実装註記）。
- **0x40の引数はIMU形式の選択**（実測: FW 4.51→`02`=圧縮クォータニオン、3.72→`01`=RAW）。FWは引数を`imu_mode`として保持し、1ならRAW 36B、2なら NS-LIB-HID `ns_mode_2_s` 同形の36B（accel y,x,z＋最大成分基準の30bitクォータニオン＋11bit ms）を返す。`3`は未解明として仮定しない。**現行の実機検証は 3.72＋RAW**（Mario Tennis / Splatoonで確認）。4.51を名乗る場合はmode2のクォータニオン経路が必須。
- 有線0x30のIMUは既定でrole0のみ（`usb_imu_wired_role_ok`）。role1/2で有線ジャイロを試す診断は `JOY_WIRED_IMU_ALL=1`。FW版のA/Bは `JOY_USB_FW_JOY_OLD=1`（Joy役を3.72に）。
- role2の軸写像はFW側で固定（`k_joy_negate_axis`: 加速度・ジャイロともY,Z反転）。SPI校正値の回転だけ `JOY_SPI_CAL_ROT`（0=なし/1=YZ/4=XZ）で切り分けられる。PC側 `[IMU] accel_*/gyro_axes・signs` は恒等が前提（HID直結でも適用される）。
- `[IMU] gyro_dead_dps` は送出直前のジャイロ各軸に効く（`core.imu_wave.apply_gyro_deadband`）。`gyro_scale` は実測用トリム。**加工後の値を返さないと不感帯が送出に効かない**（過去に実際に踏んだ）。
- HIDリーダー (`core.nintendo_hid`) はプロセス共有・読み取り専用スレッドが最新0x30を保持する。`ImuPanel` は停止時に共有ハンドルを閉じない。読取失敗が続いたら自分で閉じ、パッド/IMU側が再オープンする（UF2書き込みでUSBが再列挙されるため必須）。
- IMUパネルの入力源は通常 **「パッドAPI」（SDL）／「HID直結」** の2つ。`sine`/`step`/`rest`/`manual`、反転・感度・詳細表示は**デバッグ**時だけ出す。ボタンは「開始/停止」。

## ジャイロの記録と再生（.pgyro）

実コントローラーの動きを記録し、後から同じ動きを Switch へ送り直せる。UI は
IMUパネル（`SerialController/ImuPanel.py`）で、記録器・再生器の実体は
Tk 非依存の [`core/motion_track.py`](../SerialController/core/motion_track.py)。

- **記録**: 入力源（パッドAPI / HID直結）で送出中に「送出値を記録する」を
  有効にすると、停止までのあいだ **Pico へ送る加工後の6値（ax,ay,az,gx,gy,gz）**
  を `[IMU] record_path`（既定 `records/motion.pgyro`）へ書く。保存先は
  「参照…」で選べる。記録の有無は開始時に読み、送出中の切替は次回から。
  送出ループが抑え中（HELLO未確立・hold）でサンプルを読まない刻みは記録にも
  残らない。書き込み失敗は黙って捨て、停止時に件数をログへ出す。
- **再生**: 入力源で「記録の再生」を選び、再生ファイルを指定して「開始」。
  記録済みの値を **そのまま**送る（符号・感度・不感帯・軸変換は掛けない）。
  掛けると記録時の加工が二重になり、記録した動きからずれる。送出周期は
  ヘッダーの `period_us`（v1 は 5000µs = 200Hz）で、送り切ると自動で停止する
  （最後の値を送り続けない）。
- 記録先と再生元に同じファイルを指定した場合、再生はそのままだが記録はしない
  （上書きで元の記録が消えるため）。記録先が空のまま記録を有効にすると開始しない。

- 記録は送出ループの刻みごとに1件書き、遅れは詰めて並べる（ファイル上は
  5ms 等間隔）。送出が数tick遅れたぶんは記録の長さも実時間より短くなる。

形式（v1、リトルエンディアン）:

| 位置 | 内容 |
|---|---|
| 0-3 | magic `PGYR` |
| 4-5 | version = 1 |
| 6-7 | header_size = 32 |
| 8-11 | period_us = 5000 |
| 12-15 | sample_count |
| 16-19 | flags = 0 |
| 20-27 | start_offset_us（ボタン記録との同期用。単体では0） |
| 28-31 | reserved = 0 |
| 32- | sample_count × 12B（int16-LE ×6） |

- 記録が元の入力そのものではない点に注意: SDLセンサーは開いた時点でバイアス
  （`[IMU] gyro_bias`）を引き、`watched()` が符号・`gyro_scale`・不感帯を
  掛けた後の値が記録対象。**同じ記録を別の設定で再生しても、記録時の設定で
  送った値が出る**。記録を撮り直すときは設定を固定する。
- ヘッドレスで試すときは `tools/imu_send.py --record out.pgyro` /
  `--play out.pgyro`（`--wave sensor` / `--wave hid` と組み合わせる）。

## Blockly の記録との連携

Blockly エディタの「● 記録」は、ジャイロ送出中なら送った IMU サンプルも同時に
残す（`services/blockly_record.py` の聞き手 → `Commands/PythonCommands/blockly_*.pgyro`）。
停止時に「ジャイロを再生」ブロックが手順の先頭へ入り、生成コードは
`self.gyro_play("<パス>")` を呼ぶ。再生は `OperateMixin.gyro_play` が
`transport.start_imu_loop(..., replace=True)` で開始し、コマンド終了時の
`_cleanup` が止める。記録値は加工しない（符号・倍率・不感帯を掛け直さない）。
詳細は [`docs/BLOCKLY_EDITOR.md`](BLOCKLY_EDITOR.md) の「操作の記録」。

- IMU 送出ループの持ち主は `start_imu_loop(..., owner=...)` で区別する（`panel` / `macro:<id>`）。
  `stop_imu_loop(owner=...)` は一致するときだけ止め、省略時（`close` など）は問わずに止める。
  パネルとマクロの相互乗っ取りは [`docs/BLOCKLY_EDITOR.md`](BLOCKLY_EDITOR.md) の
  「ジャイロの競合と持ち主」を参照。
