"""IMU の 3D ジャイロプレビュー（pygame 子プロセス）。

Tk 側の ``MotionPreview`` が毎サンプル ``Orientation`` を積分し、共有メモリへ
クォータニオンと monotonic 時刻を非ブロッキングで公開する。pygame の窓は
別プロセス（spawn）で動かし、Tk/SDL のグローバル状態を共有しない。

子プロセスは Tk もハードウェアも触らない。受け取った最新姿勢を 60fps で
描くだけ。滑らかなモデルと質感の描画は ``imu_preview_gpu`` が担う。
"""

from __future__ import annotations

import ctypes
import importlib.util
import multiprocessing
import os
import sys
import threading
import time
import traceback
from typing import Any, Final

from pokecon.domain.controller_preview import PreviewControls
from pokecon.domain.imu_orientation import Orientation, relative_orientation

#: 子プロセスの窓サイズ（要件の目安 720x540）。
WINDOW_W: Final = 720
WINDOW_H: Final = 540
FPS: Final = 60

#: これを超えて新しいサンプルが来なければ「入力停止」と表示する。
STALE_SECONDS: Final = 0.5
#: Orientation が積分に使う 1 ステップの上限。これより大きい dt は
#: ギャップとして 0.0 を渡し、回転を飛ばす（ドメイン側も >0.1s は捨てる）。
MAX_STEP_SECONDS: Final = 0.1

#: start() が子の初期化を待つ上限。待ち切れなくてもプロセスが生きていれば
#: 起動成功として返し、窓は後から出る。
START_READY_TIMEOUT_S: Final = 1.5
#: close() の終了待ち。child は 60fps で stop_event を見るので通常は一瞬。
CLOSE_JOIN_S: Final = 1.0
TERMINATE_JOIN_S: Final = 0.5

#: 姿勢と操作を同じ世代で渡す。時刻は別々に持ち、入力途絶を混同しない。
#: 0..3=姿勢、4=IMU時刻、5=世代、6=ボタン、7..10=左右スティック、
#: 11=パッド接続状態、12=パッド取得時刻。
_SNAPSHOT_LEN: Final = 13
_STAMP_INDEX: Final = 4
_SEQ_INDEX: Final = 5
_CONTROLS_STAMP_INDEX: Final = 12
_ERROR_BYTES: Final = 4096

_TAB_RECTS: Final = (
    (20, 58, 160, 30),
    (188, 58, 150, 30),
    (346, 58, 150, 30),
)
_RESET_RECT: Final = (560, 58, 140, 30)

# ---- 色 ----
_PANEL = (32, 39, 52)
_PANEL_HOVER = (44, 53, 69)
_TEXT = (230, 236, 245)
_MUTED = (140, 152, 170)
_OK = (110, 220, 160)
_WARN = (255, 190, 80)
_DANGER = (255, 105, 105)

_JOY_L = (0, 195, 227)
_JOY_R = (255, 60, 40)

#: 日本語が出るフォントを優先順に。Windows 標準を想定。
_FONT_CANDIDATES: Final = (
    "Meiryo UI",
    "Meiryo",
    "Yu Gothic UI",
    "Yu Gothic",
    "MS UI Gothic",
    "MS Gothic",
    "Noto Sans JP",
)

#: モデル選択キー（1=Pro, 2=Joy-Con L, 3=Joy-Con R）。
_MODEL_NAMES: Final = (
    "Proコントローラー",
    "Joy-Con (L)",
    "Joy-Con (R)",
)
_MODEL_ACCENTS: Final = ((96, 102, 116), _JOY_L, _JOY_R)


