"""switch-bcon-proc が本家 bcon と同じ操作面を保つことを固定する。"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from pokecon.domain.transport import bcon_proc
from pokecon.domain.transport.bcon import BconTransport
from pokecon.domain.transport.bcon_proc import _ALLOWED_CALLS, BconProcTransport
from pokecon.domain.transport.bcon_protocol import BAUD_TABLE

#: proc 側が自前で持つ（本家へ転送しない）公開メンバー。
#: 転送するのは会話系（hello/config/last_* など）だけで、フレーム送出と
#: 受信ポンプは proc 側が自分のループで回す。ここを取り違えると
#: 「子プロセスが実行できない名前を許可リストへ入れる」ことになる。
LOCAL_ONLY = {
    "isOpened",
    "proc_alive",
    "rpc_stats",
    "close",
    "get_raw_serial",
    "open",
    "last_open_error",
    "send_row",
    "flush_pending",
    "add_listener",
    "remove_listener",
    # IMUの聞き手は親の一覧へ積むだけ（子へ渡すコールバックは無い）。
    "add_imu_listener",
    "remove_imu_listener",
    "set_hooks",
    "start_rx_pump",
    # IMU loopは親側で回す（sourceがコールバックで子へ渡せない）。
    "start_imu_loop",
    "stop_imu_loop",
    "imu_loop_running",
    "imu_loop_owner",
    "imu_stats",
    "stop_rx_pump",
    "rx_pump_running",
    "subscribe_rx",
    "wait_rx",  # 引数が本家と違う（別途揃えるか要判断）
    "acquire_write_lock",
}


def _public(cls: type) -> set[str]:
    return {n for n, _ in inspect.getmembers(cls) if not n.startswith("_")}


def test_proc_exposes_every_public_member_of_the_direct_transport() -> None:
    """Given: 本家と proc の 2 方式 / When: 公開メンバーを比べる / Then: 欠けが無いこと。"""
    missing = _public(BconTransport) - _public(BconProcTransport)
    # last_rumble の欠落で振動表示が必ず「（なし）」になった実例がある。
    assert not missing, f"proc に無い公開メンバー: {sorted(missing)}"


def test_forwarded_members_are_allowed_by_the_child_rpc() -> None:
    """Given: 本家だけが持つ公開メソッド / When: 子プロセスの許可リストと比べる / Then: すべて許可されていること。"""
    forwarded = {
        name
        for name in _public(BconTransport) - LOCAL_ONLY
        if callable(getattr(BconTransport, name))
        and not isinstance(getattr(BconTransport, name), property)
    }
    missing = sorted(forwarded - set(_ALLOWED_CALLS))
    assert not missing, f"許可リストに無いので子プロセスが実行できない: {missing}"


def test_signatures_of_forwarded_members_match() -> None:
    """Given: 子プロセスへ転送する公開メソッド / When: 引数を比べる / Then: 一致すること。"""
    mismatched: list[str] = []
    forwarded = sorted(
        set(_ALLOWED_CALLS) & _public(BconTransport) & _public(BconProcTransport)
    )
    for name in forwarded:
        direct = getattr(BconTransport, name)
        proc = getattr(BconProcTransport, name)
        if not (callable(direct) and callable(proc)):
            continue
        if isinstance(direct, property) or isinstance(proc, property):
            continue
        if list(inspect.signature(direct).parameters) != list(
            inspect.signature(proc).parameters
        ):
            mismatched.append(name)
    assert not mismatched, f"引数が食い違う: {mismatched}"


def test_local_only_names_have_an_implementation_on_the_proc_transport() -> None:
    """Given: proc 側が自前で持つと宣言した名前 / When: 実体を引く / Then: すべて実在すること。"""
    missing = sorted(
        name for name in LOCAL_ONLY if not hasattr(BconProcTransport, name)
    )
    assert not missing, f"宣言だけで実装が無い: {missing}"


def test_binary_transports_expose_no_raw_serial() -> None:
    """Given: バイナリ方式の 2 実装 / When: 生シリアルを問う / Then: どちらも None を返すこと。"""
    # ASCII の M をバイナリ列へ混ぜない契約（run_journal.raw_serial_for_m が使う）。
    assert BconTransport.get_raw_serial(object.__new__(BconTransport)) is None
    assert BconProcTransport.get_raw_serial(object.__new__(BconProcTransport)) is None


def test_keyboard_round_trips_through_child_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given: 生存中の子 / When: 8Bを送る / Then: 子のwrite結果をRPCで受け取ること。"""
    made = BconProcTransport()
    calls: list[tuple[str, tuple, float]] = []

    def fake_call(
        method: str,
        args: tuple = (),
        timeout: float | None = None,
        margin: float | None = None,
    ) -> tuple[bool, object]:
        calls.append((method, tuple(args), float(timeout or 0.0)))
        return True, True

    monkeypatch.setattr(made, "_call", fake_call)
    body = bytearray([0, 0, 4, 0, 0, 0, 0, 0])
    assert made.send_keyboard(body)
    body[2] = 5
    # 呼出し側の bytearray を書き換えても送った内容は変わらない（写しを渡す）。
    assert calls == [("send_keyboard", (bytes([0, 0, 4, 0, 0, 0, 0, 0]),), 2.0)]
    assert "send_keyboard" in _ALLOWED_CALLS


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ((True, True), True),
        ((True, False), False),
        ((True, None), False),
        ((False, None), False),
        ((False, True), False),
    ],
)
def test_keyboard_returns_child_write_outcome(
    monkeypatch: pytest.MonkeyPatch,
    reply: tuple[bool, object],
    expected: bool,
) -> None:
    """Given: 子の応答が成功/拒否/エラー / When: キーボードを送る / Then: 子の結果だけを返すこと。"""
    made = BconProcTransport()
    monkeypatch.setattr(made, "_call", lambda *a, **k: reply)
    assert made.send_keyboard(bytes(8)) is expected


