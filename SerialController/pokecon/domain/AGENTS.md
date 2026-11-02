# domain/ — GUI 非依存の中核

ここは tkinter を import しない層。画面の都合（Tk の変数・ウィジェット・
スレッド制約）を持ち込まない。`task bounds` が違反を落とす。

## 触る前に知ること

- [`Commands/*.py`](../../Commands/) は後方互換の再公開口で、実体はここにある。
  新しいコードは `pokecon.domain` から読み、`Commands` 側へ実装を足さない。
- ここでファイルを移すときは、旧位置に同じファイル名の再輸出 shim を残す
  （`from pokecon.domain.X import Y as Y`）。利用者スクリプトの import 経路を守るため。
- 公開名は消さない。約束した一覧は [`api_surface.py`](api_surface.py)、
  凍結 / 暫定の区分は [`user_api_allowlist.py`](user_api_allowlist.py) が持つ。
  `task api` が欠落を落とす。
- 実行時の基準ディレクトリは [`paths.py`](../paths.py) の `BASE_DIR`
  （= `SerialController/`）。`__file__` から `..` を数えない。

## 触るときに読む文書

- 層の向きと責務: [`docs/ARCHITECTURE.md`](../../../docs/ARCHITECTURE.md)
- ジャイロ・HID: [`docs/IMU.md`](../../../docs/IMU.md)
- 通信方式を足す: [`docs/BCON_TRANSPORT.md`](../../../docs/BCON_TRANSPORT.md)

## 検査

```
task bounds      # 層の向き
task api         # 公開名の欠落
task test -- -k domain
```