class MotionPreview:
    """IMU の 3D プレビューを別プロセスで開く。

    - ``push(sample, controls, controls_stamp)``: IMU 6値と同じ入力源の操作状態、
      その取得時刻を渡す。姿勢は毎サンプル積分し、最新値を共有メモリへ公開する。
    - 正面リセットは子の表示基準だけを変更する。現在の持ち方を正面とし、
      以後はその姿勢からの相対回転を表示する。積分状態と送出値は変えない。
    - ``close()`` は stop を立て、join を bounded に待ってから terminate/kill。
      再 ``start()`` できる。close 後も送信側へ影響を与えない。

    子プロセスの起動失敗は ``start()`` が ``RuntimeError`` を投げる。
    起動後に落ちた場合は ``is_alive()`` が False になり、``last_error`` /
    ``exitcode`` で理由を確認できる。
    """

    def __init__(self) -> None:
        self._ctx = multiprocessing.get_context("spawn")
        self._process: Any = None
        self._snapshot: Any = None
        self._stop_event: Any = None
        self._ready_event: Any = None
        self._error_buf: Any = None
        self._write_lock: Any = threading.Lock()
        self._orientation = Orientation()
        self._last_stamp: float | None = None
        self._last_error_text = ""
        self._last_exitcode: int | None = None

    # ------------------------------------------------------------------ 公開API
    def start(self) -> None:
        """子プロセスを起動する。すでに生きていれば何もしない。

        失敗（pygame なし、spawn 失敗、子の即時エラー）は例外にする。
        """
        if self._process is not None:
            if self._process.is_alive():
                return
            self._close_process()

        try:
            pygame_spec = importlib.util.find_spec("pygame")
        except Exception:
            pygame_spec = None
        if pygame_spec is None:
            raise RuntimeError("pygame が見つかりません（IMU 3Dプレビューに必要です）")

        self._last_error_text = ""
        self._last_exitcode = None
        self._last_stamp = None
        self._orientation.reset()
        self._snapshot = self._ctx.RawArray(ctypes.c_double, _SNAPSHOT_LEN)
        self._stop_event = self._ctx.Event()
        self._ready_event = self._ctx.Event()
        self._error_buf = self._ctx.RawArray(ctypes.c_char, _ERROR_BYTES)

        process = self._ctx.Process(
            target=_preview_main,
            args=(
                self._snapshot,
                self._stop_event,
                self._ready_event,
                self._error_buf,
            ),
            name="pokecon-imu-preview",
            daemon=True,
        )
        self._process = process
        try:
            process.start()
        except Exception as exc:
            self._process = None
            self._clear_shared()
            raise RuntimeError(f"3Dプレビューを起動できません: {exc}") from exc

        self._wait_ready(process)

    def push(
        self,
        sample: tuple[int, int, int, int, int, int],
        controls: PreviewControls,
        controls_stamp: float,
    ) -> None:
        """生サンプル 1 件を積分して最新姿勢を子へ公開する（非ブロッキング）。"""
        if not self.is_alive():
            return

        now = time.monotonic()
        last = self._last_stamp
        self._last_stamp = now
        if last is None:
            dt = 0.0
        else:
            dt = now - last
            if dt <= 0.0 or dt > MAX_STEP_SECONDS:
                # ギャップは回転を飛ばす。ドメインも >0.1s は積分しない。
                dt = 0.0

        self._orientation.update(sample, dt)
        self._publish(self._orientation.quaternion, now, controls, controls_stamp)

    def close(self) -> None:
        """子を止めて共有資源を解放する。多重呼び出し・未起動でも安全。"""
        self._close_process()
        self._clear_shared()

    def is_alive(self) -> bool:
        process = self._process
        if process is None:
            return False
        try:
            return bool(process.is_alive())
        except Exception:
            return False

    @property
    def last_error(self) -> str:
        """子が書き残したエラー（トレースバック）。なければ空文字。"""
        return self._read_error()

    @property
    def exitcode(self) -> int | None:
        process = self._process
        if process is None:
            return self._last_exitcode
        try:
            return process.exitcode
        except Exception:
            return None

    # ------------------------------------------------------------------ 内部
    def _publish(
        self,
        quaternion: tuple[float, float, float, float],
        stamp: float,
        controls: PreviewControls,
        controls_stamp: float,
    ) -> None:
        snapshot = self._snapshot
        lock = self._write_lock
        if snapshot is None or lock is None:
            return
        if not lock.acquire(False):
            # 別スレッドが書き中。次のサンプルで十分。
            return
        try:
            sequence = int(snapshot[_SEQ_INDEX])
            # 奇数 = 書き込み中。子はこの間を読まない。
            snapshot[_SEQ_INDEX] = float(sequence + 1)
            snapshot[0] = float(quaternion[0])
            snapshot[1] = float(quaternion[1])
            snapshot[2] = float(quaternion[2])
            snapshot[3] = float(quaternion[3])
            snapshot[_STAMP_INDEX] = float(stamp)
            snapshot[6] = float(controls.buttons)
            snapshot[7] = controls.lx
            snapshot[8] = controls.ly
            snapshot[9] = controls.rx
            snapshot[10] = controls.ry
            snapshot[11] = float(controls.connected)
            snapshot[_CONTROLS_STAMP_INDEX] = controls_stamp
            snapshot[_SEQ_INDEX] = float(sequence + 2)
        finally:
            lock.release()

    def _wait_ready(self, process: Any) -> None:
        ready = self._ready_event
        deadline = time.monotonic() + START_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if ready is not None and ready.wait(0.05):
                return
            if self._read_error():
                break
            if not process.is_alive():
                break
        if ready is not None and ready.is_set():
            return
        error = self._read_error()
        if error or not process.is_alive():
            self.close()
            raise RuntimeError(error or "3Dプレビューの初期化に失敗しました")

    def _close_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            stop = self._stop_event
            if stop is not None:
                stop.set()
            if process.is_alive():
                process.join(CLOSE_JOIN_S)
            if process.is_alive():
                process.terminate()
                process.join(TERMINATE_JOIN_S)
            if process.is_alive():
                process.kill()
                process.join(TERMINATE_JOIN_S)
        except Exception:
            pass
        # 終了直前のエラー / 終了コードも残す。
        self._read_error()
        try:
            self._last_exitcode = process.exitcode
        except Exception:
            self._last_exitcode = None
        try:
            process.close()
        except Exception:
            pass

    def _clear_shared(self) -> None:
        self._snapshot = None
        self._stop_event = None
        self._ready_event = None
        self._error_buf = None
        self._last_stamp = None

    def _read_error(self) -> str:
        buffer = self._error_buf
        if buffer is None:
            return self._last_error_text
        try:
            raw = bytes(buffer.raw).split(b"\0", 1)[0]
        except Exception:
            return self._last_error_text
        text = raw.decode("utf-8", "replace").strip()
        if text:
            self._last_error_text = text
            return text
        return self._last_error_text


