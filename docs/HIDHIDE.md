# HidHide とコントローラ入力

Switch Pro コントローラ（互換機を含む）の HID ジャイロとパッド入力を同じアプリで扱うには、Steam / SDL に同じ HID デバイスを触らせない必要がある。Steam は USB 接続の Switch Pro コントローラを simple report mode（`0x3F`）へ切り替えることがあり、HID 側が `0x30` のレポートを待ち受けていると数秒間入力が停止する現象が起きる（SDL #3450）。HidHide を利用することで、対象デバイスを他プロセスから隠し、許可したアプリケーションだけに HID を見せることができる。

## 前提

- HidHide 本体は**手動でインストール**する必要がある（アプリ側がカーネルドライバを自動インストールすることはない）。
- 公式配布元: <https://github.com/nefarius/HidHide/releases/latest>
- 対応環境は Windows 10 / 11（64-bit）であり、インストールには管理者権限が必要となる。
- インストール完了後は、必要に応じて PC を再起動する。

## GUI から設定する

アプリ内で設定を完結させる場合は、メニューの `ツール > HidHide 設定...` を開く（コントローラタブのパッド行にある「HidHide セットアップ」からも開ける）。設定変更時の `HidHideCLI.exe` は権限昇格（elevation）を要求しないため、この操作に管理者権限は不要である。

- **状態の確認**: 各行のチェック表示で「HidHide / アプリの許可 / Pro Controller / cloak」の状態を確認できる。
- **一括セットアップ**: `セットアップ` を実行すると、実行中の Python の許可・接続中の Switch コントローラの hide・cloak ON をまとめて書き込み、そのまま「ゲームパッドで操作」を有効化する（アプリの再起動やコントローラの挿し直しは不要である）。
- **cloak の入切**: cloak 行の `ONにする` / `OFFにする` は、hide リストの内容を保持したまま cloak の有効・無効のみを切り替える。
- **設定の解除**: `解除` を実行すると、cloak OFF・このアプリの許可解除・Switch コントローラの hide 解除をまとめて行う（他のアプリの hide には触れない）。
- **Configuration Client との競合**: HidHide の設定アプリ（Configuration Client）を開いたままにしていると、CLI がドライバを掴めず失敗する。必ず閉じてから操作する。
- **未接続時の扱い**: コントローラの hide 対象には、接続中のインスタンスしか登録できない。未接続だった場合はコントローラを接続してから `再検出` → `セットアップ` の順にやり直す。
- **「hide済み」の判定基準**: 「hide済み」の判定は、接続中のコントローラの instance path が hide リストに含まれているかで行う（別の USB ポートで使用した古いエントリだけが残っていても「済み」とは判定しない）。
- **日本語デバイス名等への補完**: 日本語のデバイス名などで `HidHideCLI` の `--dev-gaming` 出力が途中で切れる環境（実測あり）では、OS のデバイス一覧から HID 子デバイスの instance path を補完して hide を登録する。
- **エラー判定**: `HidHideCLI` は引数エラー時に `stderr` へメッセージを出したうえで終了コード `0` で停止することがあるため、`stderr` に出力がある場合は失敗として表示する（黙って一部だけを適用することはしない）。
- **反映と挿し直し**: CLI による変更は正常終了時に書き込まれる。通常はそのまま HID を開けるが、環境によって変更が反映されない場合はコントローラの挿し直し（またはデバイスの無効→有効）を行う。

## 手動で設定する（CLI）

1. `HidHideCLI.exe --dev-gaming` を実行し、対象の `deviceInstancePath` と `baseContainerDeviceInstancePath` を調べる。
2. アプリの Python 実行ファイルを許可リストへ登録する。venv の `python` は `uv` の base `python` と同一実体の場合があるため、両方を登録する。

   ```text
   HidHideCLI.exe --app-reg "C:\path\to\.venv\Scripts\python.exe"
   HidHideCLI.exe --app-reg "C:\Users\<user>\AppData\Roaming\uv\python\<...>\python.exe"
   ```

3. XInput 経由の回り込みを防ぐため、HID 子デバイスと USB 親デバイスの両方を hide したうえで cloak を有効化する。

   ```text
   HidHideCLI.exe --dev-hide "HID\VID_057E&PID_2009&MI_00\..."
   HidHideCLI.exe --dev-hide "USB\VID_057E&PID_2009\..."
   HidHideCLI.exe --cloak-on
   ```

4. 設定完了後、コントローラを挿し直す（またはデバイスの無効→有効を行う）。
5. `HidHideCLI.exe --cloak-state --dev-list --app-list` を実行して設定を確認する。

なお、GUI の HidHide Configuration Client を使っても同じ設定を行える。

## アプリ側の挙動

- HidHide が **ready**（cloak-on・アプリ許可・Switch HID の hide が揃った状態）のときだけ、HID 直結によるパッドおよびジャイロ入力を使用する。
- 未導入または未設定のときは、Switch HID のパッド・ジャイロ入力は無効となり、コントローラタブのパッド行に「HidHide セットアップ」を表示する。通常の読み取り経路は HID 直結のみであり、SDL はデバッグ用途（`POKECON_SDL_DEBUG`）の位置付けであるため、「ゲームパッドで操作」のチェック自体も無効化される。セットアップを適用すると、その場で状態判定をやり直してチェックを有効にする。
- 通常は HID 有線接続のみを使用する。デバッグ用に SDL 経路を解禁する場合に限り、アプリ起動前に `POKECON_SDL_DEBUG=1` を設定する。

## 注意

- HidHide の許可リストは実行ファイル単位で管理される。そのため Python 実行ファイルを許可すると、同じ Python を使って動作する別スクリプトからも隠したデバイスが見えてしまう。配布時は専用の実行ファイル（exe）にするか、許可対象を絞る必要がある。
- 一時的に解除する場合: `HidHideCLI.exe --cloak-off`
- 設定を元に戻す場合: `--dev-unhide` / `--app-unreg` を実行するか、HidHide 本体をアンインストールする。
