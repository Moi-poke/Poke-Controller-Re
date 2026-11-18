#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Blocklyエディタの起動と保存受け口（tkinter）。

実保存は services.blockly_save が持ち、ここでは配信・起動・
終了時の作り直しだけにする。保存ハンドラ（サーバスレッド）では
tkinterを触らず、ファイル書きの結果だけをJSONで返す。再読込は
エディタを閉じた後のGUIスレッドで行う（実行中は reloadCommands
側も塞いでいるが、入口でも断つ）。
"""

from __future__ import annotations

import functools
import http.server
import itertools
import math
import queue
import socket
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from pokecon.runtime import (
    blockly_live,
    blockly_record,
    blockly_run,
    blockly_save,
)
from pokecon.ui import WindowUtils as WindowUtils, input_dialog as input_dialog
from pokecon.ui.blockly import (
    BlocklyLiveRoutesMixin,
    BlocklyMediaRoutesMixin,
    BlocklyWorkspaceRoutesMixin,
    save_blockly_gyro,
)

_BLOCKLY_DIR = Path(WindowUtils.APP_DIR) / "assets" / "blockly"


def _save_blockly_gyro(
    take: tuple[float, list[tuple[float, tuple[int, int, int, int, int, int]]]],
    origin: float | None,
    end: float | None = None,
) -> dict[str, Any] | None:
    """記録中の IMU サンプルを .pgyro に書く（実体は ui/blockly/gyro_export.py）。"""
    return save_blockly_gyro(WindowUtils.APP_DIR, take, origin, end)


#: POST本文の上限（/save・/delete・/template用）。localhost用の安全弁。
MAX_BODY_BYTES = 5 * 1024 * 1024
#: アップロード付き照合（/match source=upload）用の上限。
#: 10MBの生画像がbase64で約13.4MBになるため余裕を見る。
MAX_UPLOAD_BODY_BYTES = 16 * 1024 * 1024

#: 待ち受けの決め打ち番号。毎回同じ番地（オリジン）にすると、ブラウザ内の
#: 編集控え・画面設定（localStorage）が次回も使える。塞がっていれば任意番号。
PREFERRED_PORT = 51793


class _ExclusiveServer(http.server.ThreadingHTTPServer):
    """番号の横取りをしない待ち受け。

    既定の SO_REUSEADDR は Windows では使用中の番号にも重ねて bind できて
    しまうため切り、Windows では排他指定を付ける（塞がっていれば素直に失敗）。
    """

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


def _bind_server() -> http.server.ThreadingHTTPServer:
    """決め打ち番号で待ち受け、塞がっていれば任意番号へ落とす。"""
    handler = functools.partial(_Handler)
    try:
        return _ExclusiveServer(("127.0.0.1", PREFERRED_PORT), handler)
    except OSError:
        return _ExclusiveServer(("127.0.0.1", 0), handler)


_server: http.server.ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None

_GET_FRAME: Callable[[], bytes | None] | None = None

#: 試し実行の司令塔（本体の PokeControllerApp）。開始・停止はこれの手順を
#: GUI スレッドで呼ぶ。無ければ（本体と繋がっていなければ）試し実行しない。
_RUN_HOST: Any = None

#: GUI スレッドへ頼む仕事の待ち行列。受け口のスレッドは Tk を触れないため、
#: 積んで結果を待つ。取り出すのはエディタの窓の見回り（after）。
_GUI_CALLS: queue.Queue[tuple[Callable[[], Any], Future[Any]]] = queue.Queue()

#: GUI スレッドの応答を待つ上限(秒)。本体が固まっていても受け口は返す。
GUI_CALL_TIMEOUT_S = 5.0

#: `/run/state` が名乗る機能。ブラウザ側は見て分岐できる（今は表示に使わない）。
RUN_CAPABILITIES = [
    "boundedTrial",
    "runIdControl",
    "inputAnswers",
    "awaitingInput",
]

#: runId が無い・古いときに返す理由（別の試し実行へ切り替わった）。
RUN_SWITCHED_MESSAGE = "試し実行が切り替わったため操作しません"

#: `/run/control` が受ける操作名。
_CONTROL_ACTIONS = frozenset({"stop", "pause", "resume", "step", "breakpoints"})


class _Trial:
    """いまの（直近の）試し実行。様子と、走らせたクラスの組。"""

    def __init__(self, session: blockly_run.TrialSession, cls: type) -> None:
        self.session = session
        self.cls = cls


_TRIAL: _Trial | None = None
#: 開いているエディタとCLIをつなぐライブ編集の仲介。
_LIVE = blockly_live.LiveBridge()
_RUN_IDS = itertools.count(1)
#: 区切り（ブレークポイント）の既定。次の実行にも引き継ぐ。
_BREAKPOINTS: frozenset[str] = frozenset()

#: 本体の操作の記録（マクロ記録）。送信線（host.serial.sender.transport）に
#: 付けて PRESS/RELEASE を溜める。付け外しは GUI スレッドで行う。
_RECORDER = blockly_record.Recorder()
_RECORD_BLOCKED: Callable[[], str | None] | None = None


def _set_record_blocked(predicate: Callable[[], str | None] | None) -> None:
    """記録の開始可否の判定口を差し替える（開く時・検証用）。"""
    global _RECORD_BLOCKED
    _RECORD_BLOCKED = predicate


def _set_run_host(host: Any) -> None:
    """試し実行の司令塔を差し替える（開く時・検証用）。"""
    global _RUN_HOST
    _RUN_HOST = host


def _call_on_gui(fn: Callable[[], Any]) -> Any:
    """fn を GUI スレッドで走らせ、結果を待って返す（例外もそのまま投げ直す）。"""
    fut: Future[Any] = Future()
    _GUI_CALLS.put((fn, fut))
    try:
        return fut.result(timeout=GUI_CALL_TIMEOUT_S)
    except TimeoutError:
        # 取り下げられれば、後で黙って走らせない（断ったのに始まるのを防ぐ）。
        if fut.cancel():
            raise
        # もう走り始めていて取り下げられない。終わりを待って結果を使う。
        return fut.result()


def _follow_record_transport() -> None:
    """記録中に本体の送信線が差し替わっていたら、記録を新しい線へ移す。

    GUI スレッドの見回りから呼ぶ（本体の Sender は GUI スレッドで線を替える）。
    """
    if not _RECORDER.recording:
        return
    host = _RUN_HOST
    sender = getattr(getattr(host, "serial", None), "sender", None)
    transport = getattr(sender, "transport", None)
    if transport is not None:
        _RECORDER.follow(transport)


def is_recording() -> bool:
    """いま操作を記録中かを返す（通常コマンド側の開始拒否用）。"""
    try:
        return bool(_RECORDER.recording)
    except Exception:
        return False


def _observe_record_owners() -> None:
    """記録中に本体側の操作が混ざっていないか見る（汚染 latch 用）。

    GUI スレッドの見回りから呼ぶ。sender の所有者の写しと調停の様子を
    渡す。持たない線では握って何もしない（記録自体は続ける）。
    """
    if not _RECORDER.recording:
        return
    host = _RUN_HOST
    sender = getattr(getattr(host, "serial", None), "sender", None)
    owners: dict[str, Any] | None = None
    arbitration: dict[str, Any] | None = None
    get_owners = getattr(sender, "getOwners", None)
    if callable(get_owners):
        try:
            got = get_owners()
            owners = got if isinstance(got, dict) else None
        except Exception:
            owners = None
    get_arb = getattr(sender, "getArbitration", None)
    if callable(get_arb):
        try:
            got_arb = get_arb()
            arbitration = got_arb if isinstance(got_arb, dict) else None
        except Exception:
            arbitration = None
    try:
        _RECORDER.observe_owners(owners, arbitration)
    except Exception:
        pass


def drain_gui_calls() -> None:
    """待ち行列の仕事を片付ける。GUI スレッドの見回りから呼ぶ。

    片付けたあとに時間上限（時間制限つき試し実行）を見る。期限は GUI スレッドで
    しか判定しないため、受け口のスレッドからは触らない。
    """
    while True:
        try:
            fn, fut = _GUI_CALLS.get_nowait()
        except queue.Empty:
            break
        if not fut.set_running_or_notify_cancel():
            continue
        try:
            fut.set_result(fn())
        except Exception as e:
            fut.set_exception(e)
    _enforce_trial_deadline()


def _our_command(trial: _Trial | None) -> Any:
    """走っている命令が、この試し実行のものならそれを返す。"""
    host = _RUN_HOST
    if trial is None or host is None:
        return None
    runner = getattr(host, "runner", None)
    cmd = getattr(runner, "running_command", None)
    if cmd is not None and type(cmd) is trial.cls:
        return cmd
    return None


def _enforce_trial_deadline() -> None:
    """時間上限に達した試し実行を止める（GUI スレッドから呼ぶ）。

    自分の試し実行が走っているときだけ効く。通常のコマンドや、別の実行へ
    切り替わったあとの古い期限では何もしない。停止は本体と同じ停止経路
    （host.stopPlay）へ頼み、二度は頼まない（期限を消してから呼ぶ）。
    """
    trial = _TRIAL
    if trial is None:
        return
    session = trial.session
    deadline = session.deadline
    if deadline is None or session.timed_out:
        return
    cmd = _our_command(trial)
    if cmd is None or not getattr(cmd, "alive", True):
        return
    if time.monotonic() < deadline:
        return
    # 先に印を付ける（stopPlay の中で見回りが再入しても二度呼ばない）。
    session.timed_out = True
    session.stop_requested = True
    session.deadline = None
    host = _RUN_HOST
    if host is None:
        return
    try:
        host.stopPlay()
    except Exception:
        pass


def _stop_trial_for_close() -> None:
    """閉じる前に、時間上限つきの自分の試し実行へ停止を頼む（GUI スレッドから）。

    上限なしの試し実行は従来どおり閉じても走り続ける（本体側で止められる）。
    停止は本体の停止ボタンと同じ経路（host.stopPlay）を使う。
    """
    trial = _TRIAL
    if trial is None:
        return
    session = trial.session
    if session.deadline is None:
        return
    cmd = _our_command(trial)
    if cmd is None or not getattr(cmd, "alive", True):
        return
    session.stop_requested = True
    session.deadline = None
    host = _RUN_HOST
    if host is None:
        return
    try:
        host.stopPlay()
    except Exception:
        pass


def _run_state(since: int) -> dict[str, Any]:
    """編集画面へ返す試し実行の様子。"""
    host = _RUN_HOST
    trial = _TRIAL
    busy = False
    if host is not None:
        try:
            busy = bool(host.runner.is_busy())
        except Exception:
            busy = False
    cmd = _our_command(trial)
    if (
        trial is not None
        and cmd is None
        and trial.session.running
        and not trial.session.awaiting_input
    ):
        # 結果は後始末の手前で決まるため、走り終えたなら実行器が空く前に出ている。
        # 結果が無いまま実行器が手放した（止まらない作業を打ち切った）ときは、
        # 編集画面が「実行中」のまま待ち続けないよう停止として閉じる。
        # 入力待ちの間は実行器に渡していないので、ここでは閉じない。
        trial.session.end("停止")
    if trial is None:
        state: dict[str, Any] = {
            "runId": 0,
            "running": False,
            "result": "",
            "blockId": "",
            "error": "",
            "errorBlock": "",
            "logs": [],
            "vars": [],
            "timedOut": False,
            "awaitingInput": False,
            "inputStatus": "none",
            "inputs": {},
            "inputSources": {},
        }
    else:
        state = trial.session.snapshot(since)
    paused = False
    if cmd is not None:
        try:
            paused = bool(cmd.isPaused())
        except Exception:
            paused = False
    busy = busy or bool(trial is not None and trial.session.awaiting_input)
    state.update(
        {
            "available": host is not None,
            "paused": paused,
            "busy": busy,
            # 本体側で一覧のコマンドが走っている（試し実行は始められない）。
            "otherBusy": busy and cmd is None,
            # 受け口が受ける操作の形（時間上限つき・runId 付き操作）。
            "capabilities": list(RUN_CAPABILITIES),
        }
    )
    return state


class _Handler(
    BlocklyLiveRoutesMixin,
    BlocklyMediaRoutesMixin,
    BlocklyWorkspaceRoutesMixin,
    http.server.SimpleHTTPRequestHandler,
):
    """静的配信＋`/save`・ライブ編集・試し実行・記録の受け口。ログは出さない。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(_BLOCKLY_DIR), **kwargs)

    def log_message(self, *args: Any) -> None:
        return

    def end_headers(self) -> None:
        # アプリ更新後に古い資産・一覧をブラウザが使い回さないよう、都度取り直させる。
        self.send_header("Cache-Control", "no-store")
        # 他サイトの iframe への埋め込みを断る（重ねたクリック誘導で削除等をさせない）。
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        super().end_headers()

    def do_GET(self) -> None:
        """API 受け口へ振り分け、該当しなければ静的配信に任せる。"""
        if self._refuse_foreign(is_post=False):
            return
        path = urllib.parse.urlsplit(self.path).path
        routes = {
            "/list": self._handle_list,
            "/load": self._handle_load,
            "/samples": self._handle_samples,
            "/templates": self._handle_templates,
            "/run/state": self._handle_run_state,
            "/live/state": self._handle_live_state,
            "/live/next": self._handle_live_next,
            "/frame": self._handle_frame,
            "/template_image": self._handle_template_image,
        }
        handler = routes.get(path)
        if handler is not None:
            handler()
            return
        super().do_GET()

    def do_POST(self) -> None:
        """JSON 本文を読み取り、ルートごとの処理へ振り分ける。"""
        if self._refuse_foreign(is_post=True):
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/record/start":
            if self._read_json() is not None:
                self._start_record()
            return
        if path == "/record/stop":
            if self._read_json() is not None:
                self._stop_record()
            return
        routes: dict[str, tuple[int, Any]] = {
            "/live/snapshot": (MAX_BODY_BYTES, self._live_snapshot),
            "/live/result": (MAX_BODY_BYTES, self._live_result),
            "/live/patch": (MAX_BODY_BYTES, self._live_patch),
            "/run": (MAX_BODY_BYTES, self._start_trial),
            "/run/control": (MAX_BODY_BYTES, self._control_trial),
            "/delete": (MAX_BODY_BYTES, self._handle_delete),
            "/save": (MAX_BODY_BYTES, self._handle_save),
            "/template": (MAX_BODY_BYTES, self._handle_save_template),
            "/match": (MAX_UPLOAD_BODY_BYTES, self._handle_match),
            "/color_ratio": (MAX_UPLOAD_BODY_BYTES, self._handle_color_ratio),
            "/filter_preview": (MAX_UPLOAD_BODY_BYTES, self._handle_filter_preview),
        }
        entry = routes.get(path)
        if entry is None:
            self.send_error(404)
            return
        max_bytes, handler = entry
        payload = self._read_json(max_bytes)
        if payload is not None:
            handler(payload)


