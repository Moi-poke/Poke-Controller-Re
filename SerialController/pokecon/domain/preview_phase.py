"""表示 tick の位相をカメラの公開周期に合わせる判断。Tk にも Win32 にも触れない。

取込と表示はそれぞれ別の時計で同じ fps を刻むので、2 つの位相がゆっくり
ずれていく。tick がフレームの公開とほぼ同時になる区間では、揺らぎだけで
「まだ前のフレーム（空振り）」と「その間に 2 枚進んだ（1 枚飛ばし）」が
交互に起き、表示 fps が 45 前後まで落ちていた（実測 2026-10-04 16:53）。

刻む周期は変えず（表示クロックの契約は設定 fps のまま）、空振りした tick が
公開境界に重なっていたら「次の公開から半周期後」に 1 回描いて、そこを起点に
据え直す。半周期の余裕があれば揺らぎでは境界を越えないので、次にずれ切る
まで空振りは出ない。

据え直しは 1 tick の追加描画と期限の引き直しを伴う。取込と表示の周期が
近ければ空振りは 1〜2% の割合で必ず出るが、それは「その窓にフレームが
無かった」だけで位相の問題ではないので据え直さない（実測: 実アプリで
1 秒ごとに連発し、表示 fps が 55 まで落ちていた）。
"""

from __future__ import annotations

from typing import Final

# 公開周期の推定が表示周期からこの割合以上離れていたら合わせない。
# 30fps のカメラを 60fps で表示するなら空振りは正常で、据え直しても得る
# ものが無い。
_RATE_TOLERANCE: Final[float] = 0.1
# 公開間隔の指数移動平均の重み。
_PERIOD_EMA_ALPHA: Final[float] = 0.1
# 据え直しの最短間隔。うなりの 1 周は数十秒なので、これより頻繁な要求は
# 揺らぎへの過剰反応として捨てる。
_COOLDOWN_NS: Final[int] = 1_000_000_000
# 据え直すのは、空振りした tick が「次の公開の直前」に居るときだけ。
# 周期のこの割合より手前で空振りしたなら、フレームがまだ来ていないだけで
# 位相の問題ではない（取込と表示の周期が近ければ必ず起きる）。そこへ
# 据え直すと追加描画と期限の引き直しで 1 tick 落とし、かえって引っかかる
# （実測: 実アプリで 1 秒ごとに連発し、表示 55fps）。
_NEAR_BOUNDARY_RATIO: Final[float] = 0.2
# 格子の外の観測は判断材料にしない。下限は据え直し直後の追加描画（同じ seq を
# 見るので空振りと誤認する）、上限は GUI が詰まって遅れて来た tick や、飛ばされた
# tick の次（位相のずれではなく実行の遅れで、据え直すと悪化する）。
_OFF_GRID_RATIO: Final[float] = 0.75
_MAX_GRID_RATIO: Final[float] = 1.75


class PhaseLock:
    """tick ごとの観測から、位相の据え直しが要るかを決める。

    ``observe`` が数値を返したら、その ns 後に 1 回描画して表示クロックを
    据え直すこと（``PreviewClock.realign``）。``None`` なら何もしない。
    """

    def __init__(self, period_ns: int) -> None:
        self._period_ns = period_ns
        self._last_seq: int | None = None
        self._last_ready_ns = 0
        self._source_period_ns: float | None = None
        self._last_realign_ns: int | None = None
        self._last_observe_ns: int | None = None

    def set_period(self, period_ns: int) -> None:
        """表示周期を変える。公開周期の推定は表示周期に依らないので残す。"""
        self._period_ns = period_ns
        self._last_realign_ns = None
        self._last_observe_ns = None

    def reset(self) -> None:
        """カメラの差し替え・停止の後に呼ぶ。過去の観測を捨てる。"""
        self._last_seq = None
        self._last_ready_ns = 0
        self._source_period_ns = None
        self._last_realign_ns = None
        self._last_observe_ns = None

    def observe(self, now_ns: int, seq: int, t_ready_ns: int) -> int | None:
        """1 tick の観測を受け取り、据え直すまでの遅延 ns を返す。

        ``t_ready_ns`` は最新フレームの公開時刻（perf_counter_ns）。0 は
        「記録無し」の印なので、そのフレームでは判断しない。
        """
        if t_ready_ns <= 0 or seq <= 0:
            return None
        last_observe = self._last_observe_ns
        if last_observe is not None:
            gap = now_ns - last_observe
            if not (
                self._period_ns * _OFF_GRID_RATIO
                <= gap
                <= self._period_ns * _MAX_GRID_RATIO
            ):
                return None
        self._last_observe_ns = now_ns
        last_seq = self._last_seq
        if last_seq is None or seq < last_seq:
            # 初回、または clear で seq が巻き戻ったときは基準を取り直す
            self._remember(seq, t_ready_ns)
            self._source_period_ns = None
            return None
        if seq > last_seq:
            interval = (t_ready_ns - self._last_ready_ns) / (seq - last_seq)
            if interval > 0:
                if self._source_period_ns is None:
                    self._source_period_ns = interval
                else:
                    self._source_period_ns += _PERIOD_EMA_ALPHA * (
                        interval - self._source_period_ns
                    )
            self._remember(seq, t_ready_ns)
            return None
        return self._on_miss(now_ns, t_ready_ns)

    def _remember(self, seq: int, t_ready_ns: int) -> None:
        self._last_seq = seq
        self._last_ready_ns = t_ready_ns

    def _on_miss(self, now_ns: int, t_ready_ns: int) -> int | None:
        """空振り（前回と同じ seq）を見たときの判断。"""
        source = self._source_period_ns
        period = self._period_ns
        if source is None or abs(source - period) > period * _RATE_TOLERANCE:
            return None
        # 次の公開までまだ間があるなら、その窓にフレームが無かっただけで
        # 位相の問題ではない。据え直しても 1 tick 落とすだけ。
        until_next = source - (now_ns - t_ready_ns)
        if until_next > period * _NEAR_BOUNDARY_RATIO:
            return None
        last = self._last_realign_ns
        if last is not None and now_ns - last < _COOLDOWN_NS:
            return None
        delay = round(t_ready_ns + source + period / 2) - now_ns
        # 公開が大きく遅れている（カメラが止まりかけている）なら、見積もり
        # 自体が当てにならないので合わせない
        if delay <= 0 or delay > period:
            return None
        self._last_realign_ns = now_ns
        return delay
