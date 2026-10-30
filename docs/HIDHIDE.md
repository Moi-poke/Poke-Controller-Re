# HidHide とコントローラ入力

Switch Pro コントローラ（互換機を含む）の HID ジャイロとパッド入力を
同じアプリで使うには、Steam / SDL に同じ HID を触らせない必要がある。
Steam は USB の Switch Pro を simple report mode（0x3F）へ切り替える
ことがあり、HID 側が 0x30 を待っていると数秒間入力が止まる（SDL #3450）。
HidHide は対象デバイスを他プロセスから隠し、許可したアプリだけに
HID を見せる。

## 前提

- HidHide は **手動でインストール**する。アプリはカーネルドライバを
  自動インストールしない。
- 公式配布: <https://github.com/nefarius/HidHide/releases/latest>
- Windows 10/11 64-bit。管理者権限が必要。
- インストール後、必要なら再起動する。

## GUI から設定する

アプリ内で完結させる場合は、`ツール > HidHide 設定...` を開く
（コントローラタブのパッド行にある「HidHide セットアップ」からも開ける）。
管理者権限は要らない。HidHideCLI.exe は設定変更に elevation を要求しない。

- 行のチェックで「HidHide / アプリの許可 / Pro Controller / cloak」を確認する。
- `セットアップ` で、この Python の許可・接続中の Switch コントローラ
  の hide・cloak ON を一度に書き込み、そのまま「ゲームパッドで操作」を
  有効にする（アプリの再起動・挿し直しは不要）。
- cloak の行の `ONにする / OFFにする` は hide リストを保ったまま入/切する。
- `解除` は cloak OFF・このアプリの許可解除・Switch コントローラの
  hide 解除をまとめて行う（他のアプリの hide は触らない）。
- HidHide の設定アプリ（Configuration Client）を開いていると CLI が
  ドライバを掴めず失敗する。閉じてから操作する。
- コントローラの hide は接続中のインスタンスにしか登録できない。
  未接続なら接続してから `再検出` → `セットアップ` をやり直す。
- 状態の「hide済み」は、接続中のコントローラの instance path が
  hide リストにあるかで判定する（別の USB ポートで使った古い entry
  だけが残っていても「済み」にしない）。
- HidHideCLI の `--dev-gaming` が途中で切れる環境（日本語のデバイス名で
  実測）では、OS のデバイス一覧から HID 子の instance path を補って
  hide する。
- HidHideCLI は引数エラーを stderr に出して終了コード 0 で止まることが
  あるため、stderr があれば失敗として表示する（黙って一部だけ適用しない）。
- CLI の変更は正常終了時に書き込まれる。通常はそのまま HID を開けるが、
  反映されない環境ではコントローラの挿し直し（またはデバイスの
  無効→有効）を行う。

## 手動で設定する（CLI）

1. `HidHideCLI.exe --dev-gaming` で対象の `deviceInstancePath` と
   `baseContainerDeviceInstancePath` を調べる。
2. アプリの Python 実行ファイルを許可する。venv の python は
   uv の base python と同一実体のことがあるため、両方登録する。

```text
HidHideCLI.exe --app-reg "C:\path\to\.venv\Scripts\python.exe"
HidHideCLI.exe --app-reg "C:\Users\<user>\AppData\Roaming\uv\python\<...>\python.exe"
```

3. HID 子と USB 親を hide する（XInput の回り込み対策）。

```text
HidHideCLI.exe --dev-hide "HID\VID_057E&PID_2009&MI_00\..."
HidHideCLI.exe --dev-hide "USB\VID_057E&PID_2009\..."
HidHideCLI.exe --cloak-on
```

4. 設定後、コントローラを挿し直す（またはデバイスを無効→有効）。
5. `HidHideCLI.exe --cloak-state --dev-list --app-list` で確認する。

GUI の HidHide Configuration Client でも同じ設定ができる。

## アプリ側の挙動

- HidHide が **ready**（cloak-on / アプリ許可 / Switch HID を hide）の
  ときだけ、HID 直結のパッド・ジャイロを使う。
- 未導入・未設定なら、Switch HID のパッド/ジャイロは無効になり、
  パッド行に「HidHide セットアップ」を出す。通常の読み取り経路は
  HID 直結だけで、SDL はデバッグ用（`POKECON_SDL_DEBUG`）のため、
  「ゲームパッドで操作」のチェックも無効にする。セットアップを
  適用すると、その場で判定をやり直してチェックを有効にする。
- 通常は HID 有線のみ。SDL をデバッグ用に解禁するときだけ、起動前に
  `POKECON_SDL_DEBUG=1` を設定する。

## 注意

- HidHide の許可リストは実行ファイル単位。Python を許可すると、
  同じ Python を使う別スクリプトも隠したデバイスを見られる。配布時は
  専用 exe にするか、許可対象を絞る。
- 一時的に解除する: `HidHideCLI.exe --cloak-off`
- 元に戻す: `--dev-unhide` / `--app-unreg`、または HidHide をアンインストール。
