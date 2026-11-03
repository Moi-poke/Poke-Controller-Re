"""CommandVision の待ち・位置・複数検出・GPU 入口の検証。実機なし。

テンプレートは tmp_path に実ファイルとして置く。合成フレームへ同じ絵を
貼り、TEMPLATE_PATH を tmp_path へ向けて相対パスの解決まで通す。
待ちは偽時計（wait / _deadline / _runElapsed）で進めるので実時間を待たない。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from Commands.PythonCommandBase import ImageProcPythonCommand
from pokecon.domain import CommandVision

FRAME_H, FRAME_W = 120, 160
PATCH_H, PATCH_W = 20, 24
# 貼る位置と、そこから導かれるテンプレート中心（画面全体の座標）。
POS_A = (40, 30)
POS_B = (110, 70)
CENTER_A = (POS_A[0] + PATCH_W // 2, POS_A[1] + PATCH_H // 2)
CENTER_B = (POS_B[0] + PATCH_W // 2, POS_B[1] + PATCH_H // 2)


def _pattern(seed: int = 7) -> np.ndarray:
    """照合に使うムラのある絵。定数だと TM_CCOEFF_NORMED が NaN になる。"""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, (PATCH_H, PATCH_W), dtype=np.uint8)


def _frame(*placements: tuple[np.ndarray, int, int]) -> np.ndarray:
    """暗い背景にグレースケールの絵を貼った BGR フレームを作る。"""
    frame = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)
    for patch, x, y in placements:
        height, width = patch.shape[:2]
        frame[y : y + height, x : x + width] = patch[..., None]
    return frame


def _noise_frame(seed: int) -> np.ndarray:
    """毎回まったく違う絵（動き続ける画面の代役）。"""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, (FRAME_H, FRAME_W, 3), dtype=np.uint8)


def _write_template(directory: Path, name: str, image: np.ndarray) -> str:
    """PNG を実ファイルとして書き、その絶対パスを返す。"""
    filespec = directory / name
    filespec.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", image)
    assert ok, f"PNG へ変換できません: {name}"
    filespec.write_bytes(encoded.tobytes())
    return str(filespec)


class _FakeCam:
    """合成フレームを順に返す撮り口。最後の1枚は繰り返す。"""

    def __init__(self, *frames: np.ndarray) -> None:
        assert frames, "少なくとも1枚は要る"
        self._frames = list(frames)
        self.reads = 0

    def readFrame(self, copy: bool = False) -> np.ndarray:
        _ = copy
        self.reads += 1
        index = min(self.reads, len(self._frames)) - 1
        return self._frames[index]


class _Cmd(ImageProcPythonCommand):
    """待ちだけを偽時計へ差し替えた画像認識コマンド。

    wait が進める時計を _deadline / _runElapsed も見るので、timeout と
    quiet は「何回 poll したか」だけで決まり、実時間に依存しない。
    """

    def __init__(self, camera: Any) -> None:
        super().__init__(camera)
        self.clock = 0.0
        self.waits: list[float] = []

    def do(self) -> None:
        pass

    def wait(self, seconds: float = 0.0) -> None:
        self.waits.append(float(seconds))
        self.clock += max(float(seconds), 0.0)

    def _deadline(self, timeout: float) -> Callable[[], bool]:
        start = self.clock
        limit = float(timeout)
        return lambda: self.clock - start >= limit

    def _runElapsed(self) -> float:
        return self.clock


@pytest.fixture(autouse=True)
def _isolated_templates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """TEMPLATE_PATH をテストごとの場所へ向け、画像 cache を前後で捨てる。"""
    monkeypatch.setattr(CommandVision, "TEMPLATE_PATH", str(tmp_path))
    CommandVision._imread_or_raise.cache_clear()
    try:
        yield tmp_path
    finally:
        CommandVision._imread_or_raise.cache_clear()


# -- 待つ ------------------------------------------------------------------


def test_wait_template_returns_true_when_it_appears(
    tmp_path: Path,
) -> None:
    """1回目は無し・2回目で出る。出た時点で待たずに True。"""
    _write_template(tmp_path, "appear.png", _pattern())
    cam = _FakeCam(_frame(), _frame((_pattern(), *POS_A)))
    cmd = _Cmd(cam)

    assert cmd.waitTemplate("appear.png", timeout=1.0, interval=0.25, threshold=0.9)

    assert cam.reads == 2
    assert cmd.waits == [0.25]


def test_wait_template_returns_false_after_timeout(tmp_path: Path) -> None:
    """出ないまま timeout を過ぎたら False。interval ごとに1回だけ照合する。"""
    _write_template(tmp_path, "never.png", _pattern())
    cam = _FakeCam(_frame())
    cmd = _Cmd(cam)

    assert not cmd.waitTemplate("never.png", timeout=0.5, interval=0.1, threshold=0.9)

    assert len(cmd.waits) == 5
    assert cam.reads == len(cmd.waits) + 1


def test_wait_template_gone_returns_true_when_it_disappears(
    tmp_path: Path,
) -> None:
    """出ている間は待ち、消えた回で True。"""
    _write_template(tmp_path, "gone.png", _pattern())
    cam = _FakeCam(_frame((_pattern(), *POS_A)), _frame())
    cmd = _Cmd(cam)

    assert cmd.waitTemplateGone("gone.png", timeout=1.0, interval=0.25, threshold=0.9)

    assert cam.reads == 2
    assert cmd.waits == [0.25]


def test_wait_template_gone_returns_false_while_still_present(
    tmp_path: Path,
) -> None:
    """出たままなら timeout で False。"""
    _write_template(tmp_path, "stays.png", _pattern())
    cam = _FakeCam(_frame((_pattern(), *POS_A)))
    cmd = _Cmd(cam)

    assert not cmd.waitTemplateGone(
        "stays.png", timeout=0.3, interval=0.1, threshold=0.9
    )

    assert len(cmd.waits) == 3


def test_wait_stable_returns_true_after_quiet_window() -> None:
    """絵が動かないまま quiet 秒続いたら True。"""
    cam = _FakeCam(_frame())
    cmd = _Cmd(cam)

    assert cmd.waitStable(quiet=0.3, timeout=1.0, interval=0.1)

    # 静止を見始めてから quiet 以上経ってから返している。
    assert cmd.clock >= 0.3


def test_wait_stable_returns_false_when_frames_keep_changing() -> None:
    """毎フレーム別の絵なら止まったとみなさず、timeout で False。"""
    cam = _FakeCam(*(_noise_frame(seed) for seed in range(8)))
    cmd = _Cmd(cam)

    assert not cmd.waitStable(quiet=0.3, timeout=0.35, interval=0.1)

    assert cmd.clock >= 0.35


def test_wait_stable_restarts_quiet_window_after_motion() -> None:
    """揺れている間は静止と数えず、止まったところから quiet を数え直す。"""
    cam = _FakeCam(_noise_frame(1), _noise_frame(2), _frame(), _frame(), _frame())
    cmd = _Cmd(cam)

    assert cmd.waitStable(quiet=0.3, timeout=2.0, interval=0.1)

    # 2回目の poll で止まったので、そこから quiet 以上かかっている。
    assert cmd.clock >= 0.5


# -- 位置・複数検出 --------------------------------------------------------


def test_get_template_position_returns_center(tmp_path: Path) -> None:
    """一致した絵の中心を返し、無ければ None。"""
    _write_template(tmp_path, "here.png", _pattern())
    miss = _write_template(tmp_path, "elsewhere.png", _pattern(seed=123))
    cmd = _Cmd(_FakeCam(_frame((_pattern(), *POS_A))))

    assert cmd.getTemplatePosition("here.png", threshold=0.9) == CENTER_A
    assert cmd.getTemplatePosition(miss, threshold=0.9) is None


def test_get_template_position_maps_crop_back_to_screen(tmp_path: Path) -> None:
    """crop した中の座標ではなく、画面全体の座標で返す。"""
    _write_template(tmp_path, "here.png", _pattern())
    cmd = _Cmd(_FakeCam(_frame((_pattern(), *POS_A))))

    position = cmd.getTemplatePosition(
        "here.png", threshold=0.9, crop=[20, 10, 140, 110]
    )

    assert position == CENTER_A


def test_find_all_templates_finds_every_copy(tmp_path: Path) -> None:
    """同じ絵を2つ置いたら2件。座標は画面全体で返る。"""
    _write_template(tmp_path, "twice.png", _pattern())
    image = _frame((_pattern(), *POS_A), (_pattern(), *POS_B))
    cmd = _Cmd(_FakeCam(image))

    found = cmd.findAllTemplates("twice.png", threshold=0.9)
    assert sorted(found) == sorted([CENTER_A, CENTER_B])
    assert cmd.countTemplate("twice.png", threshold=0.9) == 2


def test_count_template_respects_max_count(tmp_path: Path) -> None:
    """max_count を超えて数えない（待ち側の要求数を守る）。"""
    _write_template(tmp_path, "twice.png", _pattern())
    image = _frame((_pattern(), *POS_A), (_pattern(), *POS_B))
    cmd = _Cmd(_FakeCam(image))

    assert cmd.countTemplate("twice.png", threshold=0.9, max_count=1) == 1


def test_is_contain_template_max_returns_first_hit_when_breaking(
    tmp_path: Path,
) -> None:
    """break_on_hit では最初に閾値を超えた添字を返し、未評価分は 0.0 で埋める。

    2枚目（完全一致）のほうが相関が高いが、1枚目で打ち切るため添字は 0。
    """
    exact = _pattern()
    other = np.random.default_rng(99).integers(0, 256, exact.shape, dtype=np.uint8)
    weak = np.clip(
        0.7 * exact.astype(np.float64) + 0.3 * other.astype(np.float64), 0, 255
    ).astype(np.uint8)
    weak_path = _write_template(tmp_path, "weak.png", weak)
    exact_path = _write_template(tmp_path, "exact.png", exact)
    cmd = _Cmd(_FakeCam(_frame((exact, *POS_A))))

    index, values, judged = cmd.isContainTemplate_max(
        [weak_path, exact_path], threshold=0.85, break_on_hit=True
    )

    assert int(index) == 0
    assert judged == [True, False]
    assert values[1] == 0.0  # 2枚目は評価していない


def test_is_contain_template_max_keyword_list_picks_highest(
    tmp_path: Path,
) -> None:
    """キーワード形（template_path_list=）も受け、打ち切らなければ最大を返す。"""
    exact = _pattern()
    other = np.random.default_rng(99).integers(0, 256, exact.shape, dtype=np.uint8)
    weak = np.clip(
        0.7 * exact.astype(np.float64) + 0.3 * other.astype(np.float64), 0, 255
    ).astype(np.uint8)
    weak_path = _write_template(tmp_path, "weak.png", weak)
    exact_path = _write_template(tmp_path, "exact.png", exact)
    cmd = _Cmd(_FakeCam(_frame((exact, *POS_A))))

    index, values, judged = cmd.isContainTemplate_max(
        template_path_list=[weak_path, exact_path], threshold=0.85
    )

    assert int(index) == 1
    assert judged == [True, True]
    assert values[0] < values[1]


def test_is_contain_template_max_rejects_empty_list() -> None:
    """空の一覧は「全部外れ」ではなく呼び出し側の誤りとして断る。"""
    cmd = _Cmd(_FakeCam(_frame()))

    with pytest.raises(ValueError, match="空"):
        cmd.isContainTemplate_max([])


# -- 記録・先読み ----------------------------------------------------------


def test_is_contain_template_dump_saves_only_on_miss(tmp_path: Path) -> None:
    """外れたときだけフォルダを作って画面を残す。当たった回は残さない。"""
    _write_template(tmp_path, "target.png", _pattern())
    miss_dir = tmp_path / "miss"
    miss = _Cmd(_FakeCam(_frame()))

    assert not miss.isContainTemplateDump(
        "target.png", threshold=0.9, folder=str(miss_dir)
    )
    saved = list(miss_dir.glob("*.png"))
    assert len(saved) == 1
    assert "target" in saved[0].name

    hit_dir = tmp_path / "hit"
    hit = _Cmd(_FakeCam(_frame((_pattern(), *POS_A))))
    assert hit.isContainTemplateDump("target.png", threshold=0.9, folder=str(hit_dir))
    assert not hit_dir.exists()


def test_preload_templates_lists_only_unreadable_paths(tmp_path: Path) -> None:
    """読めたものは返さず、無い・壊れたものだけを入力順で返す。"""
    _write_template(tmp_path / "日本語", "good.png", _pattern())
    (tmp_path / "broken.png").write_bytes(b"not a png")
    cmd = _Cmd(_FakeCam(_frame()))

    assert cmd.preloadTemplates(["日本語/good.png", "broken.png", "missing.png"]) == [
        "broken.png",
        "missing.png",
    ]
    # カラー読みでも同じ判定になる（flags の分岐を色付きで通す）。
    assert cmd.preloadTemplates(["日本語/good.png"], use_gray=False) == []


# -- GPU -------------------------------------------------------------------


def test_ensure_cuda_false_when_no_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CUDA ビルドでも実機が無ければ False（CPU だけで動く）。"""
    monkeypatch.setattr(cv2.cuda, "getCudaEnabledDeviceCount", lambda: 0)
    cmd = _Cmd(_FakeCam(_frame((_pattern(), *POS_A))))

    assert cmd._ensure_cuda() is False
    assert cmd.gsrc is None
    assert cmd.gtmpl is None
    assert cmd.gresult is None


