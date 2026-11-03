"""入力ダイアログと回復ダイアログの実物経路（実機なし）。

CommandDialog のうち、これまで一度も実行されていなかった
  - DialogMixin.ask_recovery（パネル表示・コピー・再試行/中止）
  - DialogMixin.dialogue6widget → PokeConDialogue.__init__ / ret_value
  - ok_command / cancel_command
を、実 Tk の窓を出して確かめる。操作は root.after で駆動し、
wait_window の中のイベントループで押させる（実時間の寝待ちはしない）。
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
import tkinter.ttk as ttk
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from Commands import CommandDialog as D
from Commands.CommandDialog import PokeConDialogue
from pokecon.domain.recovery import RecoveryAdvice


class _Cmd(D.DialogMixin):
    """実 Tk の根を GUI 側として渡し、待ち幅を詰めたコマンド。"""

    def __init__(self, root: tk.Tk) -> None:
        self.gui_root = root
        self.message_dialogue: Any = None
        self._stop_event = threading.Event()
        self._DIALOGUE_WAIT_SLICE = 0.005
        self._DIALOGUE_CLOSE_WAIT = 0.2
        self._DIALOGUE_OPEN_TIMEOUT = 1.0
        self._RECOVERY_OPEN_TIMEOUT = 1.0

    def checkIfAlive(self) -> bool:
        return True


def _walk(widget: tk.Misc) -> Iterator[tk.Misc]:
    """子孫ウィジェットを深さ優先で返す。"""
    for child in widget.winfo_children():
        yield child
        yield from _walk(child)


def _toplevels(root: tk.Tk, title: str) -> list[tk.Toplevel]:
    """指定タイトルの生きている Toplevel を返す。"""
    found: list[tk.Toplevel] = []
    for widget in root.winfo_children():
        if not isinstance(widget, tk.Toplevel):
            continue
        try:
            if widget.winfo_exists() and str(widget.title()) == title:
                found.append(widget)
        except tk.TclError:  # 破棄済み
            continue
    return found


def _buttons(top: tk.Misc) -> dict[str, ttk.Button]:
    """ボタンの表示文字で引けるようにする。"""
    return {
        str(widget.cget("text")): widget
        for widget in _walk(top)
        if isinstance(widget, ttk.Button)
    }


def _labels(top: tk.Misc) -> list[str]:
    return [
        str(widget.cget("text"))
        for widget in _walk(top)
        if isinstance(widget, (ttk.Label, tk.Label))
    ]


def _destroy_all(root: tk.Tk, title: str) -> None:
    for top in _toplevels(root, title):
        try:
            top.destroy()
        except tk.TclError:
            pass


def _drive(
    root: tk.Tk,
    title: str,
    action: Callable[[tk.Toplevel], None],
    timeout: float = 5.0,
) -> dict[str, Any]:
    """after の輪で対話を探し、見つかったら action を呼ぶ。

    action が失敗しても窓を閉じる。閉じないまま待つと wait_window が
    永久に戻らず、テストがハングするため。失敗は state に残す。
    """
    state: dict[str, Any] = {"done": False, "error": None}
    deadline = time.monotonic() + timeout

    def step() -> None:
        if state["done"]:
            return
        found = _toplevels(root, title)
        if not found:
            if time.monotonic() >= deadline:
                state["error"] = AssertionError(f"ダイアログが現れません: {title}")
                state["done"] = True
                return
            root.after(10, step)
            return
        try:
            action(found[0])
        except BaseException as exc:  # アサート失敗も待ちを解くために捕まえる
            state["error"] = exc
        finally:
            state["done"] = True
            _destroy_all(root, title)

    root.after(10, step)
    return state


def _run_in_worker(root: tk.Tk, call: Callable[[], Any], timeout: float = 5.0) -> Any:
    """作業スレッドで対話を呼び、主スレッドでイベントを回して待つ。"""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # 呼び出し側で型ごと確認する
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout
    while worker.is_alive() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.005)
    worker.join(0.2)
    assert not worker.is_alive(), "対話が終わりません"
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _advice(tmp_path: Any) -> RecoveryAdvice:
    return RecoveryAdvice(
        title="テンプレート画像がありません",
        detail="画像を置いてから再試行してください。",
        hint="Template 配下に置きます。",
        path=str(tmp_path / "Template" / "a.png"),
        copy_text="pokecon テンプレート読み込みエラー\n対象: a.png",
        open_path=str(tmp_path / "Template"),
    )


def test_ask_recovery_shows_path_copy_and_returns_true(
    tk_root: tk.Tk, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: パス・コピー文・開く先を持つ回復案内
    advice = _advice(tmp_path)
    opened: list[str] = []

    def fake_open(path: str) -> bool:
        opened.append(path)
        return False

    monkeypatch.setattr(D, "_open_folder", fake_open)
    cmd = _Cmd(tk_root)
    seen: dict[str, Any] = {}

    def action(top: tk.Toplevel) -> None:
        labels = _labels(top)
        seen["labels"] = labels
        path_rows = [w for w in _walk(top) if isinstance(w, tk.Text)]
        assert path_rows, "パスの行が無い"
        seen["path"] = path_rows[0].get("1.0", "end-1c")
        buttons = _buttons(top)
        seen["buttons"] = sorted(buttons)
        assert {"再試行", "中止", "フォルダを開く", "エラー内容をコピー"} <= set(
            buttons
        )
        buttons["フォルダを開く"].invoke()
        buttons["エラー内容をコピー"].invoke()
        top.update_idletasks()
        try:
            seen["clipboard"] = top.clipboard_get()
        except tk.TclError as exc:  # クリップボードを持てない環境
            seen["clipboard_error"] = str(exc)
        buttons["再試行"].invoke()

    state = _drive(tk_root, advice.title, action)

    # When
    answer = cmd.ask_recovery(advice)

    # Then: 再試行が True として返り、窓は残らない
    assert state["error"] is None, state["error"]
    assert answer is True
    assert cmd.message_dialogue is None
    assert _toplevels(tk_root, advice.title) == []
    # パスは本文と別の行に出て、案内どおりの操作が並ぶ
    assert seen["path"] == advice.path
    assert "パス" in seen["labels"]
    assert advice.title in seen["labels"]
    assert advice.detail in "".join(seen["labels"])
    assert opened == [advice.open_path]
    if "clipboard_error" in seen:
        pytest.skip(f"clipboard unavailable: {seen['clipboard_error']}")
    assert seen["clipboard"] == advice.copy_text


def test_ask_recovery_from_worker_thread_returns_retry(tk_root: tk.Tk) -> None:
    # Given: 実際のコマンドと同じく作業スレッドから尋ねる
    advice = RecoveryAdvice(title="作業スレッドからの回復案内", detail="直しました")
    D._ensure_gui_poll(tk_root)
    cmd = _Cmd(tk_root)
    seen: dict[str, Any] = {}

    def action(top: tk.Toplevel) -> None:
        seen["buttons"] = sorted(_buttons(top))
        _buttons(top)["再試行"].invoke()

    state = _drive(tk_root, advice.title, action)

    # When: 生成は GUI 側へ積まれ、作業側は結果だけを待つ
    answer = _run_in_worker(tk_root, lambda: cmd.ask_recovery(advice))

    # Then
    assert state["error"] is None, state["error"]
    assert answer is True
    assert seen["buttons"] == ["中止", "再試行"]
    assert cmd.message_dialogue is None
    assert _toplevels(tk_root, advice.title) == []


def test_ask_recovery_abort_returns_false(tk_root: tk.Tk) -> None:
    # Given: パスもコピー文も持たない案内（副ボタンは出ない）
    advice = RecoveryAdvice(title="回復できますか", detail="直してから再試行します。")
    cmd = _Cmd(tk_root)
    seen: dict[str, Any] = {}

    def action(top: tk.Toplevel) -> None:
        buttons = _buttons(top)
        seen["buttons"] = sorted(buttons)
        buttons["中止"].invoke()

    state = _drive(tk_root, advice.title, action)

    # When
    answer = cmd.ask_recovery(advice)

    # Then: 中止は False。余計なボタンを出さない
    assert state["error"] is None, state["error"]
    assert answer is False
    assert seen["buttons"] == ["中止", "再試行"]


def test_ask_recovery_window_close_returns_false(tk_root: tk.Tk) -> None:
    # Given: 利用者が窓の×を押す（WM_DELETE_WINDOW の作法）
    advice = RecoveryAdvice(title="閉じられる回復案内", detail="本文")
    cmd = _Cmd(tk_root)

    def action(top: tk.Toplevel) -> None:
        handler = top.protocol("WM_DELETE_WINDOW")
        assert handler
        top.tk.call(handler)

    state = _drive(tk_root, advice.title, action)

    # When
    answer = cmd.ask_recovery(advice)

    # Then: 閉じた場合は中止として False
    assert state["error"] is None, state["error"]
    assert answer is False
    assert _toplevels(tk_root, advice.title) == []


def _record_dialogues(
    monkeypatch: pytest.MonkeyPatch,
) -> list[PokeConDialogue]:
    """生成された PokeConDialogue を、待ちに入る前に記録する。"""
    created: list[PokeConDialogue] = []
    original = PokeConDialogue.__init__

    def record(self: PokeConDialogue, *args: Any, **kwargs: Any) -> None:
        created.append(self)
        original(self, *args, **kwargs)

    monkeypatch.setattr(D.PokeConDialogue, "__init__", record)
    return created


def test_dialogue6widget_returns_dict_of_field_values(
    tk_root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: 6 種の入力欄（check/combo/entry/colorentry/radio/scale）
    created = _record_dialogues(monkeypatch)
    D._ensure_gui_poll(tk_root)
    cmd = _Cmd(tk_root)
    config = [
        ["check", "有効", True],
        ["combo", "選択", ["a", "b", "c"], "a"],
        ["entry", "名前", "初期"],
        ["colorentry", "色", "#000000"],
        ["radio", "動物", ["dog", "cat"], "dog"],
        ["scale", "強さ", 0, 10, 5, 0],
    ]
    seen: dict[str, Any] = {}

    def action(top: tk.Toplevel) -> None:
        assert created, "PokeConDialogue が作られていない"
        dlg = created[0]
        seen["fields"] = list(dlg.dialogue_ls)
        dlg.dialogue_ls["有効"].set(False)
        dlg.dialogue_ls["選択"].set("b")
        dlg.dialogue_ls["名前"].set("ピカチュウ")
        dlg.dialogue_ls["色"].set("#12ab34")
        dlg.dialogue_ls["動物"].set("cat")
        dlg.dialogue_ls["強さ"].set(7)
        dlg.OK.invoke()

    state = _drive(tk_root, "六つの入力", action)

    # When: 作業スレッドから呼び、GUI 側の取り出し輪に作らせる
    got = _run_in_worker(
        tk_root, lambda: cmd.dialogue6widget("六つの入力", config, need=dict)
    )

    # Then: 欄の見出しを鍵にした辞書が返る
    assert state["error"] is None, state["error"]
    assert seen["fields"] == ["有効", "選択", "名前", "色", "動物", "強さ"]
    assert got == {
        "有効": False,
        "選択": "b",
        "名前": "ピカチュウ",
        "色": "#12ab34",
        "動物": "cat",
        "強さ": 7,
    }
    assert cmd.message_dialogue is None


def test_dialogue6widget_cancel_returns_none(
    tk_root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: 1 欄だけの対話
    created = _record_dialogues(monkeypatch)
    D._ensure_gui_poll(tk_root)
    cmd = _Cmd(tk_root)
    config = [["entry", "名前", "初期"]]

    def action(top: tk.Toplevel) -> None:
        assert created, "PokeConDialogue が作られていない"
        created[0].Cancel.invoke()

    state = _drive(tk_root, "取り消す対話", action)

    # When
    got = _run_in_worker(tk_root, lambda: cmd.dialogue6widget("取り消す対話", config))

    # Then: Cancel は None（閉じた窓を値として扱わない）
    assert state["error"] is None, state["error"]
    assert got is None


def test_dialogue6widget_timeout_returns_empty(tk_root: tk.Tk) -> None:
    # Given: 取り出し輪が回らない状況（主循環が死んだ状態に相当）
    cmd = _Cmd(tk_root)
    cmd._DIALOGUE_OPEN_TIMEOUT = 0.2
    started = time.monotonic()

    # When: 主スレッドから呼ぶ。done を待つ間イベントは回らない
    got = cmd.dialogue6widget("開かない対話", [["entry", "項目", "初期"]], need=dict)

    # Then: 有界で Cancel の形（dict 指定なら {}）に揃えて戻る
    assert got == {}
    assert time.monotonic() - started < 2.0
    assert cmd.message_dialogue is None
    assert _toplevels(tk_root, "開かない対話") == []


def test_open_folder_missing_path_returns_false(tmp_path: Any) -> None:
    # 無いフォルダで例外にせず False。ダイアログの副ボタンを止めない。
    assert D._open_folder(str(tmp_path / "no-such-dir")) is False
    assert D._open_folder("") is False


def test_copy_to_clipboard_roundtrip(tk_root: tk.Tk) -> None:
    text = "診断: C:\\work\\Template\\a.png"
    try:
        D._copy_to_clipboard(tk_root, text)
        assert tk_root.clipboard_get() == text
    except tk.TclError as exc:  # クリップボードを使えない環境
        pytest.skip(f"clipboard unavailable: {exc}")


def test_dialogue_mode0_returns_the_entry_values(
    tk_root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dialogue()`（mode=0）はラベルを鍵にした Entry の値を返す。"""
    created = _record_dialogues(monkeypatch)
    D._ensure_gui_poll(tk_root)
    cmd = _Cmd(tk_root)
    seen: dict[str, Any] = {}

    def action(top: tk.Toplevel) -> None:
        assert created, "PokeConDialogue が作られていない"
        dlg = created[0]
        seen["fields"] = list(dlg.dialogue_ls)
        dlg.dialogue_ls["名前"].set("ピカチュウ")
        dlg.dialogue_ls["色"].set("赤")
        dlg.OK.invoke()

    state = _drive(tk_root, "二つの入力", action)

    got = _run_in_worker(
        tk_root, lambda: cmd.dialogue("二つの入力", ["名前", "色"], need=dict)
    )

    assert state["error"] is None, state["error"]
    assert seen["fields"] == ["名前", "色"]
    assert got == {"名前": "ピカチュウ", "色": "赤"}
    assert cmd.message_dialogue is None


