# GPU プレビュー面（SDL_Renderer）設計

Status: 実装済み（Windows で実測）。Linux の窓の埋め込みだけ未実機確認。

## 0. 何を変えたか

映像の提示を GDI（CPU の `BitBlt`）から GPU（SDL_Renderer）へ移した。
Tkinter はそのまま GUI を握り、映像の矩形だけを GPU が描く。

```
CameraBackend           最新1枚              VideoRenderer            GPU
(core.Camera /          (LatestFrame /       (core/sdl_surface.py)    texture → 拡大縮小
 core.CameraQueue)       shm 3面ローテーション)      │                   → swapchain
        │                     │                   │                        │
   cv2.VideoCapture      seq だけ進める         BGR→BGRA 1パス +          D3D11 / OpenGL
   (別スレッド/プロセス)  古いフレームは捨てる    SDL_UpdateTexture         子窓（Tk の内側）
```

取得側（`camera_backend.py` の `CameraBackend`）と描画側
（`preview_renderer.py` の `VideoRenderer`）は別の契約にしてある。描画は
フレームの来し方を知らず、取得は描画 API を知らない。

## 1. 方式の選定

候補は SDL3 GPU・Vulkan・OpenGL・SDL2 Renderer。

| 候補 | Windows | Linux | 追加依存 | 判断 |
|---|---|---|---|---|
| SDL3 GPU | D3D12 / Vulkan | Vulkan | SDL3 本体 + SPIR-V/DXIL のシェーダをビルドする仕組み | 却下。描画は共通になるが、シェーダのビルド産物を配布物へ載せる手順が要り、このアプリの「Python を入れて `uv sync`」の導入を壊す |
| Vulkan 直叩き | ✓ | ✓ | なし（ドライバ） | 却下。窓の生成・スワップチェーン・サーフェスを自前で書き、OS ごとの分岐が増える。1080p60 の矩形 1 枚には過剰 |
| OpenGL 直叩き (WGL/GLX) | ✓ | ✓ | なし | 却下。コンテキスト生成だけが OS 固有で、その部分を自前で維持することになる。SDL が同じことをしてくれる |
| **SDL2 Renderer** | **D3D11** | **OpenGL** | **なし（pygame が SDL2 を同梱）** | **採用** |

採用理由:

1. `pygame` は既に依存で、ゲームパッドのために import 済み。その wheel が
   `SDL2.dll` / `libSDL2-2.0.so.0` を同梱しているので、**新しい依存も
   ビルド手順も増えない**（`core/sdl_surface.py` が pygame のパッケージ
   ディレクトリから実体を探す。`POKECON_SDL2_LIB` で差し替え可）。
2. 描画の呼び出しが OS で共通: テクスチャ更新 → `SDL_RenderCopy`（GPU が
   拡大縮小）→ `SDL_RenderPresent`。OS 固有なのは「窓をどう用意するか」
   だけで、そこは `SdlSurfaceApi` の裏に隠してある。
3. CUDA もベンダ固有 API も使わない。SDL が D3D11 / OpenGL / Metal を選ぶ
   ので、Intel / AMD の内蔵 GPU でも同じ経路。
4. 実測（このマシン、RTX 4090、`direct3d11`、`SDL_RenderSetVSync(0)`、
   全オーバーレイ + バッジ表示、300 反復の中央値）:

   | 映像 | compose（転送込） | present（描画+提示） | 1 tick |
   |---|---|---|---|
   | 1280x720 | 0.507 ms (p99 0.802) | 1.167 ms (p99 1.449) | **1.679 ms** |
   | 1920x1080 | 1.084 ms (p99 1.439) | 1.073 ms (p99 1.762) | **2.168 ms** |

   60fps の予算 16.667 ms に対して 720p で 10%、1080p で 13%。GDI 面は
   同じ 720p で 1 tick 0.45 ms だが、その内訳の 72% は `BitBlt` で、縮小時は
   `StretchBlt(HALFTONE)` が全画素を CPU で濾す。GPU 面はその両方を GPU へ
   移し、残る CPU 仕事は `cvtColor` 1 パス（720p で 0.096 ms、1080p で
   0.178 ms）と転送だけになる。

### Windows で `direct3d11` を明示する理由

SDL2 の描画器を `-1`（自動）で作ると `direct3d`（D3D9）が選ばれる。子窓
（Tk の Frame の内側に作った HWND）へ D3D9 で提示すると、
`SDL_CreateRenderer` も `SDL_RenderPresent` も成功を返すのに**画面には
1 画素も出ない**（実測。親の背景色のまま）。`direct3d11` を指定すると
同じ子窓へ正しく出る。そこで `_DRIVER_PREFERENCE` で
`direct3d11 → metal → opengl → opengles2 → direct3d` の順に試す。

## 2. 窓の用意（OS 固有なのはここだけ）

