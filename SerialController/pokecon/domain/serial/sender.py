#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Sender.py - 姿勢（押下状態）を1つに持ち、Transport へ渡す層。
# コメント方針: なぜこうするかの理由を書く。外部文書への参照は付けない。
# 線の実体（serial/os/platform）は Transport が持つ。姿勢側では束ねない。
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from logging import DEBUG, NullHandler, getLogger
from typing import Any

# 送信の下回り（線を開く・閉じる・1行書き出す）は Transport が持つ。
#   Sender は「姿勢」と「入力ログ」を持ち、何で運ぶかは知らない。
#   運び方を替えるときは Transport を差し替える。
from pokecon.domain import InputLog as InputLog, transport as Transport
from pokecon.domain.procon_color import is_bcon_transport
from pokecon.domain.serial.arbitration import (
    ARBITRATION_COOLDOWN as ARBITRATION_COOLDOWN,
    ARBITRATION_MODE as ARBITRATION_MODE,
    ARBITRATION_MODES as ARBITRATION_MODES,
    HUMAN_SOURCES as HUMAN_SOURCES,
    REJECT_NOTIFY_INTERVAL as REJECT_NOTIFY_INTERVAL,
    Arbiter as Arbiter,
    list_arbitration_modes as list_arbitration_modes,
    resolve_arbitration_mode as resolve_arbitration_mode,
)
from pokecon.domain.serial.sender_live import SenderLiveMixin
from pokecon.domain.serial.sender_posture import SenderPostureMixin

# 互換のため、従来 Sender の直下にあった定数をここからも見えるようにする。
#   実体は Transport 側の1つ。外部が Sender.MIN_SEND_INTERVAL と
#     書いていても壊れない（設定・検証コードが読む可能性がある）。
from pokecon.domain.transport.text_serial import (
    MIN_SEND_INTERVAL,
)

# 入力ログの既定。ここを書き換えれば起動時の書式が変わる
INPUT_LOG_FORMAT = "simple"
INPUT_LOG_STICK_CHANGE = False

# 送信の間引き幅とタイムアウトの定義は Transport が持つ。
#   上の import で名前だけをここへ引き込んである（実体は1つ）。
#   これらは「線の性質」であって「姿勢」ではないため、
#   プロトコルを替えれば値も意味も変わるので、運び方と同じ場所に置く。
# 入力調停の定数と解決関数の実体は core/serial/arbitration.py にある。
# ここでは後方互換のため同じ名前で読めるようにしておく
# （from core.Sender import ARBITRATION_MODE と書いたまま動く）。
# 新規のコードは arbitration 側から読むこと。


