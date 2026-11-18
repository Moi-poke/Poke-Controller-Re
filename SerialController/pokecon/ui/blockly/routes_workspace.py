"""Blockly HTTP サーバの共通通信補助・ファイル保存/読込ルート Mixin。"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pokecon.runtime import blockly_save, blockly_templates


def _ed() -> Any:
    """差し替え対象モジュールを実行時に引き、テストでの上書きを保つ。"""
    from pokecon.ui import blockly_editor

    return blockly_editor


class BlocklyWorkspaceRoutesMixin:
    """Host/Origin 検証・JSON 送受信・`/list`・`/load`・`/save` 等を扱う。"""

    #: 受け付ける Host 名。localhost 専用の受け口のため、それ以外の名前で
    #: 届いた要求（DNS リバインディング）は断る。
    _LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})

    if TYPE_CHECKING:
        server: Any
        headers: Any
        rfile: Any
        wfile: Any
        path: str
        close_connection: bool

        def send_response(self, code: int, message: str | None = None) -> None: ...
        def send_header(self, keyword: str, value: str) -> None: ...
        def end_headers(self) -> None: ...

    def _refuse_foreign(self, *, is_post: bool) -> bool:
        """よそのページからの要求なら 403 で断り、真を返す。

        `/save` は `.py` を書き、一覧の作り直しで読み込まれる（＝コード実行）。
        待ち受け番号は固定のため、閲覧中の別サイトから狙われないよう、
        Host・Origin・JSON 種別で同じオリジンの編集画面だけを通す。
        JSON 種別はブラウザでは事前確認（preflight）が要り、ここは応じないため
        no-cors の単純 POST も届かない。
        """
        ed = _ed()
        port = self.server.server_address[1]
        host = str(self.headers.get("Host", "") or "")
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        reason = ""
        if host and name.lower() not in self._LOCAL_HOSTS:
            reason = "Host が違います"
        if is_post and not reason:
            origin = self.headers.get("Origin")
            allowed = {f"http://{h}:{port}" for h in self._LOCAL_HOSTS}
            if origin is not None and origin not in allowed:
                reason = "別のページからの要求です"
            ctype = str(self.headers.get("Content-Type", "") or "").lower()
            if not reason and not ctype.startswith("application/json"):
                reason = "JSON 以外の要求です"
        if not reason:
            return False
        if is_post:
            # 未読のまま閉じると RST で応答が届かないため、上限つきで読み捨てる。
            try:
                remaining = int(str(self.headers.get("Content-Length", "0")).strip())
                remaining = min(
                    max(remaining, 0), int(ed.MAX_UPLOAD_BODY_BYTES) + 65536
                )
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except Exception:
                pass
        body = json.dumps(
            {"ok": False, "message": f"受け付けません: {reason}"}, ensure_ascii=False
        ).encode("utf-8")
        self.close_connection = True
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _read_json(self, max_bytes: int | None = None) -> dict[str, Any] | None:
        """POST 本文を読む。壊れていたら応答済みで None を返す。"""
        limit = int(_ed().MAX_BODY_BYTES) if max_bytes is None else max_bytes
        try:
            length = int(str(self.headers.get("Content-Length", "0")).strip())
        except (TypeError, ValueError) as e:
            self._reply(False, f"保存できません: {e}")
            return None
        if length < 0 or length > limit:
            # 未読のまま閉じると RST で応答が届かないため読み捨てる。
            # 申告どおりに全部読むと巨大申告で固まるため、上限＋64KB で打ち切る。
            try:
                remaining = min(length, limit + 65536)
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except Exception:
                pass
            self.close_connection = True
            self._reply(False, "保存できません: 本文が大きすぎます")
            return None
        try:
            payload: Any = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._reply(False, f"保存できません: {e}")
            return None
        if not isinstance(payload, dict):
            self._reply(
                False,
                "受け付けません: JSONの形が違います（オブジェクトではありません）",
            )
            return None
        return payload

    def _reply(
        self, ok: bool, message: str, extra: dict[str, Any] | None = None
    ) -> None:
        body = json.dumps(
            {"ok": ok, "message": message, **(extra or {})}, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_list(self) -> None:
        app_dir = _ed().WindowUtils.APP_DIR
        stems = blockly_save.list_blockly(app_dir)
        # 置き場ごとの識別子。番地を固定したため、別インストールの編集画面とも
        # ブラウザ内の保存領域を共有する。控えをこれで分ける（中身は推測不能でよい）。
        app_id = hashlib.sha256(
            str(Path(app_dir).resolve()).encode("utf-8")
        ).hexdigest()[:16]
        self._reply(True, "", {"stems": stems, "appId": app_id})

    def _handle_load(self) -> None:
        app_dir = _ed().WindowUtils.APP_DIR
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        stem = query.get("stem", [""])[0]
        res = blockly_save.load_blockly(app_dir, stem)
        if res.status != "ok":
            self._reply(False, res.message)
            return
        # 手編集検出は保存時の要約値との比較（上書き保存で消えるため知らせる）。
        extra: dict[str, Any] = {
            "stem": stem,
            "workspaceJson": res.workspace_json,
            "externalEdit": res.external_edit,
        }
        self._reply(True, res.message, extra)

    def _handle_samples(self) -> None:
        # 作例ギャラリー用。壊れていても空の一覧で返す（編集は続けられる）。
        try:
            samples = blockly_save.list_samples(_ed().WindowUtils.APP_DIR)
        except Exception:
            samples = []
        self._reply(True, "", {"samples": samples})

    def _handle_templates(self) -> None:
        names = blockly_templates.list_image_templates(_ed().WindowUtils.APP_DIR)
        self._reply(True, "", {"templates": names})

    def _handle_delete(self, payload: dict[str, Any]) -> None:
        res = blockly_save.delete_blockly(
            _ed().WindowUtils.APP_DIR, str(payload.get("stem", ""))
        )
        print(res.message)
        self._reply(res.status == "deleted", res.message)

    def _handle_save(self, payload: dict[str, Any]) -> None:
        res = blockly_save.save_blockly(
            _ed().WindowUtils.APP_DIR,
            str(payload.get("stem", "")),
            str(payload.get("workspaceJson", "")),
            str(payload.get("pythonCode", "")),
        )
        print(res.message)
        self._reply(res.status == "saved", res.message, {"warnings": res.warnings})