# ====================================================================== 子プロセス


def _preview_main(
    snapshot: Any,
    stop_event: Any,
    ready_event: Any,
    error_buf: Any,
) -> None:
    """spawn 子プロセスの入口（トップレベル関数）。

    pygame の import / 初期化に失敗しても親を巻き込まず、エラーを共有バッファと
    stderr に残して終了する。stdout には何も出さない。
    """
    pygame: Any = None
    try:
        # pygame の起動メッセージを抑止。SDL の窓は画面中央へ。
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        os.environ.setdefault("SDL_VIDEO_CENTERED", "1")
        import pygame as pygame_module  # noqa: PLC0415

        pygame = pygame_module
        _run_preview(pygame, snapshot, stop_event, ready_event)
    except BaseException:
        error = traceback.format_exc()
        _write_child_error(error_buf, error)
        try:
            print(error, file=sys.stderr)
        except Exception:
            pass
    finally:
        if pygame is not None:
            try:
                pygame.quit()
            except Exception:
                pass


def _run_preview(
    pygame: Any,
    snapshot: Any,
    stop_event: Any,
    ready_event: Any,
) -> None:
    from pokecon.ui.imu_preview_gpu import PreviewRenderer

    pygame.display.init()
    pygame.font.init()
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 3)
    pygame.display.gl_set_attribute(
        pygame.GL_CONTEXT_PROFILE_MASK, pygame.GL_CONTEXT_PROFILE_CORE
    )
    pygame.display.gl_set_attribute(pygame.GL_DEPTH_SIZE, 24)
    pygame.display.set_mode((WINDOW_W, WINDOW_H), pygame.OPENGL | pygame.DOUBLEBUF)
    pygame.display.set_caption(_window_caption(1))
    clock = pygame.time.Clock()

    fonts = _Fonts(pygame)
    renderer = PreviewRenderer(pygame, (WINDOW_W, WINDOW_H))
    overlay = pygame.Surface((WINDOW_W, WINDOW_H), pygame.SRCALPHA)

    model_id = 1
    quaternion = (1.0, 0.0, 0.0, 0.0)
    orientation_reference = quaternion
    stamp = 0.0
    sequence = 0
    reset_pending = False
    controls = PreviewControls()
    controls_stamp = 0.0

    if ready_event is not None:
        ready_event.set()

    running = True
    try:
        while running and not (stop_event is not None and stop_event.is_set()):
            now = time.monotonic()
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                    continue
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        running = False
                    elif event.key in (pygame.K_1, pygame.K_KP1):
                        model_id = 1
                        pygame.display.set_caption(_window_caption(1))
                    elif event.key in (pygame.K_2, pygame.K_KP2):
                        model_id = 2
                        pygame.display.set_caption(_window_caption(2))
                    elif event.key in (pygame.K_3, pygame.K_KP3):
                        model_id = 3
                        pygame.display.set_caption(_window_caption(3))
                    elif event.key == pygame.K_r:
                        reset_pending = True
                    continue
                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if _point_in_rect(_RESET_RECT, event.pos):
                        reset_pending = True
                    else:
                        for index, rect in enumerate(_TAB_RECTS, start=1):
                            if _point_in_rect(rect, event.pos):
                                model_id = index
                                pygame.display.set_caption(_window_caption(index))
                                break

            latest = _read_snapshot(snapshot, sequence)
            if latest is not None:
                quaternion, stamp, sequence, controls, controls_stamp = latest
            if reset_pending and stamp > 0.0:
                # 新しい入力を待たず、最後に受け取った姿勢をその場で正面にする。
                orientation_reference = quaternion
                reset_pending = False
            display_quaternion = relative_orientation(quaternion, orientation_reference)

            age = (now - stamp) if stamp > 0.0 else None
            waiting = age is None
            stale = age is not None and age > STALE_SECONDS
            if waiting:
                dim = 0.55
            elif stale:
                dim = 0.68
            else:
                dim = 1.0
            if now - controls_stamp > STALE_SECONDS or not controls.connected:
                controls = PreviewControls()

            overlay.fill((0, 0, 0, 0))
            _draw_ui(
                pygame,
                overlay,
                fonts,
                model_id,
                display_quaternion,
                stamp,
                age,
                reset_pending,
            )
            renderer.render(display_quaternion, model_id, dim, overlay, controls)
            pygame.display.flip()
            clock.tick(FPS)
    finally:
        renderer.close()


