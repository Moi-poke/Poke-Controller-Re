# フレーム駆動プレビュー描画（描画スレッド化）設計

Status: implemented 2026-10-30.

改訂元: [`2026-09-26-gdi-preview-renderer-design.md`](2026-09-26-gdi-preview-renderer-design.md)
（`StretchDIBits` 廃止・GDI 子窓・バックバッファ・HALFTONE はそのまま引き継ぐ）。

## 0. 直した問題

native clock（[`2026-09-25-windows-high-resolution-preview-clock-design.md`](2026-09-25-windows-high-resolution-preview-clock-design.md)）
は tick を Tk のメインスレッドへ渡し、その場で compose + present していた。
Tk がログ描画・コマンド・入力処理で数十 ms 止まると tick が遅れ、latest-only の
フレーム箱を読むので、その間のフレームが落ちる。

**実測（2026-10-30、1280x720・合成 60fps 源、`tests/` の外の使い捨て計測）**:

| 条件 | 旧（clock・メインスレッド描画） | 新（描画スレッド） |
|---|---|---|
| 負荷なし 60fps | 59.99 fps / 0 落ち | 59.99 fps / 0 落ち |
| メインスレッドに 200ms ごと 40ms の Python 仕事 | **49.3 fps / 30 回の >34ms 間隔** | **59.94 fps / 1 回** |

（>34ms 間隔 = 2 フレーム以上の取りこぼし。負荷なしの新経路は 4.5 秒窓で
公開 270 枚に対し提示 270 枚。）

## 1. 方針

描画を `core/preview_pump.py` の専用スレッドへ移し、**フレームの公開で起きる**。
時刻で刻む限り、取込と表示の位相がずれたぶんだけ「空振り（同じ seq）」と
「1 枚飛ばし」が出る（`core/preview_phase.py` の実測 45fps がそれ）。公開で
起きれば、取込と表示が同じ 60fps のとき原理的に 0 落ちになる。

- 描画スレッドは Tk・Tk 変数・ウィジェットを触らない。描画面（GdiSurface）は
  子 HWND へ直接描くのでスレッドを選ばない（8節の実測）。
- Tk のウィジェットへ描く面（`ui/photo_surface.py`、mac/Linux）は
  `cross_thread_render = False` で、従来どおり clock がメインスレッドで描く。
- 表示 fps の設定は「次の期限までの待ち」で上限として掛ける。期限は
  `deadline += period` で進め、大きく遅れたら `now + period` へ取り直す
  （溜まった期限を連射しない）。判定には 1.5ms の許容を置く。0 にすると
  60fps 同士でも公開の揺らぎで半分ほど落ちる（実測 58.7fps）。

## 2. 起床

`LatestFrame`（[`core/Camera.py`](../../../SerialController/core/Camera.py)）に
2 つ足した。

- `wait_for_change(after_seq, timeout)`: 世代が変わるまで Condition で待つ。
  `Camera.wait_for_frame` がそのまま公開する。
- `subscribe(callback)` / `unsubscribe`: 公開のたびに呼ぶ。描画スレッドの
  `notify()`（オーバーレイ・画面サイズ・fps の変更）と合わせて、状態変化でも
  起きる。

`put` / `clear` は錠の上で `notify_all` する。購読者の呼び出しは錠の外。
購読者が自分の錠を取るので、持ったまま呼ぶとロック順が絡む。

## 3. CaptureArea との分担

- `startCapture()`: pump を作って `set_fps` / `set_active(True)` / `start()`。
  このときだけ `sys.setswitchinterval(0.001)` に下げる（5節）。
- `_dispatch_tick()`（pump があるとき）: `is_show_var` を読んで active を
  反映し、オーバーレイが変わったときだけ `notify()`。**描かない。**
- `stopCapture()`: 先に pump を止めて join する。join できなければ
  `StopResult.PENDING`（終了階段は STOPPED のときだけ映像面を解放する）。
- `getStats()` / `_preview_evidence_snapshot()`: pump のカウンタを返す
  （描画は Tk スレッドで起きないので `_stat_shown` は常に 0 になる）。

`_drawFrame` は pump から呼ばれる。Tk を触らないので、そのまま共有できる
（オーバーレイは不変 dataclass の差し替え、フィルタ設定は有効旗を最後に
代入する順序が保証）。pump へは束縛済みメソッドではなく
`lambda frame, seq: self._drawFrame(...)` を渡す。計測・検査が後から
`_drawFrame` を差し替えても効くようにするため。

## 4. 面のスレッド安全性（GdiSurface）

- `compose` / `recompose` / `present` / `resize` / `attach` / `release` /
  `client_size` を `threading.RLock` で直列化する。1 フレーム 2 回の取得で、
  競合していないときの費用は無視できる。
- ctypes の DLL を `WinDLL` → `PyDLL` にした。`WinDLL` は呼び出しごとに GIL を
  離すので、1 フレームに 10 回前後の取得が続き、他スレッドが GIL を持って
  いると取得待ちが `sys.getswitchinterval()` ぶん積み上がる。実測（同じ負荷）:
  描画 1 回が平均 7.0ms・最大 40ms → 平均 5.5ms・最大 29ms、落ちたフレーム
  6 → 2枚。GDI 呼び出しは 1 回 1ms 未満なので、GIL を保持しても他を待たせない。

## 5. GIL の切り替え間隔

`sys.setswitchinterval()` は既定 5ms。1 フレームで GIL を離すのは
`cv2.cvtColor` と `np.copyto` の 2 回で、そのたびに最大 5ms 待つ。
描画中だけ 1ms に下げ、止めたら元へ戻す（実測、CPU を常時使うスレッドが
居る場合: 1ms で 60.1fps、2ms で 49.7fps、5ms で 33.7fps）。

## 6. 既知の限界

- 取込より速くは描けない。カメラが 60fps を割れば表示も割る。
- 表示 fps の上限はフレーム単位で量子化される。60fps 源を 45fps に絞ると
  期限判定の粒度で 45 前後になる（設定の意味は「上限」であって「厳密な周期」
  ではない）。
- 描画スレッドが GDI 呼び出しの中で止まった場合、その 1 枚は落ちる。
  次の公開で起き直すので、遅れは累積しない。
- メインスレッドが長時間 GIL を保持する C 呼び出し（巨大な正規表現など）を
  している間は、どの Python スレッドも動けない。切り替え間隔では直せない。

## 7. 検証

- `tests/test_preview_pump.py`: 同一レートで全フレームを描く／表示上限で
  早いフレームを捨てる／遅い源で落とさない／非表示で描かない／停止画像／
  描画例外／統計／購読と停止／**停止画像で空転しない**。
- `tests/test_gdi_surface_contract.py`: `PreviewRenderer.cross_thread_render`。
- 使い捨て計測（実装時に `.tmp/` で実行し、終了後に削除）: 合成 60fps 源 +
  実 CaptureArea + 実 GDI 面 + Tk ルート。提示回数と間隔を
  `surface.present` のラップで記録し、画面の中心画素が源の色（40,40,40）で
  あることまで確認した（スレッド跨ぎの BitBlt が実際に画面へ出る）。