def _as_ids(value: Any) -> frozenset[str]:
    """区切りのID一覧（JSON配列）を集合にする。形が違えば空。"""
    if not isinstance(value, list):
        return frozenset()
    return frozenset(str(v) for v in value if isinstance(v, str) and v)


def _parse_max_seconds(payload: dict[str, Any]) -> tuple[bool, float | None, str]:
    """試し実行の時間上限を読む。無ければ (True, None, "")。

    あるときは正の有限の数だけを受ける（真偽値・文字列・NaN・無限・0以下は断る）。
    ブラウザから来ない形でも、受け口は自分で確かめる。
    """
    if "maxSeconds" not in payload:
        return (True, None, "")
    value = payload.get("maxSeconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return (False, None, "時間上限は秒数（正の数）で書いてください")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        return (False, None, "時間上限は0より大きい有限の秒数で書いてください")
    return (True, seconds, "")


def _is_run_id(value: Any) -> bool:
    """runId として使える正の整数か（真偽値は整数扱いしない）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _py_dir_mtime() -> float:
    """保存先フォルダの更新時刻。読めなければ 0.0（比較不能＝変化なし扱い）。"""
    try:
        return (
            Path(WindowUtils.APP_DIR, *blockly_save.PY_DIR_REL.split("/"))
            .stat()
            .st_mtime
        )
    except OSError:
        return 0.0


class _ReloadWatch:
    """保存先の変化を見て、一覧の作り直しが要るかを決める。

    実行中は作り直せない（reloadCommands 側も断る）。変化を見た時点で
    「見た」ことにすると、実行中に保存した分を取りこぼすため、作り直せた
    ときにだけ見た時刻を進める。
    """

    def __init__(self, mtime: Callable[[], float] = lambda: _py_dir_mtime()) -> None:
        self._mtime = mtime
        self._seen = mtime()

    def tick(self, busy: bool) -> bool:
        """作り直すべきなら真（呼び出し側が作り直す）。"""
        try:
            cur = self._mtime()
        except Exception:
            return False
        if cur == self._seen or busy:
            return False
        self._seen = cur
        return True


#: 見回りの間隔(ms)。試し実行の操作（停止・一時停止）を待たせないよう短くする。
POLL_MS = 100
#: 保存先を見に行く間隔（見回り何回ごとか）。約1秒。
RELOAD_EVERY_TICKS = 10


def _stop_server() -> None:
    """待ち受けを止める。何度呼んでもよい。

    GUIスレッドを固めないよう shutdown/close だけここで行い、
    join は短命のdaemon番兵に任せる（serve側もdaemonのため
    万一残っても放置でよい）。

    記録が付きっぱなしにならないよう、ここで記録中なら外す。
    エディタの窓を閉じたときに受け口の多重起動で残らないようにする。
    """
    global _server, _thread
    try:
        # 送信線の聞き手を外すだけなので、どのスレッドからでもよい。
        _RECORDER.stop()
    except Exception:
        pass
    server, _server = _server, None
    if server is not None:
        server.shutdown()
        server.server_close()
    thread, _thread = _thread, None
    if thread is not None:

        def _join() -> None:
            thread.join(timeout=5.0)

        threading.Thread(target=_join, name="BlocklyJoin", daemon=True).start()


def stop_blockly_editor() -> None:
    """Menubar.closeAllから呼ぶ。開きっぱなしでの終了を防ぐ。

    閉じる前に、時間上限つきの試し実行が走っていれば止める（GUI スレッドから
    呼ばれる）。上限なしの試し実行は従来どおり本体側で止める。
    """
    _stop_trial_for_close()
    _stop_server()


def open_blockly_editor(
    root: Any,
    *,
    is_busy: Callable[[], bool],
    reload_commands: Callable[[], None],
    get_frame: Callable[[], bytes | None] | None = None,
    run_host: Any = None,
    record_blocked: Callable[[], str | None] | None = None,
) -> None:
    """エディタを開く。終わったら一覧を作り直す。

    run_host は試し実行の司令塔（本体）。start_external_command・stopPlay・
    set_running_paused・syncPauseState・runner を持つ。無ければ試し実行しない。
    record_blocked は記録の開始可否（拒否理由、無ければ None）。無ければ
    試し実行中だけを見て、通常コマンド実行中は断らない（従来どおり）。
    """
    import tkinter as tk
    import tkinter.messagebox as tkmsg

    if is_busy():
        print("実行中はBlocklyエディタを開けません")
        tkmsg.showwarning("Blockly", "実行中はBlocklyエディタを開けません", parent=root)
        return
    global _GET_FRAME
    _GET_FRAME = get_frame
    _set_run_host(run_host)
    _set_record_blocked(record_blocked)
    global _server, _thread
    _server = _bind_server()
    port = _server.server_address[1]
    _thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _thread.start()
    url = f"http://127.0.0.1:{port}/editor.html"
    webbrowser.open(url)

    win = tk.Toplevel(root)
    win.title("Blocklyエディタ")
    win.resizable(False, False)
    tk.Label(win, text=url).pack(padx=10, pady=10)

    def copy_url() -> None:
        """起動URLをクリップボードへ入れる（自動で開かない環境用）。"""
        try:
            win.clipboard_clear()
            win.clipboard_append(url)
        except Exception:
            pass

    def reopen() -> None:
        """タブを閉じてしまったときに開き直す（編集中の控えはブラウザ側に残る）。"""
        try:
            webbrowser.open(url)
        except Exception:
            pass

    buttons = tk.Frame(win)
    buttons.pack(padx=10, pady=(0, 10))
    tk.Button(buttons, text="ブラウザで開く", command=reopen).pack(side=tk.LEFT, padx=4)
    tk.Button(buttons, text="URLをコピー", command=copy_url).pack(side=tk.LEFT, padx=4)

    watch = _ReloadWatch()
    ticks = 0

    def poll() -> None:
        """GUI スレッドの見回り。受け口から頼まれた仕事と一覧の作り直しを行う。

        サーバスレッドから tkinter を触れないため、試し実行の開始・停止は
        待ち行列で受け、ここで片付ける。保存先の変化は約1秒ごとに見る。
        窓が閉じたら連鎖を止める。
        """
        nonlocal ticks
        try:
            alive = bool(win.winfo_exists())
        except Exception:
            return
        if not alive:
            return
        try:
            drain_gui_calls()
        except Exception:
            pass
        try:
            _follow_record_transport()
        except Exception:
            pass
        try:
            _observe_record_owners()
        except Exception:
            pass
        sync = getattr(run_host, "syncPauseState", None)
        if callable(sync):
            try:
                sync()
            except Exception:
                pass
        ticks += 1
        if ticks >= RELOAD_EVERY_TICKS:
            ticks = 0
            if watch.tick(busy=is_busy()):
                reload_commands()
        try:
            win.after(POLL_MS, poll)
        except Exception:
            pass

    def on_close() -> None:
        # 閉じたあと止められなくなるため、上限つきの試し実行はここで止める。
        _stop_trial_for_close()
        _stop_server()
        try:
            win.destroy()
        except Exception:
            pass
        if not is_busy():
            reload_commands()

    win.protocol("WM_DELETE_WINDOW", on_close)
    win.after(POLL_MS, poll)
