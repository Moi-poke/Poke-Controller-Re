"""Blockly HTTP サーバのライブ編集・試し実行・操作記録ルート Mixin。"""

from __future__ import annotations

import time
import urllib.parse
from typing import TYPE_CHECKING, Any

from pokecon.domain import blockly_headless, blockly_validate, inputs as input_specs
from pokecon.runtime import blockly_author, blockly_live, blockly_run


def _ed() -> Any:
    """差し替え対象モジュールを実行時に引き、テストでの上書きを保つ。"""
    from pokecon.ui import blockly_editor

    return blockly_editor


class BlocklyLiveRoutesMixin:
    """`/live/*`・`/run*`・`/record/*` のハンドラを担当する Mixin。"""

    if TYPE_CHECKING:
        path: str

        def _reply(
            self, ok: bool, message: str, extra: dict[str, Any] | None = None
        ) -> None: ...

    def _handle_run_state(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        try:
            since = int(query.get("since", ["0"])[0])
        except ValueError:
            since = 0
        self._reply(True, "", _ed()._run_state(since))

    def _handle_live_state(self) -> None:
        snapshot = _ed()._LIVE.snapshot()
        if snapshot is None:
            self._reply(
                False,
                "エディタの状態がまだありません（開いて少し待ってください）",
            )
            return
        self._reply(
            True,
            "",
            {
                "revision": snapshot["revision"],
                "workspace": snapshot["workspace"],
                "dirty": snapshot["dirty"],
                "client": snapshot["client"],
                "queued": snapshot["queued"],
                "active": snapshot["active"],
            },
        )

    def _handle_live_next(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        client = query.get("client", [""])[0]
        if not client:
            self._reply(False, "client がありません")
            return
        self._reply(True, "", _ed()._LIVE.next_request(client))

    def _live_snapshot(self, payload: dict[str, Any]) -> None:
        """ブラウザの現在のワークスペースと版を受け取る。"""
        client = payload.get("client")
        revision = payload.get("revision")
        workspace = payload.get("workspace")
        if not isinstance(client, str) or not client:
            self._reply(False, "client がありません")
            return
        if not isinstance(revision, str) or not revision:
            self._reply(False, "revision がありません")
            return
        if not isinstance(workspace, dict):
            self._reply(False, "workspace がありません")
            return
        leased = _ed()._LIVE.update(
            client, revision, workspace, bool(payload.get("dirty"))
        )
        self._reply(True, "", {"leased": leased})

    def _live_result(self, payload: dict[str, Any]) -> None:
        """ブラウザの適用結果を受け取る。"""
        client = payload.get("client")
        request_id = payload.get("id")
        if not isinstance(client, str) or not client:
            self._reply(False, "client がありません")
            return
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            self._reply(False, "id がありません")
            return
        revision = payload.get("revision")
        workspace = payload.get("workspace")
        accepted = _ed()._LIVE.complete(
            client,
            request_id,
            ok=payload.get("ok") is True,
            revision=revision if isinstance(revision, str) else "",
            workspace=workspace if isinstance(workspace, dict) else None,
            error=str(payload.get("error") or ""),
            status=str(payload.get("status") or ""),
        )
        self._reply(accepted, "" if accepted else "要求が切り替わっています")

    def _live_patch(self, payload: dict[str, Any]) -> None:
        """CLI の要求を検証し、開いているエディタへ渡して結果を待つ。"""
        ed = _ed()
        base_revision = payload.get("baseRevision")
        operations = payload.get("operations")
        if not isinstance(base_revision, str) or not base_revision:
            self._reply(False, "baseRevision を書いてください", {"status": "usage"})
            return
        snapshot = ed._LIVE.snapshot()
        if snapshot is None:
            self._reply(
                False,
                "エディタの状態がまだありません（開いて少し待ってください）",
                {"status": "unavailable"},
            )
            return
        if snapshot["revision"] != base_revision:
            self._reply(
                False,
                "エディタの内容が変わっています（読み直してください）",
                {
                    "status": "conflict",
                    "revision": snapshot["revision"],
                    "workspace": snapshot["workspace"],
                },
            )
            return
        catalog = blockly_headless.catalog(ed.WindowUtils.APP_DIR)
        if not catalog.ok or catalog.data is None:
            self._reply(
                False,
                (catalog.errors or ["カタログを取得できませんでした"])[0],
                {"status": "failed"},
            )
            return
        patched = blockly_author.patch_workspace(
            snapshot["workspace"], operations, catalog.data
        )
        if not patched.ok or patched.workspace is None:
            self._reply(
                False,
                (patched.errors or ["操作を適用できませんでした"])[0],
                {"status": "failed", "errors": patched.errors},
            )
            return
        structural = any(change.get("op") != "set_field" for change in patched.changes)
        live_ops: list[dict[str, Any]] = []
        if not structural:
            for change in patched.changes:
                block_id = change.get("blockId")
                if not isinstance(block_id, str) or not block_id:
                    self._reply(
                        False,
                        "ブロックIDがありません（エディタで読み直してください）",
                        {"status": "failed"},
                    )
                    return
                live_ops.append(
                    {
                        "blockId": block_id,
                        "field": change["field"],
                        "value": change.get("liveValue"),
                    }
                )
        checked = blockly_author.check_workspace(
            patched.workspace, ed.WindowUtils.APP_DIR
        )
        if not checked.ok or checked.workspace is None:
            self._reply(
                False,
                (checked.errors or ["検査に通りませんでした"])[0],
                {"status": "failed", "errors": checked.errors},
            )
            return
        request_id, error = ed._LIVE.enqueue(
            live_ops,
            base_revision,
            mode="load" if structural else "ops",
            workspace=patched.workspace,
        )
        if request_id is None:
            latest = ed._LIVE.snapshot() or snapshot
            self._reply(
                False,
                error,
                {
                    "status": "unavailable",
                    "revision": latest["revision"],
                    "workspace": latest["workspace"],
                },
            )
            return
        result = ed._LIVE.wait(request_id, timeout=blockly_live.REQUEST_SECONDS + 2.0)
        if result.get("ok") is True:
            self._reply(
                True,
                "AIの変更を適用しました（保存はまだです）",
                {
                    "status": "applied",
                    "revision": result.get("revision", ""),
                    "workspace": result.get("workspace"),
                    "changes": patched.changes,
                    "code": checked.code,
                },
            )
            return
        latest = ed._LIVE.snapshot() or snapshot
        self._reply(
            False,
            result.get("error") or "AIの変更を適用できませんでした",
            {
                "status": result.get("status", "failed"),
                "revision": latest["revision"],
                "workspace": latest["workspace"],
                "changes": patched.changes,
            },
        )

    def _start_trial(self, payload: dict[str, Any]) -> None:
        """保存せずに今の組み立てを本体の実行器で走らせる（試し実行）。

        実行時引数（`POKECON_INPUTS`）があるときは、開始前に
        answers → ポリシー（dialog / default / fail）で確定する。
        dialog は GUI スレッドで表示を予約してすぐ応答し、結果は
        `/run/state` で返す（HTTP 応答を待たせない）。
        """
        ed = _ed()
        host = ed._RUN_HOST
        if host is None:
            self._reply(
                False,
                "本体と繋がっていないため試し実行できません（保存して本体で実行してください）",
            )
            return
        if ed._RECORDER.recording:
            # 記録中に走らせると、試し実行の操作まで記録に混ざる。
            self._reply(
                False,
                "操作の記録中は試し実行できません（記録を止めてから試してください）",
            )
            return
        current = ed._TRIAL
        if current is not None and current.session.awaiting_input:
            self._reply(
                False,
                "実行時引数の入力中です（先に閉じてください）",
            )
            return
        allowed, max_seconds, reason = ed._parse_max_seconds(payload)
        if not allowed:
            self._reply(False, reason)
            return
        policy = payload.get("inputPolicy", "dialog")
        if not isinstance(policy, str) or policy not in input_specs.POLICIES:
            self._reply(
                False,
                "inputPolicy は dialog / default / fail のどれかにしてください",
            )
            return
        answers = payload.get("answers", {})
        if answers is None:
            answers = {}
        if not isinstance(answers, dict):
            self._reply(False, "answers は key → 値のオブジェクトで書いてください")
            return
        app_dir = ed.WindowUtils.APP_DIR
        code = str(payload.get("code", ""))
        raw_spec, spec_errors = input_specs.raw_spec_from_code(code)
        if spec_errors:
            self._reply(False, "実行できません:\n- " + "\n- ".join(spec_errors))
            return
        specs, spec_errors = input_specs.parse_spec(raw_spec, app_dir)
        if spec_errors:
            self._reply(False, "実行時引数が不正です:\n- " + "\n- ".join(spec_errors))
            return
        input_errors = blockly_validate.validate_input_code(code, app_dir)
        if input_errors:
            self._reply(False, "実行時引数が不正です:\n- " + "\n- ".join(input_errors))
            return
        resolution = input_specs.resolve(specs, answers, policy, app_dir)
        if resolution.errors:
            self._reply(
                False, "実行時引数が不正です:\n- " + "\n- ".join(resolution.errors)
            )
            return
        if resolution.missing and policy in ("fail", "default"):
            self._reply(
                False,
                "実行時引数が足りません",
                {
                    "status": "input_required",
                    "inputRequired": resolution.missing,
                },
            )
            return
        if "breakpoints" in payload:
            ed._BREAKPOINTS = ed._as_ids(payload.get("breakpoints"))
        session = blockly_run.TrialSession(next(ed._RUN_IDS))
        session.breakpoints = ed._BREAKPOINTS
        run_to = payload.get("runTo", "")
        if isinstance(run_to, str) and run_to:
            # 行き先が来なくても止まらないだけ（走らせる前に断らない）。
            session.run_to = run_to
        cls, errors = blockly_run.build_trial_class(code, session)
        if cls is None:
            self._reply(False, "実行できません:\n- " + "\n- ".join(errors))
            return
        trial = ed._Trial(session, cls)

        def _finish_start(
            values: dict[str, Any], sources: dict[str, str]
        ) -> str | None:
            """期限を引数確定後に取り、実行器へ渡す。"""
            session.inputs = dict(values)
            session.input_sources = dict(sources)
            session.input_status = "accepted" if specs else "none"
            session.awaiting_input = False
            if max_seconds is not None:
                session.deadline = time.monotonic() + max_seconds
            try:
                message = host.start_external_command(cls, values)
            except Exception:
                # 開始の後の手順で落ちても、走っていれば追跡する。
                if ed._our_command(trial) is not None:
                    ed._TRIAL = trial
                raise
            if not message:
                ed._TRIAL = trial
            return message

        def _begin() -> str | None:
            """混雑の確認・開始・公開をまとめて GUI スレッドで行う。"""
            if host.runner.is_busy():
                return "本体で別のコマンドが実行中です（止めてから試してください）"
            if ed.input_dialog.is_open():
                return "実行時引数の入力中です（先に閉じてください）"
            return _finish_start(resolution.values, resolution.sources)

        if not resolution.missing:
            try:
                message = ed._call_on_gui(_begin)
            except Exception as e:
                if ed._TRIAL is trial:
                    self._reply(
                        True,
                        f"試し実行を始めました（本体で注意: {e}）",
                        {"runId": session.run_id},
                    )
                    return
                self._reply(False, f"本体が応答しません: {e}")
                return
            if message:
                self._reply(False, str(message))
                return
            self._reply(True, "試し実行を始めました", {"runId": session.run_id})
            return

        # dialog ポリシー: 表示だけ予約し、HTTP はすぐ返す。
        def _show_input() -> None:
            root = getattr(host, "root", None)
            try:
                result = ed.input_dialog.prompt(
                    root, raw_spec, ed.WindowUtils.APP_DIR, initial=resolution.values
                )
            except Exception as e:
                session.awaiting_input = False
                session.input_status = "failed"
                session.fail(f"実行時引数の入力を開けません: {e}")
                session.end("エラー")
                return
            if result.values is None:
                session.awaiting_input = False
                if result.error:
                    session.input_status = "failed"
                    session.fail(result.error)
                    session.end("エラー")
                else:
                    session.input_status = "canceled"
                    session.log("実行時引数の入力を取り消しました")
                    session.end("停止")
                return
            resolved = input_specs.resolve(
                specs,
                answers,
                "dialog",
                ed.WindowUtils.APP_DIR,
                dialog_values=result.values,
            )
            if resolved.errors or resolved.missing:
                session.awaiting_input = False
                session.input_status = "failed"
                session.fail(
                    "実行時引数が不正です: "
                    + "; ".join(
                        resolved.errors + [item["key"] for item in resolved.missing]
                    )
                )
                session.end("エラー")
                return
            try:
                message = _finish_start(resolved.values, resolved.sources)
            except Exception as e:
                session.fail(f"試し実行を開始できません: {e}")
                session.end("エラー")
                return
            if message:
                session.fail(message)
                session.end("エラー")

        def _reserve() -> str | None:
            if host.runner.is_busy():
                return "本体で別のコマンドが実行中です（止めてから試してください）"
            if ed.input_dialog.is_open():
                return "実行時引数の入力中です（先に閉じてください）"
            root = getattr(host, "root", None)
            if root is None:
                return "本体の画面が無いため引数ダイアログを開けません"
            ed._TRIAL = trial
            session.awaiting_input = True
            session.input_status = "awaiting"
            session.inputs = dict(resolution.values)
            session.input_sources = dict(resolution.sources)
            root.after(0, _show_input)
            return None

        try:
            message = ed._call_on_gui(_reserve)
        except Exception as e:
            self._reply(False, f"本体が応答しません: {e}")
            return
        if message:
            self._reply(False, str(message))
            return
        self._reply(
            True,
            "実行時引数の入力を待っています",
            {"runId": session.run_id, "status": "awaiting_input"},
        )

    def _control_trial(self, payload: dict[str, Any]) -> None:
        """試し実行の停止・一時停止・再開・1 つ進む・区切りの差し替え。"""
        ed = _ed()
        action = str(payload.get("action", ""))
        if action not in ed._CONTROL_ACTIONS:
            self._reply(False, f"知らない操作です: {action}")
            return
        run_id = payload.get("runId")
        if not ed._is_run_id(run_id):
            self._reply(False, ed.RUN_SWITCHED_MESSAGE)
            return

        def _apply() -> tuple[bool, str]:
            trial = ed._TRIAL
            if trial is None or trial.session.run_id != run_id:
                return (False, ed.RUN_SWITCHED_MESSAGE)
            session = trial.session
            if action == "breakpoints":
                ed._BREAKPOINTS = ed._as_ids(payload.get("breakpoints"))
                session.breakpoints = ed._BREAKPOINTS
                return (True, "")
            if session.awaiting_input:
                if action == "stop":
                    # 表示中の引数ダイアログを閉じる。閉じた結果は
                    # /run/state の inputStatus:"canceled" で見える。
                    ed.input_dialog.cancel()
                    return (True, "")
                return (False, "実行時引数の入力を待っています")
            host = ed._RUN_HOST
            cmd = ed._our_command(trial)
            if host is None or cmd is None:
                return (False, "試し実行は動いていません")
            try:
                if action == "stop":
                    session.stop_requested = True
                    host.stopPlay()
                elif action == "pause":
                    host.set_running_paused(True)
                elif action == "resume":
                    host.set_running_paused(False)
                else:  # step
                    # 次のブロックの手前で止まるよう頼んでから動かす。
                    session.step_pending = True
                    host.set_running_paused(False)
            except Exception as e:
                return (False, f"本体が応答しません: {e}")
            return (True, "")

        try:
            ok, message = ed._call_on_gui(_apply)
        except Exception as e:
            self._reply(False, f"本体が応答しません: {e}")
            return
        self._reply(ok, message)

    def _start_record(self) -> None:
        """本体の送信線に記録を付ける（マクロ記録の開始）。"""
        ed = _ed()
        host = ed._RUN_HOST
        if host is None:
            self._reply(
                False,
                "本体と繋がっていないため記録できません（本体で操作してから試してください）",
            )
            return

        def _attach() -> tuple[bool, str, bool]:
            if ed._RECORDER.recording:
                return (False, "既に記録中です（止めてから始めてください）", False)
            if ed._our_command(ed._TRIAL) is not None:
                # 試し実行の操作まで記録に混ざるため断る。
                return (
                    False,
                    "試し実行中は記録できません（試し実行を止めてから始めてください）",
                    False,
                )
            blocked = ed._RECORD_BLOCKED
            if blocked is not None:
                try:
                    reason = blocked()
                except Exception:
                    reason = None
                if reason:
                    # 通常コマンドの操作まで記録に混ざるため断る。
                    return (False, str(reason), False)
            serial = getattr(host, "serial", None)
            sender = getattr(serial, "sender", None)
            transport = getattr(sender, "transport", None)
            if transport is None:
                return (
                    False,
                    "送信線が無いため記録できません（シリアルに接続してから試してください）",
                    False,
                )
            if not ed._RECORDER.start(transport):
                return (
                    False,
                    "この通信方式では記録できません（送信行を作らないため）",
                    False,
                )
            imu_on = False
            try:
                running = getattr(transport, "imu_loop_running", None)
                imu_on = bool(running()) if callable(running) else False
            except Exception:
                imu_on = False
            return (True, "記録を始めました（本体で操作してください）", imu_on)

        try:
            ok, message, imu_on = ed._call_on_gui(_attach)
        except Exception as e:
            self._reply(False, f"本体が応答しません: {e}")
            return
        self._reply(ok, message, {"imu": imu_on} if ok else None)

    def _stop_record(self) -> None:
        """記録を外し、手順を返す（マクロ記録の停止）。"""
        ed = _ed()
        host = ed._RUN_HOST
        if host is None:
            self._reply(
                False,
                "本体と繋がっていないため記録できません（本体で操作してから試してください）",
            )
            return

        def _detach() -> tuple[
            bool,
            list[dict[str, Any]],
            list[Any],
            bool,
            tuple[float, list[tuple[float, tuple[int, int, int, int, int, int]]]]
            | None,
            float | None,
            float | None,
        ]:
            was = ed._RECORDER.recording
            warnings: list[Any] = []
            steps = ed._RECORDER.stop(warnings)
            contaminated = bool(ed._RECORDER.contaminated)
            gyro = ed._RECORDER.take_gyro()
            origin = ed._RECORDER.timeline_origin()
            end = ed._RECORDER.timeline_end()
            return (
                was,
                steps if isinstance(steps, list) else [],
                warnings,
                contaminated,
                gyro,
                origin,
                end,
            )

        try:
            was_recording, steps, warnings, contaminated, gyro, origin, end = (
                ed._call_on_gui(_detach)
            )
        except Exception as e:
            self._reply(False, f"本体が応答しません: {e}")
            return
        if not was_recording:
            self._reply(False, "記録していません（記録ボタンで始めてください）")
            return
        gyro_payload: dict[str, Any] | None = None
        gyro_note = ""
        if gyro is not None:
            try:
                gyro_payload = ed._save_blockly_gyro(gyro, origin, end)
            except Exception as e:
                gyro_note = f"ジャイロの保存に失敗しました: {e}"
        if gyro_payload is None and not gyro_note:
            if gyro is not None:
                gyro_note = "ジャイロは操作の前だけだったため記録していません"
            else:
                gyro_note = (
                    "ジャイロは記録されていません"
                    "（ジャイロ送出を開始してから記録してください）"
                )
        message = f"{len(steps)} 個の操作を記録しました"
        if gyro_payload is not None:
            seconds = gyro_payload["duration_ms"] / 1000.0
            message += f"、ジャイロ {seconds:.1f} 秒"
        if contaminated:
            message += "（本体側の操作が混ざった可能性があります）"
        extra: dict[str, Any] = {"steps": steps}
        if gyro_payload is not None:
            extra["gyro"] = gyro_payload
        if gyro_note:
            extra["gyroNote"] = gyro_note
        if warnings:
            extra["warnings"] = warnings
        if contaminated:
            extra["contaminated"] = True
        self._reply(True, message, extra)