**Windows**: GDI 面と同じ形。Tk の Frame の HWND を親にして `STATIC`
クラスの子窓を作り、`SDL_CreateWindowFrom` で SDL に包ませる。子窓は
`EnableWindow(FALSE)` にするのでマウスは親の Tk へ届き、既存のスティック
操作（`<Control-ButtonPress-1>` など）はそのまま動く（実測）。

子窓の大きさは **`SetWindowPos` で直接変える**。`SDL_SetWindowSize` は
`SDL_CreateWindowFrom` で包んだ窓（`SDL_WINDOW_FOREIGN`）には効かず、
戻り値は成功なのに HWND は 1 画素も変わらない（実測: 640x360 のまま
988x434 を要求し、`SDL_GetRendererOutputSize` も 640x360 を返し続けた）。
その間 Tk 側の表示面だけが広がるので、映像が伸びず、オーバーレイは
古い矩形に切られて操作位置とずれる。`SetWindowPos` なら SDL の窓手続きが
`WM_SIZE` を受け取り、描画器の出力サイズもその場で追従する（実測）。

Tk の Frame の HWND を直接 `SDL_CreateWindowFrom` に渡してはならない。
SDL がその窓の手続きを差し替えるので、Tk の事象処理と取り合いになる。

**Linux（未実機確認）**: Tk の widget の窓 ID（`winfo_id()`）を
`SDL_CreateWindowFrom` へ渡す。X11 では SDL がその窓をそのまま使う。
Wayland では XWayland 上の窓として扱われるため、動かない場合は
`attach` が失敗し（`attached == False`）、GUI が GDI 無しの次段
（PhotoImage）へ落ちる。**落ちる先を用意してあるのが要点**で、起動は止まらない。

## 3. 画素の道筋（CPU コピーは 1 回だけ）

```
camera.read() → frame (BGR, C 連続)            … 取得スレッド（コピーなし）
   ↓ 公開（参照の差し替えだけ。キューに積まない）
描画 tick（GUI スレッド）
   cv2.cvtColor(frame, BGR2BGRA, dst=_bgra)    … 1 パス。作業バッファは 1 本を再利用
   SDL_UpdateTexture(tex, NULL, _bgra, w*4)    … ステージング領域へ 1 コピー
   SDL_RenderCopy(tex, dest)                   … GPU が拡大縮小と色順の解釈
   SDL_RenderPresent                           … スワップチェーン
```

* PIL / `ImageTk` / DIB / `BitBlt` は映像経路から消えた。PIL が残るのは
  バッジの文字を焼くときだけで、**文字が変わった瞬間に 1 回**だけ
  （テクスチャはキャッシュし、同じ文字なら使い回す）。
* `ARGB8888`（メモリ順 B,G,R,A）は D3D11 が広告する唯一の 32bit 形式。
  `BGR24` を渡すと SDL 内部で CPU 変換される（実測で広告外）。だから
  `cvtColor` の 1 パスは残す。720p で 0.086 ms（GDI 面と同じ計測）。
* テクスチャは映像の解像度に追従する（初回とサイズ変更時だけ作り直す）。
  したがって **1080p のカメラでもそのまま通る**（GDI/Photo 面は
  `CAPTURE_SIZE` 固定で、それ以外のフレームを `dimension_mismatch` で捨てる）。
  オーバーレイの座標変換は実フレームの大きさから `fit_rect` で求める。

## 4. 遅延

* 取得側はキューを持たない（`LatestFrame` / 共有メモリ 3 面）。読む側が
  遅れても古いフレームは溜まらず、常に最新が上書きされる。
* `SDL_RenderSetVSync(renderer, 0)`。Tk の `PreviewClock` が提示の間隔を
  決めており、vsync を入れると取得→表示の待ちが 1 フレーム増える。
* `present` は毎回「クリア → 映像 → オーバーレイ → present」。D3D11 の
  スワップチェーンは提示後に中身が保証されないので、`recompose`（映像は
  そのまま、オーバーレイだけ変更）でもクリアから描き直す。映像の
  **再転送はしない**（テクスチャは残っている）。
* 「テクスチャに 1 枚入っているか」(`_has_frame`) と「次の present で
  描き直す必要があるか」(`_dirty`) を別の旗にしている。1 本にすると
  提示後に `recompose` が `no_frame` を返し、**カメラが止まっている間の
  スティック描画が更新されない**（実測でドラッグ中に固まった）。

## 5. オーバーレイ

GDI 面と同じ絵を、装置座標で描く。`OverlayState` は不変データのまま。

* スティック外周: 64 分割の折れ線（1px）。ノブ: 三角形の扇で塗る。
  **ノブの中心は `knob_x` / `knob_y`**（押下点ではない）。押下点に固定して
  描いていたため、ドラッグしても小円が動かなかった（実測）。外周円は
  `center_x` / `center_y` のまま動かない。
* 範囲枠: SDL に破線が無いので、装置座標で 4px 刻みに切って線分を並べる。
* 認識枠: 白 4px（キャプチャ座標）→ 認識色 2px の順。太さは表示倍率を
  掛け、縮小時も消えないよう下限 1px。
