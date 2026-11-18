"""Blockly HTTP サーバの画像フレーム・テンプレート・色判定ルート Mixin。"""

from __future__ import annotations

import base64
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pokecon.runtime import blockly_capture, blockly_color, blockly_match


def _ed() -> Any:
    """差し替え対象モジュールを実行時に引き、テストでの上書きを保つ。"""
    from pokecon.ui import blockly_editor

    return blockly_editor


class BlocklyMediaRoutesMixin:
    """`/frame`・`/template_image`・`/template`・`/match`・`/color_ratio` 等。"""

    if TYPE_CHECKING:
        wfile: Any
        path: str

        def send_response(self, code: int, message: str | None = None) -> None: ...
        def send_header(self, keyword: str, value: str) -> None: ...
        def end_headers(self) -> None: ...
        def _reply(
            self, ok: bool, message: str, extra: dict[str, Any] | None = None
        ) -> None: ...

    def _send_binary(self, content_type: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _grab_camera_frame(self) -> bytes | None:
        getter = _ed()._GET_FRAME
        if getter is None:
            self._reply(False, blockly_capture.NO_CAMERA_MESSAGE)
            return None
        try:
            png: bytes | None = getter()
        except Exception:
            png = None
        if png is None:
            self._reply(False, blockly_capture.FRAME_FAIL_MESSAGE)
            return None
        return png

    def _resolve_image_source(
        self, payload: dict[str, Any], fail_prefix: str
    ) -> Any | None:
        """`upload`（base64 画像）または `frame`（カメラ画像）を復号して返す。"""
        if str(payload.get("source", "frame")) == "upload":
            try:
                # 復号は 1 回だけ（decode→ndarray→match/filter へ受渡し）。
                # 文言は従来の bytes API と同一に保つ。
                return blockly_match.decode_upload_array(str(payload.get("image", "")))
            except ValueError as e:
                self._reply(False, f"{fail_prefix}: {e}")
                return None
        return self._grab_camera_frame()

    def _handle_frame(self) -> None:
        png = self._grab_camera_frame()
        if png is not None:
            self._send_binary("image/png", png)

    def _handle_template_image(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        name = query.get("name", [""])[0]
        reason = blockly_capture.validate_template_name(name.strip())
        if reason is not None:
            self._reply(False, f"画像を出せません: {reason}")
            return
        base = Path(_ed().WindowUtils.APP_DIR) / "Template"
        target = (base / Path(str(name).replace("\\", "/"))).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError:
            self._reply(False, "画像を出せません: `..` は使えません")
            return
        content_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".bmp": "image/bmp",
        }
        ctype = content_types.get(target.suffix.lower())
        if ctype is None or not target.is_file():
            self._reply(False, f"画像を出せません: Template/{name} がありません")
            return
        try:
            body = target.read_bytes()
        except OSError as e:
            self._reply(False, f"画像を出せません: {e}")
            return
        self._send_binary(ctype, body)

    def _handle_save_template(self, payload: dict[str, Any]) -> None:
        png = self._grab_camera_frame()
        if png is None:
            return
        res = blockly_capture.save_template(
            _ed().WindowUtils.APP_DIR,
            str(payload.get("name", "")),
            png,
            payload.get("rect"),
        )
        print(res.message)
        extra = {"path": res.rel} if res.status == "saved" else {}
        self._reply(res.status == "saved", res.message, extra)

    def _handle_match(self, payload: dict[str, Any]) -> None:
        frame = self._resolve_image_source(payload, "照合できません")
        if frame is None:
            return
        res = blockly_match.match_template(
            _ed().WindowUtils.APP_DIR,
            frame,
            str(payload.get("template", "")),
            payload.get("threshold", 0.7),
            bool(payload.get("use_gray", True)),
            payload.get("crop", None),
        )
        print(res.message)
        extra = (
            {
                "score": res.score,
                "matched": res.matched,
                "count": res.count,
                "rect": res.rect,
            }
            if res.status == "ok"
            else {}
        )
        self._reply(res.status == "ok", res.message, extra)

    def _handle_color_ratio(self, payload: dict[str, Any]) -> None:
        frame = self._resolve_image_source(payload, "色を見られません")
        if frame is None:
            return
        ratio, err = blockly_color.ratio_on_png(
            frame,
            payload.get("lower"),
            payload.get("upper"),
            payload.get("crop", None),
        )
        if err is not None:
            self._reply(False, f"色を見られません: {err}")
            return
        message = f"割合 {ratio * 100:.1f}%"
        print(message)
        self._reply(True, message, {"ratio": ratio})

    def _handle_filter_preview(self, payload: dict[str, Any]) -> None:
        frame = self._resolve_image_source(payload, "色を見られません")
        if frame is None:
            return
        out, ratio, err = blockly_color.filter_png(
            frame,
            payload.get("lower"),
            payload.get("upper"),
            payload.get("mode"),
            payload.get("crop", None),
        )
        if err is not None or out is None:
            self._reply(False, f"色を見られません: {err}")
            return
        message = f"割合 {ratio * 100:.1f}%"
        print(message)
        image = base64.b64encode(out).decode("ascii")
        self._reply(True, message, {"ratio": ratio, "image": image})
