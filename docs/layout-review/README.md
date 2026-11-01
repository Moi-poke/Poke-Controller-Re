# layout-review 資料の使い分け

## 次のチャット AI に投げるもの

**自己完結版（ローカルパス不要）**: `reply-to-chat-ai.md` をそのまま貼り、実画面 5 枚を添付する。

**目的: 次段階の設計確認**

1. `next-stage-decisions.md` — 決定してほしい Q1〜Q7（Q3 と sash、DPI 測定条件を修正済み）
2. `next-stage-plan.md` — チャット AI の回答を記録した確定案と実装順
3. `implementation-status.md` — 現在の実装済み / 未実装
4. 実画面 5 枚（`images/real-1280-*.png`, `images/real-1920-*.png`）

「この計画でよいか」を聞くだけなら **2 + 3 + 画像** で足ります。
「未決定の Q を判断してほしい」なら **1 + 画像** を送ってください。

## 送らなくてよいもの

- `reply-to-reviewer.md` / `proposal-triage.md` / `response-to-first-review.md` / `response-to-second-review.md`
  - 過去のレビュー往復。数値や前提が古い。
- `current-layout-review.md`
  - 最初の現行 UI 資料。相手がまだ見ていない場合だけ送る。
- `detail-ui-audit.md`
  - 詳細監査。必要なら追加で送るが、`next-stage-plan.md` と `implementation-status.md` に要点は入っている。

## 根拠を聞かれたら

- DPI: `~/research/tkinter-dpi/map.md` と `sources/`
- 0x33: `https://github.com/Moi-poke/Switch-bcon/blob/main/spec/protocol_v3.md#L123` と `src/main.c` の `FX_KEY_DELETE`
- 実測: `implementation-status.md` の「検証」と実画面

## 画像

- `images/real-1280-serial.png`
- `images/real-1280-bcon.png`
- `images/real-1280-controller.png`
- `images/real-1280-controller-hidhide.png`
- `images/real-1280-preview-off.png`
- `images/real-1280-controller-tabs-right.png`
- `images/real-640-serial-tabs-right-fallback.png`
- `images/real-1280-camera.png`
- `images/real-1920-camera.png`
- `images/real-1280x480-controller-tabs-right.png`
- `images/real-1280-command.png`
- `images/real-1920-serial.png`
- `images/real-1920-controller.png`
- `images/real-150-1920-controller.png`
- `images/real-1280-bcon-after.png`
- `images/real-1280-log-tab.png`