def test_dialogue_mode0_rejects_duplicate_labels(tk_root: tk.Tk) -> None:
    """同じラベルを2つ渡すと後勝ちで黙って潰れるため、明示的に断る。"""
    cmd = _Cmd(tk_root)

    with pytest.raises(RuntimeError, match="Duplicated label"):
        _run_in_worker(
            tk_root, lambda: cmd.dialogue("重複", ["同じ", "同じ"], need=dict)
        )

    # 生成に失敗したとき、空の窓を残さない
    assert _toplevels(tk_root, "重複") == []
    assert cmd.message_dialogue is None


def test_dialogue6widget_rejects_an_unknown_widget_kind(tk_root: tk.Tk) -> None:
    """種別の綴り違いで欄が黙って消えない（数が合わなくなる）。"""
    cmd = _Cmd(tk_root)

    with pytest.raises(RuntimeError, match="未知の widget 種別"):
        _run_in_worker(
            tk_root,
            lambda: cmd.dialogue6widget(
                "未知", [["checkbox", "有効", True]], need=dict
            ),
        )

    assert _toplevels(tk_root, "未知") == []


def test_dialogue6widget_rejects_a_short_widget_spec(tk_root: tk.Tk) -> None:
    cmd = _Cmd(tk_root)

    with pytest.raises(RuntimeError, match="要素が必要"):
        _run_in_worker(
            tk_root,
            lambda: cmd.dialogue6widget("不足", [["entry", "名前"]], need=dict),
        )

    assert _toplevels(tk_root, "不足") == []