def test_ensure_cuda_false_without_gpumat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CUDA 無効ビルド（cuda_GpuMat が無い）でも落ちずに False。"""
    monkeypatch.delattr(cv2, "cuda_GpuMat", raising=False)
    cmd = _Cmd(_FakeCam(_frame()))

    assert cmd._ensure_cuda() is False


def test_ensure_cuda_false_when_driver_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ドライバ問い合わせが例外でも、コマンドを巻き込まず False。"""

    def boom() -> int:
        raise RuntimeError("driver missing")

    monkeypatch.setattr(cv2.cuda, "getCudaEnabledDeviceCount", boom)
    cmd = _Cmd(_FakeCam(_frame()))

    assert cmd._ensure_cuda() is False
    assert cmd.gsrc is None


def test_ensure_cuda_true_when_prepared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一度用意できたなら、CUDA を問い合わせ直さず True。"""
    cmd = _Cmd(_FakeCam(_frame()))
    cmd.gsrc = object()

    def boom() -> int:
        raise AssertionError("用意済みなのに問い合わせた")

    monkeypatch.setattr(cv2.cuda, "getCudaEnabledDeviceCount", boom)

    assert cmd._ensure_cuda() is True


def test_is_contain_template_gpu_rejects_color() -> None:
    """GPU 版はグレースケール専用。カラーは黙って誤結果にせず断る。"""
    cmd = _Cmd(_FakeCam(_frame()))

    with pytest.raises(ValueError, match="グレースケール"):
        cmd.isContainTemplateGPU("colour.png", use_gray=False)


def test_is_contain_template_gpu_without_cuda_points_to_cpu_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CUDA が無い環境では GPU 版は撮らずに止まり、CPU 版を案内する。"""
    monkeypatch.setattr(cv2.cuda, "getCudaEnabledDeviceCount", lambda: 0)
    _write_template(tmp_path, "target.png", _pattern())
    cam = _FakeCam(_frame((_pattern(), *POS_A)))
    cmd = _Cmd(cam)

    with pytest.raises(RuntimeError, match=r"isContainTemplate\(\)"):
        cmd.isContainTemplateGPU("target.png", threshold=0.9)

    assert cam.reads == 0  # 照合の前に断る
    assert cmd.isContainTemplate("target.png", threshold=0.9)


