# ui/ — tkinter の部品

ここは Tk を触る層。1つのスレッド（GUI スレッド）からしか widget を触れない。
ワーカーからは値をキューへ積むだけにし、描画は GUI 側で行う。

## 触る前に知ること

- `pokecon.app.Window` を import しない（循環になる）。部品は Window を知らず、
  必要な値は呼び出し側が渡す。
- カメラ画像の描画は [`domain/preview_renderer.py`](../domain/preview_renderer.py) の
  裏にある。OS 固有の描画をこの層に直接書かない（`SdlSurfaceApi` か新しい surface へ）。
- `after(1)` の自前チェーンを足さない。Tk が数秒おきに数百 ms 止まる原因になる。
  周期処理は `PreviewClock.set_poll_hook` に乗る。
- 設定値は `pokecon.app.Settings` / `pokecon.domain.config` 経由で読む。
  ini のキー名は既存利用者のファイルが凍結面なので、勝手に改名しない。

## 検査

```
task bounds      # 層の向き（ui → Window の禁止など）
task app         # 実画面で確認する（ヘッドレスでは描画を確かめられない）
```

実画面の確認は [`gui-probe`](../../../.opencode/skills/gui-probe/SKILL.md) の手順を使う。
確認していない見た目を「確認した」と書かない。
