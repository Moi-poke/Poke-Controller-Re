#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recovery.py - 回復可能な実行時エラーの分類と、操作単位の再試行。

なぜ分けるか:
  利用者の自動化スクリプトは任意の Python なので、例外が出た位置から
  フレームを再開することはできない。復帰できるのは「同じ操作をもう一度
  呼べる」場所だけである。そこで、画像読み込み・カメラ読み取りのような
  冪等な操作をこの層で包み、失敗したら同じ操作をやり直す。

  何を回復可能とみなすかは、その場の例外の型で決める。構文エラー・
  NameError・TypeError のようなプログラマ誤りは回復不能として素通しする。
  一過性の失敗（カメラの一時切断、シリアル断）だけを回復可能として扱う。

根拠（一般のベストプラクティス）:
  ・一時的な障害だけ再試行し、非一時的な障害は再試行しない
    （Microsoft "Best practices for transient fault handling"）
  ・再試行は冪等な操作に限る。無制限に繰り返さない
    （Microsoft "Retry pattern" / AWS "Backoff pattern"）
  ・対話的な操作は短い間隔で数回、自動で待ってから人へ渡す
    （同記事。バックグラウンド処理向けの指数バックオフをそのまま
      使うと、利用者の目の前で待ち時間だけが伸びる）
  ・画像が見つからないときは、人が直してから同じ探索をやり直せる形にする
    （SikuliX の FindFailed、WinActor の画像マッチング再試行）

このモジュールは tkinter を触らない。ダイアログを出すかどうかは
呼び出し側（CommandVision / PythonCommandBase）が ask として渡す。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from loguru import logger
from serial import SerialException

T = TypeVar("T")


@dataclass(frozen=True)
class RecoveryAdvice:
    """利用者に見せる回復の案内と、自動再試行の回し方。

    auto_attempts は「初回の失敗のあと、人が触らなくても追加で試す回数」。
    0 なら自動では試さず、すぐ人へ尋ねる。テンプレート画像の欠如は
    人がファイルを置くまで直らないため 0、カメラの一時切断は数回
    試す価値があるため 2 にしている。

    ジッターは持たない。クラウドのクライアントと違い、このアプリは
    1プロセスが1本の線を使うだけなので、同時再試行の集中は起きない。
    """

    title: str
    detail: str = ""
    hint: str = ""
    retry_label: str = "再試行"
    abort_label: str = "中止"
    auto_attempts: int = 0
    initial_delay: float = 0.25
    multiplier: float = 2.0
    max_delay: float = 2.0

    def delay(self, retry_index: int) -> float:
        """retry_index 回目（0 始まり）の前に待つ秒数。上限で頭打ちにする。"""
        if self.initial_delay <= 0:
            return 0.0
        delay = self.initial_delay * (self.multiplier ** max(0, retry_index))
        return min(float(self.max_delay), float(delay))

    def body(self) -> str:
        """見出しを除いた本文（詳細と案内）。空の節は詰める。"""
        parts = [self.detail] if self.detail else []
        if self.hint:
            parts.append(self.hint)
        return "\n".join(parts)

    def message(self) -> str:
        """見出しを含む全文。ログやテストで使う。"""
        return "\n".join(part for part in (self.title, self.body()) if part)