@pytest.mark.parametrize(
    "body", [b"", bytes(7), bytes(9), None, 8, "12345678", [256] * 8]
)
def test_keyboard_rejects_bad_report_before_rpc(
    monkeypatch: pytest.MonkeyPatch, body: object
) -> None:
    """Given: 8Bでないレポート / When: 送る / Then: RPCを出さずFalseを返すこと。"""
    made = BconProcTransport()
    calls: list[object] = []

    def call(*args: object, **kwargs: object) -> tuple[bool, bool]:
        calls.append(args)
        return True, True

    monkeypatch.setattr(made, "_call", call)
    assert made.send_keyboard(body) is False  # type: ignore[arg-type]
    assert calls == []


def test_keyboard_rejects_dead_worker_and_unavailable_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given: 子が死んでいる/指令キューが無い / When: 送る / Then: Falseで落ちないこと。"""
    made = BconProcTransport()
    assert not made.send_keyboard(bytes(8))
    monkeypatch.setattr(made, "_worker_alive", lambda: True)
    # 子が生存していても指令キューが無ければRPCを送れない。
    assert not made.send_keyboard(bytes(8))


@pytest.mark.parametrize(
    ("candidates", "per"),
    [
        (None, 0.5),
        (list(BAUD_TABLE.values()), 0.5),
        (list(BAUD_TABLE.values()), 2.0),
        (None, float("nan")),
        (None, float("inf")),
        (None, "不正"),
        ([115200, 1000000] * 8, 2.0),
    ],
)
def test_baud_hunt_rpc_timeout_covers_child_scan_and_upgrade(
    candidates: Any, per: Any
) -> None:
    """Given: 候補と timeout_per / When: RPC待ちを見積もる / Then: scan+昇格/復帰に足りること。"""
    limit = bcon_proc._baud_hunt_rpc_timeout(candidates, per)
    # 子の最悪: 候補ごとに quick HELLO 0.15s＋呼び水 0.7s＋HELLO 2回(最大per=2.0)、
    # lock 後の昇格2.0s＋復帰確認（HELLO 0.5s×2＋呼び水0.7s）。
    attempts = len(candidates) if candidates is not None else len(BAUD_TABLE)
    bound = float(attempts) * (0.15 + 0.7 + 2.0 * 2.0) + 2.0 + 0.5 * 2 + 0.7
    # 以前の固定30sを上回り、昇格込みの最悪見積もりにも余裕があること。
    assert limit > 30.0
    assert limit >= bound


def test_baud_hunt_passes_candidates_and_derived_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given: 候補指定 / When: baud_huntする / Then: 子へそのまま渡し、見積もった待ちで待つこと。"""
    made = BconProcTransport()
    seen: dict[str, Any] = {}

    def fake_call(
        method: str,
        args: tuple = (),
        timeout: float | None = None,
        margin: float | None = None,
    ) -> tuple[bool, object]:
        seen.update(method=method, args=args, timeout=timeout)
        return True, 1000000

    monkeypatch.setattr(made, "_call", fake_call)
    candidates = [115200, 1000000]
    assert made.baud_hunt(candidates, timeout_per=0.5) == 1000000
    assert seen["method"] == "baud_hunt"
    assert seen["args"] == (candidates, 0.5)
    assert seen["timeout"] == bcon_proc._baud_hunt_rpc_timeout(candidates, 0.5)