def _write_child_error(buffer: Any, text: str) -> None:
    if buffer is None:
        return
    try:
        raw = text.encode("utf-8", "replace")[: _ERROR_BYTES - 1]
        for index, value in enumerate(raw):
            buffer[index] = bytes((value,))
        buffer[len(raw)] = b"\0"
    except Exception:
        pass


def _read_snapshot(
    snapshot: Any, last_sequence: int
) -> (
    tuple[tuple[float, float, float, float], float, int, PreviewControls, float] | None
):
    """seqlock で最新スナップショットを読む。新しくなければ None。"""
    if snapshot is None:
        return None
    for _ in range(4):
        try:
            first = int(snapshot[_SEQ_INDEX])
        except Exception:
            return None
        if first <= 0 or (first & 1):
            return None
        quaternion = (
            float(snapshot[0]),
            float(snapshot[1]),
            float(snapshot[2]),
            float(snapshot[3]),
        )
        stamp = float(snapshot[_STAMP_INDEX])
        controls = PreviewControls(
            buttons=int(snapshot[6]),
            lx=float(snapshot[7]),
            ly=float(snapshot[8]),
            rx=float(snapshot[9]),
            ry=float(snapshot[10]),
            connected=bool(snapshot[11]),
        )
        controls_stamp = float(snapshot[_CONTROLS_STAMP_INDEX])
        try:
            second = int(snapshot[_SEQ_INDEX])
        except Exception:
            return None
        if first == second:
            if first == last_sequence:
                return None
            return quaternion, stamp, first, controls, controls_stamp
    return None