def test_clear_cuda_cache_drops_matcher_and_template_state() -> None:
    """作り置きしたマッチャとテンプレートの両方を捨てる。"""
    cmd = _Cmd(_FakeCam(_frame()))
    cmd._cuda_matchers[(cv2.CV_8UC1, cv2.TM_CCOEFF_NORMED)] = object()
    cmd._cuda_templates[("a.png", True)] = object()

    cmd.clearCudaCache()

    assert cmd._cuda_matchers == {}
    assert cmd._cuda_templates == {}


def test_clear_template_caches_resets_cpu_gpu_and_match_state(
    tmp_path: Path,
) -> None:
    """CPU の画像 cache・GPU cache・照合結果 cache をまとめて捨てる。"""
    target = _write_template(tmp_path, "target.png", _pattern())
    cmd = _Cmd(_FakeCam(_frame((_pattern(), *POS_A))))
    cmd.preloadTemplates([target])
    assert CommandVision._imread_or_raise.cache_info().currsize == 1

    cmd._cuda_matchers[(cv2.CV_8UC1, cv2.TM_CCOEFF_NORMED)] = object()
    cmd._cuda_templates[("target.png", True)] = object()
    cmd._src_cache[(1,)] = object()
    cmd._match_cache[(2,)] = object()
    cmd._cached_seq = 7

    cmd.clearTemplateCaches()

    assert CommandVision._imread_or_raise.cache_info().currsize == 0
    assert cmd._cuda_matchers == {}
    assert cmd._cuda_templates == {}
    assert cmd._src_cache == {}
    assert cmd._match_cache == {}
    assert cmd._cached_seq is None