class RecoverableError(Exception):
    """人が直してから同じ操作をやり直せる可能性がある実行時エラー。

    利用者スクリプトからも投げられる。`hint` に「何を直せばよいか」を
    書くと、GUI の回復ダイアログにそのまま出る。
    """

    #: ダイアログの見出し。派生クラスで具体化する。
    title = "回復可能なエラー"

    def __init__(self, message: str = "", *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint

    def advice(self) -> RecoveryAdvice:
        return RecoveryAdvice(title=self.title, detail=str(self), hint=self.hint)


class TemplateImageError(RecoverableError, FileNotFoundError):
    """テンプレート画像を読めない。

    FileNotFoundError を継承しているのは互換のため。既存のスクリプトが
    `except FileNotFoundError` で受けている場合もそのまま捕まる。
    ファイルが無いのか、壊れているのかはメッセージで区別する。
    """

    title = "テンプレート画像を読み込めません"
    auto_attempts = 0

    def advice(self) -> RecoveryAdvice:
        return RecoveryAdvice(
            title=self.title,
            detail=str(self),
            hint=self.hint,
            auto_attempts=self.auto_attempts,
        )


class CameraUnavailableError(RecoverableError, RuntimeError):
    """カメラからフレームを取得できない（一時的な切断・Disable 等）。

    「カメラが割り当てられていない」は設定・コードの誤りなので、
    この型にはせず RuntimeError のままにする。
    """

    title = "カメラから画像を取得できません"
    auto_attempts = 2

    def advice(self) -> RecoveryAdvice:
        return RecoveryAdvice(
            title=self.title,
            detail=str(self),
            hint=self.hint
            or "カメラを再接続するか、カメラ欄で有効にしてから再試行してください。",
            auto_attempts=self.auto_attempts,
            initial_delay=0.25,
            multiplier=2.0,
            max_delay=1.0,
        )


class RecoveryAborted(Exception):
    """利用者が「再試行」ではなく「中止」を選んだ。"""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"再試行を中止しました: {cause}")
        self.cause = cause


# 一時的な可能性があるが、操作単位ではやり直せないもの。
# コマンド全体を最初からやり直すかどうかを人に決めてもらう。
_TRANSIENT_ADVICE = RecoveryAdvice(
    title="一時的な通信エラーが発生しました",
    hint=(
        "接続先（USB・COM ポート・ネットワーク）を確認してから再試行してください。"
        "再試行するとコマンドは最初からやり直しになります。"
    ),
    auto_attempts=0,
)

_SERIAL_ADVICE = RecoveryAdvice(
    title="シリアル通信に失敗しました",
    hint=(
        "USB ケーブルと COM ポートを確認し、必要なら接続し直してから"
        "再試行してください。再試行するとコマンドは最初からやり直しになります。"
    ),
    auto_attempts=0,
)


def classify(exc: BaseException) -> RecoveryAdvice | None:
    """例外が回復可能かを判定する。回復不能なら None。

    型で決める。メッセージの文字列では判定しない（文言の変更で
    挙動が変わり、利用者スクリプトの独自例外まで拾ってしまうため）。
    """
    if isinstance(exc, RecoverableError):
        return exc.advice()
    if isinstance(exc, SerialException):
        return _SERIAL_ADVICE
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return _TRANSIENT_ADVICE
    return None


def retry_with_recovery(
    operation: Callable[[], T],
    *,
    ask: Callable[[BaseException, RecoveryAdvice], bool | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    classifier: Callable[[BaseException], RecoveryAdvice | None] = classify,
) -> T:
    """operation を実行し、回復可能な例外なら同じ操作をやり直す。

    ask(exc, advice) の戻り値:
      True  … 利用者が直したので、もう一度 operation を呼ぶ
      False … 利用者が中止した。RecoveryAborted を送出する
      None  … 人に尋ねられない（GUI が無い・ダイアログを開けない）。
              元の例外をそのまま送出する

    ask が None のときは自動再試行だけを行い、終わったら元の例外を
    送出する。停止要求は sleep 側（CommandOperate.wait）が例外にして
    抜けるため、ここでは握らない。
    """
    auto_done = 0
    manual_for: RecoveryAdvice | None = None
    while True:
        try:
            return operation()
        except Exception as exc:
            advice = classifier(exc)
            if advice is None:
                raise
            if advice != manual_for and auto_done < int(advice.auto_attempts):
                delay = advice.delay(auto_done)
                auto_done += 1
                logger.info(
                    f"{advice.title}: 自動で再試行します"
                    f" ({auto_done}/{advice.auto_attempts}, {delay:.2f}s 後)"
                )
                if delay > 0:
                    sleep(delay)
                continue
            if ask is None:
                raise
            logger.warning(f"{advice.title}: 利用者に回復を促します")
            decision = ask(exc, advice)
            if decision is None:
                raise
            if not decision:
                raise RecoveryAborted(exc) from exc
            # 人が直したあとは、同じ種類の失敗を自動で繰り返さない
            # （押した本人が待っているのに、黙って待ち時間だけ伸ばさない）。
            # 別の種類の失敗に変わったら、その種類の自動再試行は使える。
            manual_for = advice
            auto_done = 0
            logger.info(f"{advice.title}: 利用者の操作後に再試行します")