# ====================================================================== 描画


class _Fonts:
    """日本語フォントを 1 度だけ探してサイズ別に保持する。"""

    def __init__(self, pygame: Any) -> None:
        self._pygame = pygame
        path = None
        for name in _FONT_CANDIDATES:
            try:
                path = pygame.font.match_font(name)
            except Exception:
                path = None
            if path:
                break
        self._path = path
        self.title = self._load(22, bold=True)
        self.subtitle = self._load(12)
        self.tab = self._load(16)
        self.status = self._load(15)
        self.hint = self._load(13)
        self.q = self._load(12)

    def _load(self, size: int, bold: bool = False) -> Any:
        if self._path:
            font = self._pygame.font.Font(self._path, size)
        else:
            font = self._pygame.font.Font(None, size)
        font.set_bold(bold)
        return font


def _draw_ui(
    pygame: Any,
    surface: Any,
    fonts: _Fonts,
    model_id: int,
    quaternion: tuple[float, float, float, float],
    stamp: float,
    age: float | None,
    reset_pending: bool,
) -> None:
    pygame.draw.rect(surface, (13, 17, 24, 250), (0, 0, WINDOW_W, 101))
    pygame.draw.rect(surface, (24, 31, 44, 250), (0, 472, WINDOW_W, 68))
    pygame.draw.line(surface, (45, 55, 75), (0, 100), (WINDOW_W, 100))
    pygame.draw.line(surface, (45, 55, 75), (0, 472), (WINDOW_W, 472))

    _text(surface, fonts.title, "IMU 3D ジャイロプレビュー", (20, 12), _TEXT)
    _text(
        surface,
        fonts.subtitle,
        "Nintendo Switch コントローラー姿勢（送出値の積分・ヨーは磁気補正なし）",
        (20, 40),
        _MUTED,
    )

    mouse = pygame.mouse.get_pos()
    for index, (rect, label, accent) in enumerate(
        zip(_TAB_RECTS, _MODEL_NAMES, _MODEL_ACCENTS), start=1
    ):
        active = index == model_id
        hovered = _point_in_rect(rect, mouse)
        if active:
            base = _mix(accent, (18, 22, 30), 0.72)
        else:
            base = _PANEL_HOVER if hovered else _PANEL
        pygame.draw.rect(surface, base, rect, border_radius=8)
        if active:
            pygame.draw.rect(surface, accent, rect, 1, border_radius=8)
        pygame.draw.circle(surface, accent, (rect[0] + 13, rect[1] + 15), 4)
        label_surface = fonts.tab.render(label, True, _TEXT if active else _MUTED)
        surface.blit(
            label_surface,
            label_surface.get_rect(midleft=(rect[0] + 24, rect[1] + rect[3] // 2)),
        )

    reset_hovered = _point_in_rect(_RESET_RECT, mouse)
    reset_base = (66, 50, 36) if reset_pending else (48, 42, 36)
    if reset_hovered:
        reset_base = _mix(reset_base, (255, 255, 255), 0.08)
    pygame.draw.rect(surface, reset_base, _RESET_RECT, border_radius=8)
    pygame.draw.rect(
        surface,
        _WARN if reset_pending else (98, 78, 52),
        _RESET_RECT,
        1,
        border_radius=8,
    )
    reset_label = fonts.tab.render("正面リセット", True, _WARN)
    surface.blit(
        reset_label,
        reset_label.get_rect(
            center=(
                _RESET_RECT[0] + _RESET_RECT[2] // 2,
                _RESET_RECT[1] + _RESET_RECT[3] // 2,
            )
        ),
    )
    if reset_hovered:
        # 送出値ではなく、表示の基準だけを変える。
        tip_rect = (430, 94, 280, 24)
        pygame.draw.rect(surface, (42, 35, 26), tip_rect, border_radius=6)
        pygame.draw.rect(surface, _WARN, tip_rect, 1, border_radius=6)
        tip = fonts.hint.render("今の持ち方を正面の基準にする", True, _WARN)
        surface.blit(tip, tip.get_rect(center=(570, 106)))

    if stamp > 0.0:
        q_text = (
            f"W {quaternion[0]:+.3f}   X {quaternion[1]:+.3f}   "
            f"Y {quaternion[2]:+.3f}   Z {quaternion[3]:+.3f}"
        )
    else:
        q_text = "W ---   X ---   Y ---   Z ---"
    _text(surface, fonts.q, q_text, (700, 18), _MUTED, anchor="topright")

    if reset_pending:
        status = "リセット待ち：最初のサンプルの姿勢を正面の基準にします"
        status_color = _WARN
    elif age is None:
        status = "IMUサンプル待ち：パネルでジャイロ送出を開始してください"
        status_color = _MUTED
    elif age > STALE_SECONDS:
        status = f"入力停止：最終受信から {age:.1f} 秒"
        status_color = _DANGER
    else:
        status = f"受信中：最終受信 {age * 1000.0:.0f} ms 前"
        status_color = _OK

    pygame.draw.circle(surface, status_color, (24, 496), 4)
    _text(surface, fonts.status, status, (38, 484), status_color)
    _text(
        surface,
        fonts.hint,
        "1/2/3 モデル切替　R 今の持ち方を正面にする　Esc 閉じる",
        (20, 512),
        _MUTED,
    )

    if age is None:
        _badge(pygame, surface, fonts, "待機中", (78, 72, 46), _WARN)
    elif age > STALE_SECONDS:
        _badge(pygame, surface, fonts, "入力停止", (86, 36, 36), _DANGER)


def _badge(
    pygame: Any,
    surface: Any,
    fonts: _Fonts,
    label: str,
    background: tuple[int, int, int],
    foreground: tuple[int, int, int],
) -> None:
    rect = (300, 112, 120, 28)
    pygame.draw.rect(surface, background, rect, border_radius=14)
    pygame.draw.rect(surface, foreground, rect, 1, border_radius=14)
    image = fonts.hint.render(label, True, foreground)
    surface.blit(image, image.get_rect(center=(360, 126)))


def _window_caption(model_id: int) -> str:
    index = model_id - 1
    name = _MODEL_NAMES[index] if 0 <= index < len(_MODEL_NAMES) else _MODEL_NAMES[0]
    return f"PokeCon 3Dジャイロプレビュー - {name}"


def _mix(
    first: tuple[int, int, int],
    second: tuple[int, int, int],
    amount: float,
) -> tuple[int, int, int]:
    return (
        max(0, min(255, int(first[0] + (second[0] - first[0]) * amount))),
        max(0, min(255, int(first[1] + (second[1] - first[1]) * amount))),
        max(0, min(255, int(first[2] + (second[2] - first[2]) * amount))),
    )


def _point_in_rect(rect: tuple[int, int, int, int], point: tuple[int, int]) -> bool:
    return (
        rect[0] <= point[0] < rect[0] + rect[2]
        and rect[1] <= point[1] < rect[1] + rect[3]
    )


def _text(
    surface: Any,
    font: Any,
    text: str,
    position: tuple[int, int],
    color: tuple[int, int, int],
    anchor: str = "topleft",
) -> None:
    image = font.render(text, True, color)
    rect = image.get_rect()
    setattr(rect, anchor, position)
    surface.blit(image, rect)


__all__ = ["MotionPreview"]