class Sender(SenderLiveMixin, SenderPostureMixin):
    # 内容が変わらないので実体は1つでよい（旧コードは生成のたびに作り直していた）
    Buttons = InputLog.BUTTON_NAMES
    Hat = tuple(name.split(".")[1] for name in InputLog.HAT_NAMES)

    def __init__(
        self,
        is_show_serial: Any,
        if_print: bool = True,
        input_log_emit: Callable[[str], None] | None = None,
        transport: Transport.Transport | None = None,
    ) -> None:
        # 運び方（Transport）を1つ持つ。既定は従来と同じテキスト
        #   シリアルなので、何も指定しなければ挙動は不変。
        #   引数で差し替えられるようにしてあるのは、別のプロトコルを
        #   試すためと、検証で偽の線を差し込むため。
        self.transport = (
            transport if transport is not None else Transport.TextSerialTransport()
        )
        self.is_show_serial = is_show_serial
        # 送信スレッドが読む値の控え。Tk の変数は GUI スレッドでしか
        #   読まないため、作った場所（GUI）で一度だけ読んでおく。
        self._show_serial_last = self._initialShowSerial(is_show_serial)

        self._logger = getLogger(__name__)
        self._logger.addHandler(NullHandler())
        self._logger.setLevel(DEBUG)
        self._logger.propagate = True
        # Transport にも同じログ口を使わせる（出どころを1つに保つ）
        self.transport._logger = self._logger
        # 入力調停の判断は Arbiter が持つ。姿勢とは別の状態なので分ける。
        self._arbiter: Arbiter = Arbiter(logger=self._logger)

        self.L_holding = False
        self._L_holding = None
        self.R_holding = False
        self._R_holding = None
        self.is_print = if_print
        self.time_bef = time.perf_counter()
        self.time_aft = time.perf_counter()
        # 応答遅延の計測用。既定は切。入れたときだけ記録するので、
        #   通常利用では計測処理に触らず挙動が変わらない。
        self._perf_recording = False
        self._perf_log: deque[dict[str, Any]] = deque(maxlen=10000)
        self._perf_event_time: float | None = None
        self._perf_event_source: str | None = None
        self._perf_write_start: float | None = None
        # 入力ログ。writeRow が送った1行を InputLogger へ流し、
        # 前回との差分から press / release と押下時間を組み立てて表示する。
        self.input_logger = InputLog.InputLogger(
            emit=input_log_emit,
            template=INPUT_LOG_FORMAT,
            log_stick_change=INPUT_LOG_STICK_CHANGE,
        )
        self.input_logger.set_enabled(if_print)
        # 送信行を入力ログへ運ぶ経路は、Transport の聞き手として
        #   繋ぐ。繋がったかを必ず見る。バイナリで運ぶ実装は送信行
        #   （文字列）を作らないため繋がらず、黙っていると「1行も出ない
        #   のに動いていると思う」形で静かに壊れる。
        self._input_log_linked = bool(
            self.transport.add_listener(self.input_logger.feed)
        )
        if not self._input_log_linked:
            msg = (
                "入力ログは、この通信方式（"
                + str(getattr(self.transport, "name", "?"))
                + "）では出せません（送信行を作らないため）。"
            )
            print(msg)
            self._logger.warning(msg)
        # 送信の前後で自分の帳簿を付けるための手。計測（time_bef /
        #   time_aft）と直前の行（before）は Sender の持ち物なので、
        #   Transport には持たせずここで受け取る。
        self.transport.set_hooks(self._onWriteBegin, self._onWriteEnd)
        # 姿勢と調停を触るあいだ守る錠。送信の入口は4経路あり、うち3つは
        # 別のスレッドから来る（GUI / コマンド / キーボード）。RLock なのは
        # holdHat が applyHat を呼ぶなど、同じスレッドで錠を取り直すため。
        self._lock = threading.RLock()
        # 通信方式の切替世代。二重切替の競合で古い方が新しい worker を
        # 止めないよう、番号で見分ける（closeSerial と同じく錠の外で待つ）。
        self._transport_gen = 0
        # live送出の最低保持秒。既定16ms（8〜64msの範囲で変えられる）。
        # 8msスロット量子化で実効16ms×2状態=32msとなり、40ms間隔の
        # 連打を滞留なく通せる上限である。repeat間隔（LIVE_REPEAT_S）
        # とは独立した門である。
        self._live_min_dwell_s = 0.016
        # Pico live-state 能力がある線だけ worker を立てる。
        if self._liveCapable() and not is_bcon_transport(self.transport):
            self.startLiveWorker()

    # -- 入力ログの設定 -----------------------------------------------------

    def setInputLogFormat(self, template: str, actions: Any = None) -> None:
        """表示書式を差し替える。

        template はプリセット名（simple / detail / compact / csv / command /
        raw）か、テンプレート文字列そのもの。差し込み欄と省略ブロックの書き方は
        InputLog.py の冒頭を参照。

        actions は記録する操作の絞り込み。"PRESS,RELEASE" のような文字列か
        ("PRESS",) のような並び。None なら書式ごとの既定に従う。
        """
        self.input_logger.set_format(template, actions)

    def setInputLogEnabled(self, enabled: bool) -> None:
        """入力ログの出力を止める・再開する。"""
        self.input_logger.set_enabled(enabled)

    def setInputLogStickChange(self, enabled: bool) -> None:
        """倒したままの向き変更（CHANGE）を出すかどうか。"""
        self.input_logger.log_stick_change = bool(enabled)

    def flushInputLog(self) -> None:
        """入力ログの集約で保留になっている行を出し切る。

        連打は「A x12」の形にまとめて出すが、確定するのは次の操作が来た
        ときなので最後の1回が残る。GUI が描画のたびにここを呼ぶことで、
        操作をやめた後も取り残されずに表示される。
        """
        self.input_logger.flush()

    # =====================================================================
    # 線そのものは Transport が受け持つ
    # =====================================================================
    # ここから下の openSerial / closeSerial / isOpened / writeRow /
    #   flushPending は、名前も引数も戻り値も従来どおり。中身だけを
    #   Transport への委譲に替えた。呼び出し側は直さなくてよい。
    #
    # なぜ「名前を残して中身だけ移す」のか:
    #   これらは既存コードが直接呼んでいる公開 API で、呼び出し箇所が多い。
    #   名前を替えると移植と改名が混ざり、問題が出たときに切り分けられない。

    def _liveCapable(self, transport: Any = None) -> bool:
        """live worker が必要かを判断する唯一の場所。

        Transport.LIVE_WORKER_CAPABILITIES に名がある方式だけ True。
        新しい接続方式で worker が要る場合は、その capability 名を
        集合へ加える（未知値は legacy 等価＝同期 send_row で動く）。
        将来 end 行の要否等が増えても分岐はここへ集める。
        """
        target = self.transport if transport is None else transport
        cap = getattr(target, "capability", None)
        try:
            return bool(cap in Transport.LIVE_WORKER_CAPABILITIES)
        except TypeError:
            return False

    def isLiveCapable(self) -> bool:
        """live 経路（Pico の S 行・bcon の STATE）を使っているか。

        Keys.end の分岐用。live には 'end' という行が無く、送ると
        ERR になる。公開メソッドにしてあるのは、Keys が Transport の
        定数を知らなくて済むようにするため（循環 import 回避）。
        """
        return bool(self._liveCapable())

    def setTransport(self, transport: Transport.Transport) -> bool:
        """通信方式を切り替える。

        live worker を停止してから切り替え、新しい通信方式が live 対応で
        あれば 1 本だけ起動する。戻り値は入力ログを接続できたかどうかで
        あり、worker の停止に失敗した場合も False を返す。

        錠は参照の差し替えだけに使う。worker の join（最大 1 秒）と
        線の close を錠の中で待つと、その間すべての申告が詰まる。
        closeSerial と同じく、止める・閉じるのは外で行い、世代番号で
        二重切替の競合を見分ける。
        """
        with self._lock:
            gen = int(getattr(self, "_transport_gen", 0)) + 1
            self._transport_gen = gen
            old = self.transport
        # 止める相手はこの時点で残っている worker に限る。二重切替で勝者の
        # 世代が起こした worker へ差し替わっていたら触らない（勝者に任せる）。
        # 錠の下で同一性だけ見て、止める・閉じるのは外で行う。
        self._ensureLiveState()
        with self._live_lock:
            mine = self._live_thread
        # 錠の外で止める・閉じる（最大で 2 秒弱かかることがある）。
        if not self._stopLiveWorkerIf(mine):
            self._logger.error("PicoLiveWorker did not stop; transport unchanged")
            with self._lock:
                # 古い世代なら他の切替が進めているので黙って抜ける。
                if gen != int(getattr(self, "_transport_gen", 0)):
                    return False
            return False
        try:
            try:
                old.remove_listener(self.input_logger.feed)
            except Exception:
                self._logger.debug("旧線の listener 解除に失敗", exc_info=True)
            try:
                old.close()
            except Exception:
                self._logger.error(
                    f"Failed to close previous transport: {traceback.format_exc()}"
                )
        except Exception:
            self._logger.error(
                f"Failed to close previous transport: {traceback.format_exc()}"
            )
        with self._lock:
            # 二重切替で先を越されていたら、新しい方は捨てて抜ける。
            # 置き去りの worker を起こさない（漏れ防止）。
            if gen != int(getattr(self, "_transport_gen", 0)):
                return False
            self.transport = transport
            self.transport._logger = self._logger
            self.transport.set_hooks(self._onWriteBegin, self._onWriteEnd)
            self._input_log_linked = bool(
                self.transport.add_listener(self.input_logger.feed)
            )
            linked = bool(self._input_log_linked)
            need_live = self._liveCapable() and not is_bcon_transport(self.transport)
        if not linked:
            msg = (
                "入力ログは、この通信方式（"
                + str(getattr(transport, "name", "?"))
                + "）では出せません（送信行を作らないため）。"
            )
            print(msg)
            self._logger.warning(msg)
        if need_live:
            # 起こす前に世代を見る。既に先を越されていたら起こさない。
            # 起こしてから止め直すと、その間に勝者が起こした worker と
            # 取り違えて殺す余地が残る。
            with self._lock:
                lost_early = gen != int(getattr(self, "_transport_gen", 0))
            # 起こすのは外で。錠の中で起こすと、worker が錠を待つ間に詰む。
            started = None if lost_early else self._startLiveWorker(transport)
            with self._lock:
                # 起こしている間に先を越されていたら止め直す（漏れ防止）。
                # 止めるのは自分が起こした worker が残っているときだけ。
                # 勝者の worker へ差し替わっていたら触らない。
                if gen != int(getattr(self, "_transport_gen", 0)):
                    need_stop = True
                else:
                    need_stop = False
            if need_stop:
                if started is not None:
                    self._stopLiveWorkerIf(started)
                return False
        return linked

    def getTransportName(self) -> str:
        """いま使っている運び方の名前（設定画面・検算用）。"""
        return str(getattr(self.transport, "name", "?"))

    def isInputLogLinked(self) -> bool:
        """入力ログが送信行を受け取れているか（静かに壊れないための確認）。"""
        return bool(getattr(self, "_input_log_linked", False))

    @property
    def ser(self) -> Any:
        """生のシリアルオブジェクト。後方互換のための覗き窓。

        従来 Sender は self.ser を直に持っていた。実体は
          Transport 側へ移ったが、外から self.ser を見ているコードが
          あるため、同じ名前で覗けるようにしておく。
        """
        return getattr(self.transport, "ser", None)

    @ser.setter
    def ser(self, value: Any) -> None:
        # 差し込みたい場合（テスト・偽の線）も従来どおり書けるようにする
        setattr(self.transport, "ser", value)

    @property
    def before(self) -> Any:
        """最後に書き出した行。後方互換のための覗き窓（実体は Transport）。"""
        return getattr(self.transport, "_before", None)

    @property
    def _send_interval(self) -> float:
        """いま使っている間引き幅。後方互換のための覗き窓。"""
        return float(getattr(self.transport, "_send_interval", MIN_SEND_INTERVAL))

    def _calcSendInterval(self, baudrate: int) -> float:
        """通信速度から送信の最小間隔を決める。実体は Transport 側。

        名前を残しているのは、検証コード（verify_all.py など）が
          この名前で呼んでいる可能性があるため。
        """
        calc = getattr(self.transport, "calc_send_interval", None)
        if callable(calc):
            return calc(baudrate)
        return Transport.TextSerialTransport.calc_send_interval(baudrate)

    def _onWriteBegin(self, row: str, show: bool = True) -> None:
        """Transport が実際に書き出す直前に呼ばれる。計測の起点。

        引数 show の意味は「画面へ表示してよいか」であり、
          計測するかどうかではない。Transport は measure_perf を
          そのまま渡してくる。以前は measure_perf が偽だとフック自体が
          呼ばれず、間引きで保留された行（最も遅れる行）が計測から
          抜けていた。
        time_bef は従来どおり残す。外部が読んでいる可能性を否定
          できないため。
        帳簿は Sender の錠の中で付ける。Transport の錠の外から呼ばれる
        ため、入れ子で詰まることはない。
        """
        _ = (row, show)
        with self._lock:
            self.time_bef = time.perf_counter()
            if self._perf_recording:
                self._perf_write_start = self.time_bef

    def _onWriteEnd(self, row: str, show: bool = True) -> None:
        """Transport が書き出した直後に呼ばれる。計測の終点。

        表示（show）と計測を分けている。計測は常に行い、
          print だけを show で制御する。従来 measure_perf=False の
          行は print もされなかったので、表示の挙動は変わらない。
        帳簿は Sender の錠の中で付ける（_onWriteBegin と同じ理由）。
        """
        with self._lock:
            self.time_aft = time.perf_counter()
            if self._perf_recording:
                self._recordPerf(row)
        # Show sending serial datas
        if show and self._should_show_serial():
            print(row)

    # ------------------------------------------------------------------
    # 応答遅延の計測
    # ------------------------------------------------------------------
    # 何のためにあるか:
    #   送信の仕組みを変える前後で、応答の速さを比べるための物差し。
    #   測る対象は event_time（入口で申告を受けた時刻）から
    #     write_start_time（実際に線へ書き始めた時刻）までの間隔。
    #
    # なぜ既定で切ってあるか:
    #   測る行為そのものが遅延を生む。常時記録すると、測った値が
    #     「測っていないときの速さ」からずれる。必要なときだけ入れる。
    #   切ってあるあいだは計測処理を行わないので、
    #     通常利用の挙動は変わらない。
    #
    # event_time をどこで採るか:
    #   申告の入口（pressButtons / releaseButtons / setHat / setStick /
    #     holdHat / releaseHat）で採る。『いつ押されたか』であって
    #     『いつ送ったか』ではない。
    #   送信後に採った時刻は「送信後」の時刻なので、起点には使えない。

    def setPerfRecording(self, enabled: bool, capacity: int = 10000) -> None:
        """応答遅延の記録を入／切する。既定は切。"""
        with self._lock:
            if enabled and not self._perf_recording:
                self._perf_log = deque(maxlen=int(capacity))
            self._perf_recording = bool(enabled)

    def isPerfRecording(self) -> bool:
        """いま記録しているか。"""
        return bool(self._perf_recording)

    def markEvent(self, source: str | None = None) -> None:
        """入口で申告を受けた時刻を記録する。

        申告系の入口から必ず呼ぶ。ここを呼び忘れた経路は、
          その行の遅延が測れない（＝統計から抜ける）。
        記録が切のときは何もしない。
        """
        if not self._perf_recording:
            return
        with self._lock:
            # まだ線へ出ていない申告が続いたときは、最初の時刻を残す。
            #   上書きすると『待たされた時間』が消え、短く見える。
            if self._perf_event_time is None:
                self._perf_event_time = time.perf_counter()
                self._perf_event_source = source

    def _recordPerf(self, row: str) -> None:
        """1行ぶんの計測を記録へ積む。_onWriteEnd から呼ばれる。

        1件だけ持つ形（time_bef / time_aft）では中央値も95パーセン
          タイルも出せない。上書きされて消えるため、記録は溜める。
        event_time が無い行（内部由来の送信など）は None のまま積む。
          捨てないのは、送信本数を数えるときに欠けると合わなくなるため。
        """
        ev = self._perf_event_time
        start = self._perf_write_start
        end = self.time_aft
        self._perf_log.append(
            {
                "event": ev,
                "write_start": start,
                "write_end": end,
                "source": self._perf_event_source,
                "row": row,
                # 遅延そのものはここで引いておく。後から引くと、
                #   event が None の行を数え間違える。
                "latency": (start - ev)
                if (ev is not None and start is not None)
                else None,
            }
        )
        # 1行出したら次の申告のために空ける（次の event_time を採れる）
        self._perf_event_time = None
        self._perf_event_source = None

    def getPerfLog(self) -> list[dict[str, Any]]:
        """記録の写しを返す。読み取り専用。

        ここでは統計を計算しない。中央値の求め方や外れ値の扱いは
          測る側の判断なので、Sender は「起きたこと」だけを渡す。
        """
        with self._lock:
            return list(self._perf_log)

    def clearPerfLog(self) -> None:
        """記録を捨てる（測り直すとき用）。"""
        with self._lock:
            self._perf_log.clear()
            self._perf_event_time = None
            self._perf_event_source = None

    def openSerial(
        self,
        portNum: int,
        portName: str = "",
        baudrate: int = 9600,
        **extra: Any,
    ) -> bool:
        """線を開く。中身は Transport が行う。

        extra は将来の接続方式のための余白（VID/PID・IP 等）。
        そのまま Transport.open へ渡す。既存の3引数呼び出しは壊れない。

        開けた場合は live 経路に限り worker を起動する。切断で worker を
        止めているため、ここで起こさないと開き直しても状態が送出されない。
        判定は他の呼び出し元と同じく _liveCapable に揃える。既に動いて
        いれば startLiveWorker が二重には起こさない。
        """
        opened = self.transport.open(portNum, portName, baudrate, **extra)
        if opened and self._liveCapable() and not is_bcon_transport(self.transport):
            if not self.startLiveWorker():
                # 直前の close が join を諦めた場合、停止指示を受けた古い
                # worker がまだ send_row で詰まっている。そのままでは
                # startLiveWorker が「生きている」と見て何も起こさず、
                # 古い worker が抜けた後に無音になる（接続表示は繋がったまま）。
                # 1度だけ止め直してから起こし直す。二重起動はしない。
                self.stopLiveWorker(1.0)
                if not self.startLiveWorker():
                    self._logger.error(
                        "PicoLiveWorker did not restart; "
                        "the old worker is still stopping"
                    )
        return opened

    def closeSerial(self) -> None:
        """回線を安全に閉じる。

        停止は順序が全てである。

            1  新規の live 入力の受付を停止する（live のみ）
            2  未送信の通常状態を破棄する（live のみ）
            3  中立を送る（live は _closing 指定。legacy は閉じる直前に
               中立の1行を書く）
            4  送出の完了を期限つきで待つ（live のみ）
            5  worker へ停止を要求する（live のみ）
            6  期限つきで停止を待つ（live のみ）
            7  入力ログを初期化する（live / legacy の両方）
            8  Transport を閉じる

        中立の送出は worker が生きているうちに行う。worker を止めてから
        列へ入れても誰も送出しないため、押下が残る。legacy は状態を送る
        相手が消えるため、閉じる直前に neutral の1行を書いておかないと、
        最後の押下行が線に残って Switch 側が押しっぱなしになる。

        ステップ 1 から 7 は try の内側に置き、ステップ 8 は finally で必ず実行
        する。途中で失敗しても回線を開いたままにしない。

        錠（_lock）は状態の変更だけに使う。送出の完了待ち・worker の
        join・入力ログの初期化まで錠の中で行うと、その間すべての申告と
        読み取りが止まり、GUI が固まったように見える。順序は
        変えず、待つ部分だけ外へ出す。
        """
        self._logger.debug("Closing the serial communication")
        live = self._liveCapable()
        try:
            with self._lock:
                if live:
                    # 1: これ以降の状態変化を mailbox へ入れない。
                    self._live_closing = True
                    # 2: 中立より後に古い状態が出ないようにする。
                    self.discardLive()
                    # 3: worker が生きているうちに中立を送る。
                    self.releaseAll()
                    self.putLive(self.snapshot(), _closing=True)
            if live:
                # 4: 送出の完了を待つ。期限を過ぎたら次へ進む。
                if not self.waitLiveDrained(self.CLOSE_DRAIN_S):
                    self._logger.error("Neutral state was not sent before closing")
                # 5 から 7: 停止を要求し、起床させ、期限つきで待つ。
                if not self.stopLiveWorker(self.CLOSE_JOIN_S):
                    self._logger.error("PicoLiveWorker did not stop; closing anyway")
            else:
                # 7: legacy は閉じたあとに送る相手が居ない。押下を落として
                #    中立の1行を書いてから閉じる（状態だけ戻しても、線に
                #    残っている最後の行は押下のままになる）。
                try:
                    self.sendNeutralAll()
                except Exception:
                    self._logger.error(
                        "Neutral state could not be sent before closing",
                        exc_info=True,
                    )
            # 8: 入力ログを初期化する。live / legacy のどちらも、閉じた
            #    時点で押下の差分を捨てる（次に開いたときの差分を狂わせない）。
            self.input_logger.reset()
        finally:
            # 9: どの手順が失敗しても回線は必ず閉じる。手順 1〜3 を
            # try の外に置くと、そこで例外が出たときに閉じず、かつ
            # 閉じ中の印が残って以後すべての live 入力を断る。
            with self._lock:
                self._live_closing = False
            self.transport.close()

    def isOpened(self) -> bool:
        self._logger.debug("Checking if serial communication is open")
        return self.transport.is_open()

    def _should_show_serial(self) -> bool:
        """いま送信行を表示するか。送信スレッドからも呼ばれる。

        素の bool はそのまま読む。Tk の変数（get を持つもの）は
        GUI スレッドからだけ読み、送信スレッドからは押し込まれた値を
        使う。Tk の変数は GUI スレッドのもので、worker から読むと
        GUI が忙しい間（Tcl が回っていない間）そこで待たされる。
        実機で測ると 1.5 秒待たされ、その間は姿勢が1本も送られなかった。

        読めないときは直前の値を保つ。表示の有無で送信は変えない。
        """
        target = self.is_show_serial
        getter = getattr(target, "get", None)
        if not callable(getter):
            self._show_serial_last = bool(target)
            return self._show_serial_last
        if threading.current_thread() is not threading.main_thread():
            # ここで Tk の変数は読まない（GUI の都合で待たされるため）。
            return bool(getattr(self, "_show_serial_last", True))
        try:
            value = bool(getter())
        except Exception:
            # 読めないときは直前の値を保つ。表示の有無で送信は変えない。
            return bool(getattr(self, "_show_serial_last", True))
        self._show_serial_last = value
        return value

    def setShowSerial(self, value: Any) -> None:
        """送信行を表示するかを押し込む（GUI スレッドから呼ぶ）。

        画面が値の変化を知らせるための入口である。Tk の変数を
        送信スレッドに読ませないために要る。画面側の入口は
        SerialService.set_show_serial。

        素の bool を渡されていた場合は属性そのものが正なので、
        そちらも揃える（外から見た値と送信側の値が食い違わない）。
        Tk の変数は書き換えない（画面の表示と取り合いになるため）。
        """
        self._show_serial_last = bool(value)
        if not callable(getattr(self.is_show_serial, "get", None)):
            self.is_show_serial = self._show_serial_last

    @staticmethod
    def _initialShowSerial(value: Any) -> bool:
        """表示可否の初期値を読む。

        Tk の変数は GUI スレッドのときだけ読む。読めない場所で
        作られた場合の既定は「表示する」（送信そのものは変えない）。
        """
        getter = getattr(value, "get", None)
        if not callable(getter):
            return bool(value)
        if threading.current_thread() is not threading.main_thread():
            return True
        try:
            return bool(getter())
        except Exception:
            return True

    def writeRow(
        self, row: str, is_show: bool = False, measure_perf: bool = True
    ) -> None:
        """1行送信する。中身は Transport が行う。

        間引き（coalescing）の条件も、送れなかったときの扱いも従来と
          同じ。移しただけで判断は変えていない。
        is_show は後方互換のため残置。入力ログは常に input_logger が行う。
        """
        self.transport.send_row(row, measure_perf=measure_perf)

    def _is_coalescable(self, row: str) -> bool:
        """間引いてよい行か。実体は Transport 側（名前だけ残す）。"""
        judge = getattr(self.transport, "_coalescable", None)
        return bool(judge(row)) if callable(judge) else False

    def flushPending(self) -> None:
        """間引きで保留した行があれば送る。

        「最後に少しだけ倒した」状態が送られずに残ると、操作が中途半端
        なまま止まる。GUI が離した時や切断時など、区切りで呼ぶ。
        """
        self.transport.flush_pending()

    def _write(self, row: str, measure_perf: bool = True) -> None:
        """実際に書き出す。実体は Transport 側（名前だけ残す）。

        互換のために戻した名前であり、リポジトリ外の利用者コードが
          触っている可能性は否定できない。
          消す判断は、消してよい根拠が取れてからにする。
        錠を通る send_row 経由で送る。_write 直呼びでは Transport の
        錠を迂回し、聞き手の付け外しと競合するため。
        """
        self.transport.send_row(row, measure_perf=measure_perf)

    def writeRow_wo_perf_counter(self, row: str, is_show: bool = False) -> None:
        """後方互換のための薄いラッパ。writeRow(measure_perf=False) と等価。"""
        self.writeRow(row, is_show=is_show, measure_perf=False)

    def show_input(self, output: Any) -> None:
        """後方互換の入口。旧APIはトークン列を受け取っていた。

        中身は input_logger へ委譲する。press / release / 押下時間の判定は
        すべて InputLog 側が持つので、ここでは行へ戻して渡すだけでよい。
        """
        try:
            if isinstance(output, (list, tuple)):
                row = " ".join(str(x) for x in output)
            else:
                row = str(output)
            self.input_logger.feed(row)
        except Exception:
            self._logger.error(f"show_input failed: {traceback.format_exc()}")

    # =====================================================================
    # 姿勢（Posture）を Sender が1つ持つ
    # =====================================================================
    # なぜここへ置くか:
    #   押下状態が系統ごとに分かれていると、どの系統も「自分の状態」しか
    #   知らないため、送る行が他系統の押下を消す。押しながら別系統で触ると
    #   ボタンが落ちたり、左右スティックを同時に倒せなかったりする。
    #
    #   Sender は最初から一意で、全経路が既にここへ集まっている。送信状態も
    #   既に持っているので、姿勢もここへ置けば「送信に関することは Sender」
    #   で一貫する。同じ錠で守れるため、錠も増えない。
    #
    # _buildRow について:
    #   Keys.SendFormat.convert2str と同じ結果を返す実装をここへ「並べて」
    #   置く。既存の動作を変えないため、一時的に同じ処理が2箇所に並ぶ。

    # =====================================================================
    # 状態の統合と、調停の口
    # =====================================================================
    # 配線は全て Sender に集まったが、KeyPress が自分専用の姿勢を
    #   持ち続けていると、書き出すたびに「自分の内容で全体を上書き」する。
    #   ここでは KeyPress から専用状態を外し、差分だけを申告させる。
    #
    # 調停（arbitration）について:
    #   姿勢が本当に1つになると、スクリプトと人の操作が同じ姿勢を
    #   書き換える。遠隔操作の実装では、人の手入力を
    #   最優先し、自動側を横取りして一定時間拒否する「入力調停」を
    #   置くのが一般的。
    #   ただしここでは調停を入れない。理由は2つ。
    #     1. 現状も混ざっている（混ざり方が壊れていただけ）
    #     2. 調停は「所有者」という新しい状態を持ち込む。統合とは別の問題で、
    #       混ぜると成否が分からなくなる。
    #   代わりに『口』だけ用意する: source で誰の申告かを渡せるようにし、
    #     受理したかを bool で返す。ここでは常に True（全部受理）。

    # =====================================================================
    # 入力調停（Input Control Arbitration）
    # =====================================================================
    # 姿勢が本当に1つになった結果、スクリプト・キーボード・マウスが
    #   同じ姿勢を書き換えるようになった。
    #   だが「常に混ざってよいか」は別問題である。
    #
    # 方針:
    #   ・誰の申告かは source で分かる
    #   ・受理したかは bool で返す
    #   ・ここでは「受理するかどうかの判断」だけを _accept へ入れる。
    #     _accept の1箇所を書き換えれば全経路に効く設計なので、
    #     足すのはこの1箇所で済む。
    #
    # 既定は "off"（全部受理）にしてある。理由:
    #   ・それまでの挙動を1文字も変えないため（既存コマンドへの影響なし）
    #   ・調停は「うっかり触って邪魔する」を防ぐ仕組みだが、裏返すと
    #     「触ったのに効かない時間」を作る。どちらが良いかは使い方次第で、
    #     利用者が選べるべきもの。
    #
    # 拒否は黙って捨てない。_notifyReject で必ず知らせる。
    #   黙って無視すると「スクリプトが動かない」と見え、原因が分からなく
    #   なる。

    def _ensureArbitration(self) -> None:
        """調停の持ち主がまだ無ければ作る（遅延初期化）。

        _ensurePosture と同じ作法。__init__ を書き換えないのは、
        古い版から作られた Sender でも動くようにするため。
        通常は __init__ で作ってあるので何もしない。
        """
        if not hasattr(self, "_arbiter"):
            self._arbiter = Arbiter(logger=getattr(self, "_logger", None))

    def setArbitration(
        self, mode: str | None = None, cooldown: float | None = None
    ) -> None:
        """調停の設定を変える。設定画面や起動引数から呼ぶ。

        実体は Arbiter が持つ。ここでは口だけ残す。
        """
        self._ensureArbitration()
        self._arbiter.set(mode=mode, cooldown=cooldown)

    def setLiveMinDwell(self, ms: int) -> None:
        """live送出の最低保持を変える。8〜64ms以外は無視する。"""
        try:
            value = int(ms)
        except (TypeError, ValueError):
            return
        if not 8 <= value <= 64:
            return
        self._live_min_dwell_s = value / 1000.0
        sched = getattr(self, "_live_sched", None)
        if sched is not None:
            sched.set_min_dwell(value / 1000.0)

    def getArbitration(self) -> dict[str, Any]:
        """現在の調停の設定と実績を返す（設定画面・確認用）。"""
        self._ensureArbitration()
        return self._arbiter.get()

    def setArbitrationNotifier(self, fn: Callable[[str], None] | None) -> None:
        """拒否を知らせる先を差し替える（既定は print とログ）。

        GUI ならログ欄へ、CUI なら標準出力へ、といった具合に
        呼び出し側が決められるようにする。
        """
        self._ensureArbitration()
        self._arbiter.set_notifier(fn)

    @staticmethod
    def _isHumanSource(source: str | None) -> bool:
        """人の手による操作か。source が無いもの(None)は自動側とみなす。"""
        return Arbiter.is_human_source(source)

    def _notifyReject(self, source: str | None, winner: str) -> None:
        """拒否したことを知らせる。実体は Arbiter が持つ。"""
        self._ensureArbitration()
        self._arbiter._notify_reject(source, winner)

    def setHandedOver(self, handed: bool) -> None:
        """自動側が操作を人へ渡している最中かを設定する。実体は Arbiter。

        時間ではなく、渡したという事実で判断する。詳しくは
        Arbiter.set_handed_over の説明を参照。
        """
        self._ensureArbitration()
        self._arbiter.set_handed_over(handed)

    def isHandedOver(self) -> bool:
        """いま人へ渡している最中かを返す（読むだけ）。"""
        self._ensureArbitration()
        return self._arbiter.is_handed_over()

    def _accept(self, source: str | None, releasing: bool = False) -> bool:
        """その申告を受理してよいか。判断の実体は Arbiter が持つ。

        ここを経由する形は残す。全経路（pressButtons / releaseButtons /
        setHat / sendPosture / sendNeutralAll）がここを通るため、
        受け口を変えずに判断だけを移せる。
        """
        self._ensureArbitration()
        return self._arbiter.accept(source, releasing=releasing)

    def pressButtons(self, btns: Any, source: str | None = None) -> bool:
        """ボタンを押す（差分の申告）。受理したら True。

        呼び出し側は「押したいボタン」だけを渡す。
          押していない他のボタンには一切触れないので、別系統が
          押しているものを消さない。
        """
        if not self._accept(source):
            return False
        self.markEvent(source)  # 入口の時刻（受理した申告だけ測る）
        self.applyButtons(press=btns, source=source)
        return True

    def releaseButtons(self, btns: Any, source: str | None = None) -> bool:
        """ボタンを離す（差分の申告）。受理したら True。"""
        if not self._accept(source, releasing=True):
            return False
        self.markEvent(source)  # 入口の時刻（受理した申告だけ測る）
        self.applyButtons(release=btns, source=source)
        return True

    def setHat(self, hat: Any = None, source: str | None = None) -> bool:
        """十字キーの向きを申告する。None で中立。受理したら True。

        向きの申告も所有者ごとに記録する。holdHat だけが記録していた
        頃は、通常の押下が suspend / drop の対象外になり、Pause で
        十字キーが漏れていた。None（中立）は解放と同じ扱いにし、自分の
        申告を取り下げる。
        """
        if not self._accept(source, releasing=hat is None):
            return False
        self.markEvent(source)  # 入口の時刻（受理した申告だけ測る）
        with self._lock:
            self._ensureHatHold()
            self._ensureOwners()
            owner = self._ownerKey(source)
            if hat is None:
                self._hat_held.pop(str(source), None)
                self._owner_hat.pop(owner, None)
            else:
                self._owner_hat[owner] = (int(hat), self.getRevision() + 1)
        self.applyHat(hat)
        return True

    def setStick(
        self,
        stick: str,
        x: int | None = None,
        y: int | None = None,
        source: str | None = None,
    ) -> bool:
        """スティックの座標を申告する。常に受理して True を返す。

        スティックは連続値であり、ボタンのような所有権になじまない。
        中立へ戻す途中も含めて拒否すると半倒しが残るため、調停の対象外
        とする。人の手で倒せる操作は、調停の mode にかかわらず出せる。
        複数系統が同時に触った場合は revision が新しい申告が採られる。
        """
        self.markEvent(source)  # 入口の時刻
        self.applyStick(stick, x, y, source=source)
        return True

    def sendPosture(self, source: str | None = None) -> bool:
        """現在姿勢を送る。Pico live経路では列へ積むだけにする。

        送るのは「合成後の全体」である。棄却された系統の値は入って
        いないが、通った別系統の値（常時受理のスティックなど）は入る。
        調停で送出そのものを断られた行は、次の受理で送り直される。
        中立への復帰は sendNeutralAll（常時受理）で行うため、解放が
        届かないことはない。
        """
        if not self._accept(source):
            return False
        # apply* が既に列へ入れている。ここで同期送信
        #   するとGUIスレッドが Transport の錠と UART 書き込みを待ち、
        #   worker と二重送信になる。legacy だけ従来どおり同期送信する。
        if self._liveCapable():
            return True
        self.writeRow(self._buildRow())
        return True

    def sendNeutralAll(self, source: str | None = None) -> bool:
        """すべて中立へ戻す。live 経路では列へ渡す。

        既に中立であっても再送する。releaseAll は状態が変わらな
        ければ列へ渡さないため、ここで現在の snapshot を渡す。
        revision は増やさない。
        """
        if not self._accept(source, releasing=True):
            return False
        self.releaseAll()
        if self._liveCapable():
            # 錠の外で採った snapshot を渡すと順序が崩れ得るので、採取ごと任せる。
            self.putLive()
            return True
        self.writeRow(self._buildRow())
        return True

    def _bumpRevision(self) -> int:
        """姿勢が変わったことを記録する。錠の中から呼ぶこと。

        呼び忘れると snapshot の revision が据え置きになり、
          「変わっていない」と誤判定され、worker が送信を省く。
        """
        rev = getattr(self, "_posture_revision", 0) + 1
        self._posture_revision = rev
        return rev

    def getRevision(self) -> int:
        """いまの revision。読むだけで何も変えない。"""
        return int(getattr(self, "_posture_revision", 0))

    def snapshot(self) -> dict[str, Any]:
        """現在の姿勢の写しを1つ返す（読み取り専用・副作用なし）。

        返すのは新しい辞書なので、呼び出し側が書き換えても
          Sender の状態には影響しない。逆に、返したあとで Sender 側が
          変わっても、返した写しは変わらない（immutable として扱える）。

        含めるもの: btn / hat / lx / ly / rx / ry / revision。
          変化印（_L/_R_stick_changed）は含めない。あれは legacy の
            可変長書式のための状態で、状態そのものではないため。
        """
        with self._lock:
            self._ensurePosture()
            p = self._posture
            return {
                "btn": int(p["btn"]),
                "hat": int(p["hat"]),
                "lx": int(p["lx"]),
                "ly": int(p["ly"]),
                "rx": int(p["rx"]),
                "ry": int(p["ry"]),
                "revision": self.getRevision(),
            }

    def verifySnapshotPurity(self, times: int = 3) -> bool:
        """snapshot を何度読んでも同じ値かを確かめる。

        _buildRow は2度目に違う行を返す（変化印が下りるため）。
          snapshot がその轍を踏んでいないことを機械で確かめる。
        目視で「副作用は書いていない」と言うのではなく、実際に
          複数回読んで一致を見る。
        """
        if times < 2:
            times = 2
        first = self.snapshot()
        for _ in range(times - 1):
            if self.snapshot() != first:
                return False
        return True

    # ------------------------------------------------------------------
    # Pico 専用 full-state encoder
    #
    # 何のためか:
    #   Pico ファームは "S <btn> <hat> <lx> <ly> <rx> <ry>" を受け取る。
    #   従来書式（_buildRow）とは別書式なので、変換する口が要る。
    #
    # 従来書式と決定的に違う2点（ここを間違えると別のボタンが押される）:
    #   1. 従来書式は btn を2ビット左シフトし、下位2bit へスティックの
    #     変化印を入れる。Pico はシフトしない。同じ姿勢でも値が4倍違う。
    #   2. 従来書式は変化した側のスティックだけを付ける可変長。
    #     Pico は6項目すべて必須。足りないと ERR を返される。
    #
    # だから Pico 側には「変化印」という概念が要らない。
    #   毎回すべての軸を送るので、1行落ちても次の行で復旧する
    #   （フルステート方式）。
    #
    # 書式の細かい決まり（すべて Pico ファームの受信仕様による）:
    #   6項目とも16進。hat も16進。
    #   "0x" を付けてはいけない（x は不正文字として ERR になる）。
    #   区切りは空白1つ以上。
    #
    # 純関数にしてある理由:
    #   snapshot だけを引数に取り、self を読まない（@staticmethod）。
    #   同じ snapshot からは常に同じ行が出る。何度呼んでも変わらない。
    #   _buildRow のような読み取り副作用を持ち込まない。
    # ------------------------------------------------------------------