* バッジ: 「表示 1:1 で文字 16px・余白 4px」を装置座標で直接描く。
  GDI 面はキャプチャ座標へ逆数倍してから拡大縮小していたが、同じ見た目に
  なるので換算を持たない。
* ノブ（塗り）は `SDL_RenderGeometry` で描く。この API は**描画色ではなく
  頂点色**を使う。頂点色を入れないと全部 0（黒）になり、ノブが映像に
  開いた穴のように見える（実測でそうなっていた）。
* 映像の外（余白）へはみ出さないよう `SDL_RenderSetClipRect` で映像矩形に
  切ってから描く。空の `OverlayState` は 1 つも描画呼び出しを出さない。

## 6. 落ち方（3 段）

| 段 | 実装 | いつ使う |
|---|---|---|
| 1 | `core/sdl_surface.py` | 既定。GPU |
| 2 | `core/gdi_surface.py` | Windows で SDL が無い/GPU に載らない |
| 3 | `ui/photo_surface.py` | 最終手段（Canvas + PhotoImage。60fps は出ない） |

失敗は 2 か所で拾う。**組み立て**（SDL2 の実体が無い・GDI が無い）は
`_create_preview_surface` が次の候補へ進む。**接続**（GPU 無し・子窓を
作れない）は `CaptureArea._attach_preview_surface` が `attached` を見て
1 段だけ落ちる。どちらもログに理由を残し、起動は止めない。

## 7. zero-copy の拡張点（未実装）

`preview_renderer.py` に `NativeFrameHandle` と `NativeFrameTarget` を置いた。
キャプチャ側が GPU 上にフレームを持てるようになったら（D3D11 テクスチャ
共有・dmabuf・NV12 サーフェス）、`CameraBackend` の実装がハンドルを返し、
`isinstance(面, NativeFrameTarget)` が真の面へ `compose_native` で渡す。
それ以外の面は従来どおり `compose(ndarray)` を受けるので、経路を増やしても
既存の 3 面は変わらない。SDL2 は NV12/YV12 テクスチャを GPU で変換できる
（D3D11 が広告する）ため、キャプチャボードが NV12 を直接出せるようになれば
`cvtColor` の 1 パスも消せる。

## 8. 検証

* `tests/test_sdl_surface_contract.py` — `SdlSurfaceApi` を偽物にして、
  窓・描画器・テクスチャの生成数、BGRA 転送のピッチとバッファ再利用、
  サイズ変更時のテクスチャ作り直し、`present` の順序、空オーバーレイで
  描画 0 回、色変換（COLORREF → RGBA）、バッジのキャッシュ、解放順を固定。
* `tests/test_sdl_surface_e2e.py` — 実画面が要る部分だけを `POKECON_RUN_SDL_E2E=1`
  で走らせる。`resize` が SDL の実クライアントを広げること（`SDL_SetWindowSize`
  では広がらない不具合の再発防止）と、新しい大きさで合成・提示できることを固定。
  CI では skip する（ディスプレイが無い環境で Tk/SDL を起こさない）。
* `tests/test_renderer_selection.py` — 既定が GPU になったこと、明示 `gdi` /
  `photo` が尊重されること、SDL 不在時に GDI / Canvas へ落ちること、
  接続失敗時に 1 段だけ落ちることを固定。
* 実画面: Tk の Frame に `SdlSurface` を繋ぎ、合成した 1280x720 の
  BGR フレームと全オーバーレイ（両スティック・範囲枠・認識枠・バッジ）を
  120 フレーム提示してスクリーンショットで確認（Windows / D3D11）。
  `BitBlt` 相当の読み戻しはしていないので、確認は画面の画素で行った。

## 9. 未確認・既知の制約

* Linux の窓の埋め込み（X11 / Wayland）は実機が無く未確認。失敗しても
  PhotoImage 面へ落ちる。
* 子窓は `EnableWindow(FALSE)` で入力を取らない。このため子窓自身への
  ドラッグ＆ドロップや IME は対象外（従来の GDI 面と同じ扱い）。
* `macOS` は Metal を候補に置いたが未確認。
* 映像の色はカメラの BGR をそのまま BGRA へ詰める。色域変換（BT.601/709）
  はしない。`cv2.VideoCapture` が BGR を返す前提で、GDI 面と同じ。
* DPI: このアプリは DPI unaware（Tk が論理座標で動き、Windows が 125% 等で
  引き伸ばす）。SDL も同じ文脈で動くので単位は一致し、スティック円の
  60px は Tk の 60px として見える。`SDL_SetWindowSize`/`SetWindowPos` の
  要求と `SDL_GetRendererOutputSize` が食い違ったときは、どちらかの座標系が
  ずれた合図なので `resize` が警告を出す。アプリ側で `SetProcessDPIAware` を
  呼ぶと Tk と SDL の単位がずれるので、呼ばない。
