"""单个 WebSocket 连接处理。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from time import perf_counter
from typing import Any, Coroutine

import numpy as np
import websockets

from src.core.turn_processor import TurnContext, TurnProcessor
from websockets.exceptions import ConnectionClosed

from src.core.audio_worker import AudioStreamWorker
from src.core.turn_trace import log_turn_trace
from src.domain.session import ConversationRecorder, SessionInfo
from src.providers import InferenceModules, TtsPlaybackSession, TtsStreamMetrics
from src.protocol.edge_cloud import (
    ClientMessageType,
    MessageEnvelope,
    ServerMessageType,
)
from src.services.orchestration import DeviceBinding, OrchestrationService
from src.services.vla_debug import VLADebugService
from src.utils.logging import logger

EDGE_TOOL_DEVICE_STATUS = "device.status"
EDGE_TOOL_GESTURE_OK = "gesture.ok"
EDGE_TOOL_GESTURE_EXTEND_HAND = "gesture.extend_hand"
EDGE_TOOL_GESTURE_WAVE = "gesture.wave"
EDGE_TOOL_GESTURE_GUIDE = "gesture.guide"
EDGE_TOOL_DEFAULT_TIMEOUT_MS = 60_000
EDGE_TOOL_ACK_TIMEOUT_MS = 3_000
INTERRUPT_CANCEL_TIMEOUT_SEC = 1.0
EDGE_GESTURE_TOOL_NAMES = (
    EDGE_TOOL_GESTURE_OK,
    EDGE_TOOL_GESTURE_EXTEND_HAND,
    EDGE_TOOL_GESTURE_WAVE,
    EDGE_TOOL_GESTURE_GUIDE,
)


class EdgeToolInvocationError(RuntimeError):
    """边端工具执行失败。"""

    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ConnectionHandler:
    """单个 WebSocket 客户端连接的状态与消息处理。"""

    def __init__(
        self,
        config: dict[str, Any],
        modules: InferenceModules,
        *,
        conversation_recorder: ConversationRecorder | None = None,
        orchestration_service: OrchestrationService | None = None,
        vla_debug_service: VLADebugService | None = None,
    ) -> None:
        self.websocket: websockets.ServerConnection | None = None
        self.config = config
        self.modules = modules
        self.conversation_recorder = conversation_recorder
        self.orchestration_service = orchestration_service
        self.vla_debug_service = vla_debug_service
        self.headers: dict[str, str] = {}
        self.client_ip: str | None = None
        self.device_id: str | None = None
        self.request_path = ""
        self._device_binding: DeviceBinding | None = None
        self._registered_machine_id: str | None = None
        self._session_info: SessionInfo | None = None
        self.stream_config: dict[str, Any] = {}
        if self.modules.vad_model_path is None:
            raise ValueError("ConnectionHandler 初始化失败: 缺少 vad_model_path")
        self.audio_worker = AudioStreamWorker(self.modules.vad_model_path)
        self.tts_session: TtsPlaybackSession | None = None
        self.turn_processor = TurnProcessor(
            self.modules,
            conversation_recorder=self.conversation_recorder,
            edge_tool_handler=self._maybe_handle_edge_tool,
            llm_action_handler=self._handle_llm_actions,
            llm_payload_builder=self._build_llm_payload,
        )
        self.audio_stream_id = 0
        self.accepting_audio = False
        self.processing_task: asyncio.Task[None] | None = None
        self.tts_task: asyncio.Task[TtsStreamMetrics] | None = None
        self._input_started_at: float | None = None
        self._control_tasks: set[asyncio.Task[None]] = set()
        self._pending_tool_invocations: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending_tool_acks: dict[str, asyncio.Future[None]] = {}

    @property
    def session_info(self) -> SessionInfo | None:
        return self._session_info

    async def handle_connection(self, ws: websockets.ServerConnection) -> None:
        try:
            self.websocket = ws
            self.tts_session = TtsPlaybackSession(
                ws,
                self.modules.tts,
                inference_lock=self.modules.inference_lock,
            )
            request = ws.request
            if request is None:
                self.headers = {}
                self.request_path = ""
            else:
                self.headers = dict(request.headers)
                self.request_path = request.path or ""
            self.client_ip = _resolve_client_ip(ws, self.headers)
            self.device_id = self.headers.get("device-id", None)
            logger.debug(
                "连接上下文已建立 | client_ip={} device-id={} path={} headers={}",
                self.client_ip,
                self.device_id,
                self.request_path,
                self.headers,
            )

            self.audio_worker.start()
            async for message in ws:
                await self._route_message(message)
        except ConnectionClosed as exc:
            logger.info("客户端连接关闭: {}", exc)
        except Exception:
            logger.exception("会话处理异常")
            raise
        finally:
            await self.close()

    async def close(self) -> None:
        await self._shutdown_connection()

    async def _route_message(self, message: str | bytes) -> None:
        if isinstance(message, str):
            await self._handle_text_message(message)
            return

        await self._handle_binary_message(message)

    async def _handle_text_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("收到无法解析的文本消息: {}", message)
            return

        proto = payload.pop("_proto", None)
        if proto:
            logger.debug(
                "客户端协议元信息 | version={} seq={} ts_ms={} turn_id={}",
                proto.get("version"),
                proto.get("seq"),
                proto.get("ts_ms"),
                proto.get("turn_id"),
            )

        message_type = str(payload.get("type") or "unknown")
        logger.debug(
            "收到控制消息 | type={} payload={}",
            message_type,
            json.dumps(payload, ensure_ascii=False),
        )
        if message_type == ClientMessageType.INTERRUPT.value:
            await self.handle_interrupt()
        elif message_type == ClientMessageType.AUDIO_START.value:
            await self.handle_audio_start(payload)
        elif message_type == ClientMessageType.AUDIO_STOP.value:
            if str(payload.get("reason") or "") == "no_input_timeout":
                await self.handle_no_input_timeout()
            else:
                await self.finish_current_turn(flush_vad=True)
        elif message_type == ClientMessageType.DEVICE_HELLO.value:
            await self.handle_device_hello(payload)
        elif message_type == ClientMessageType.BINDING_ACK.value:
            await self.handle_binding_ack(payload)
        elif message_type == ClientMessageType.TOOL_ACK.value:
            await self.handle_tool_ack(payload)
        elif message_type == ClientMessageType.TOOL_PROGRESS.value:
            await self.handle_tool_progress(payload)
        elif message_type == ClientMessageType.TOOL_RESULT.value:
            await self.handle_tool_result(payload)
        elif message_type == ClientMessageType.TOOL_ERROR.value:
            await self.handle_tool_error(payload)
        elif message_type == ClientMessageType.VLA_DEBUG_CAMERA_FRAME.value:
            await self.handle_vla_debug_camera_frame(payload)
        elif message_type == ClientMessageType.LIVE_CAMERA_STATUS.value:
            await self.handle_live_camera_status(payload)

    async def _handle_binary_message(self, message: bytes) -> None:
        if not self.accepting_audio:
            return
        self.audio_worker.enqueue_packet(self.audio_stream_id, message)

    async def handle_audio_start(self, payload: dict[str, Any]) -> None:
        await self._prepare_for_new_turn()
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        requested_session_id = str(metadata.get("session_id") or "").strip()
        binding = self._device_binding
        machine_id = str(
            metadata.get("machine_id")
            or metadata.get("device_id")
            or (binding.machine_id if binding is not None else "")
            or self.device_id
            or ""
        ).strip()
        client_binding_version = _coerce_binding_version(
            metadata.get("binding_version")
        )
        binding = await self._refresh_device_binding_if_needed(
            machine_id=machine_id,
            client_binding_version=client_binding_version,
        )
        orchestration_id = (
            str(
                metadata.get("orchestration_id")
                or (binding.orchestration_id if binding is not None else "")
                or ""
            ).strip()
            or None
        )
        server_binding_version = binding.binding_version if binding is not None else 0
        session_info, generated = self._activate_session(
            requested_session_id=requested_session_id,
            machine_id=machine_id,
            orchestration_id=orchestration_id,
        )
        logger.debug(
            (
                "收到音频上行开始 | session_id={} machine_id={} orchestration_id={} "
                "client_binding_version={} server_binding_version={}"
            ),
            session_info.session_id,
            session_info.machine_id,
            session_info.orchestration_id,
            client_binding_version,
            server_binding_version,
        )
        log_turn_trace(
            stage="AUDIO",
            event="start",
            session_id=session_info.session_id,
            stream=payload.get("stream") or {},
            machine_id=session_info.machine_id,
            orchestration_id=session_info.orchestration_id,
            client_binding_version=client_binding_version,
            server_binding_version=server_binding_version,
        )

        self._start_input_stream(payload.get("stream") or {})

        if generated:
            await self._send_json(
                {
                    "type": ServerMessageType.SESSION_CREATED.value,
                    "session_id": session_info.session_id,
                    "machine_id": session_info.machine_id,
                }
            )

    async def _refresh_device_binding_if_needed(
        self,
        *,
        machine_id: str,
        client_binding_version: int,
    ) -> DeviceBinding | None:
        binding = self._device_binding
        if not machine_id or self.orchestration_service is None:
            return binding
        if (
            binding is not None
            and binding.machine_id == machine_id
            and binding.binding_version >= client_binding_version
        ):
            return binding

        refreshed = await self.orchestration_service.resolve_device_binding(
            machine_id=machine_id,
            binding_version=client_binding_version,
            capabilities=None,
        )
        self._device_binding = refreshed
        if refreshed is not None:
            logger.info(
                "音频开始时刷新设备绑定 | machine_id={} orchestration_id={} binding_version={}",
                refreshed.machine_id,
                refreshed.orchestration_id,
                refreshed.binding_version,
            )
        return refreshed

    async def handle_interrupt(self) -> None:
        logger.debug("收到 interrupt 控制消息")
        session_id = self.session_info.session_id if self.session_info else None
        await self._send_json(
            {
                "type": ServerMessageType.INTERRUPT_ACK.value,
                "session_id": session_id,
            },
            suppress_connection_closed=True,
        )
        self._schedule_control_task(
            self._abort_current_turn_after_interrupt(session_id=session_id)
        )

    async def _abort_current_turn_after_interrupt(
        self, *, session_id: str | None
    ) -> None:
        try:
            await asyncio.wait_for(
                self._abort_current_turn(notify_tts_interrupted=True),
                timeout=INTERRUPT_CANCEL_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "处理中断时取消当前轮次超时，继续保持连接 | session_id={}",
                session_id,
            )
            self._reset_current_input_stream()
        except Exception as exc:
            logger.warning(
                "处理中断时取消当前轮次失败，继续保持连接 | session_id={} error={}",
                session_id,
                exc,
            )
            self._reset_current_input_stream()

    def _schedule_control_task(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine, name="nexus-control-task")
        self._control_tasks.add(task)
        task.add_done_callback(self._control_tasks.discard)

    async def handle_no_input_timeout(self) -> None:
        logger.debug("收到 no_input_timeout，清理当前音频流")
        self._reset_current_input_stream()

    async def handle_device_hello(self, payload: dict[str, Any]) -> None:
        machine_id = str(
            payload.get("machine_id")
            or payload.get("device_id")
            or self.device_id
            or ""
        ).strip()
        client_binding_version = _coerce_binding_version(payload.get("binding_version"))
        capabilities = payload.get("capabilities")
        capability_count = 0
        if isinstance(capabilities, dict):
            capability_count = len(capabilities)
        elif isinstance(capabilities, list):
            capability_count = len(capabilities)
        logger.info(
            ("收到设备握手 | machine_id={} binding_version={} capabilities={}"),
            machine_id or "<unknown>",
            client_binding_version,
            capability_count,
        )
        await self._register_machine_connection(machine_id)

        if self.orchestration_service is None:
            self._device_binding = None
            await self._send_json(
                {
                    "type": ServerMessageType.DEVICE_UNBOUND.value,
                    "machine_id": machine_id,
                    "binding_version": client_binding_version,
                }
            )
            return

        binding = await self.orchestration_service.resolve_device_binding(
            machine_id=machine_id,
            binding_version=client_binding_version,
            capabilities=capabilities,
        )
        self._device_binding = binding
        if binding is None:
            await self._send_json(
                {
                    "type": ServerMessageType.DEVICE_UNBOUND.value,
                    "machine_id": machine_id,
                    "binding_version": client_binding_version,
                }
            )
            return

        await self._handle_live_camera_status_from_hello(binding.machine_id, payload)

        await self._send_json(
            {
                "type": ServerMessageType.DEVICE_BOUND.value,
                "machine_id": binding.machine_id,
                "orchestration_id": binding.orchestration_id,
                "binding_version": binding.binding_version,
                "robot_id": binding.robot_id,
                "environment_id": binding.environment_id,
                "voice_id": binding.voice_id,
                "allowed_tools": list(binding.allowed_tools),
                "welcome_message": binding.welcome_message,
            }
        )

    async def handle_binding_ack(self, payload: dict[str, Any]) -> None:
        logger.info(
            "收到边端绑定确认 | machine_id={} orchestration_id={} binding_version={} status={}",
            str(payload.get("machine_id") or self.device_id or "").strip()
            or "<unknown>",
            str(payload.get("orchestration_id") or "").strip() or None,
            _coerce_binding_version(payload.get("binding_version")),
            str(payload.get("status") or "").strip() or None,
        )

    async def handle_tool_ack(self, payload: dict[str, Any]) -> None:
        logger.debug(
            "收到边端工具确认 | invocation_id={} tool={} session_id={}",
            str(payload.get("invocation_id") or "").strip() or None,
            str(payload.get("tool_name") or "").strip() or None,
            str(payload.get("session_id") or "").strip() or None,
        )
        invocation_id = str(payload.get("invocation_id") or "").strip()
        if not invocation_id:
            return
        future = self._pending_tool_acks.get(invocation_id)
        if future is not None and not future.done():
            future.set_result(None)

    async def handle_tool_progress(self, payload: dict[str, Any]) -> None:
        logger.debug(
            "收到边端工具进度 | invocation_id={} tool={} progress={}",
            str(payload.get("invocation_id") or "").strip() or None,
            str(payload.get("tool_name") or "").strip() or None,
            payload.get("progress"),
        )

    async def handle_tool_result(self, payload: dict[str, Any]) -> None:
        logger.debug(
            "收到边端工具结果 | invocation_id={} tool={}",
            str(payload.get("invocation_id") or "").strip() or None,
            str(payload.get("tool_name") or "").strip() or None,
        )
        invocation_id = str(payload.get("invocation_id") or "").strip()
        if not invocation_id:
            return
        future = self._pending_tool_invocations.get(invocation_id)
        if future is None or future.done():
            return
        result = payload.get("result")
        future.set_result(result if isinstance(result, dict) else {})
        await self._notify_vla_debug_tool_finished(
            invocation_id=invocation_id,
            tool_name=str(payload.get("tool_name") or "").strip(),
            ok=True,
        )

    async def handle_tool_error(self, payload: dict[str, Any]) -> None:
        error = payload.get("error")
        code = None
        message = None
        if isinstance(error, dict):
            code = str(error.get("code") or "").strip() or None
            message = str(error.get("message") or "").strip() or None
        logger.warning(
            "收到边端工具错误 | invocation_id={} tool={} code={} message={}",
            str(payload.get("invocation_id") or "").strip() or None,
            str(payload.get("tool_name") or "").strip() or None,
            code,
            message,
        )
        invocation_id = str(payload.get("invocation_id") or "").strip()
        if not invocation_id:
            return
        future = self._pending_tool_invocations.get(invocation_id)
        pending_acks = getattr(self, "_pending_tool_acks", {})
        ack_future = pending_acks.get(invocation_id)
        error = EdgeToolInvocationError(
            code=code or "tool_error",
            message=message or "edge tool invocation failed",
        )
        if future is not None and not future.done():
            future.set_exception(error)
        if ack_future is not None and not ack_future.done():
            ack_future.set_exception(error)
        await self._notify_vla_debug_tool_finished(
            invocation_id=invocation_id,
            tool_name=str(payload.get("tool_name") or "").strip(),
            ok=False,
        )

    async def _notify_vla_debug_tool_finished(
        self,
        *,
        invocation_id: str,
        tool_name: str,
        ok: bool,
    ) -> None:
        if tool_name != "vla.control" or self.vla_debug_service is None:
            return
        await self.vla_debug_service.handle_edge_tool_finished(
            invocation_id=invocation_id,
            ok=ok,
        )

    async def handle_vla_debug_camera_frame(self, payload: dict[str, Any]) -> None:
        machine_id = str(
            payload.get("machine_id")
            or (self._device_binding.machine_id if self._device_binding else "")
        ).strip()
        if not machine_id:
            logger.warning(
                "VLA 调试相机帧缺少 machine_id，已丢弃 | subscription_id={}",
                payload.get("subscription_id"),
            )
            return
        if self.vla_debug_service is None:
            return
        await self.vla_debug_service.handle_debug_camera_frame(
            machine_id=machine_id,
            frame=payload,
        )

    async def handle_live_camera_status(self, payload: dict[str, Any]) -> None:
        machine_id = str(
            payload.get("machine_id")
            or (self._device_binding.machine_id if self._device_binding else "")
            or self.device_id
            or ""
        ).strip()
        if not machine_id:
            logger.warning("Live Camera Preview 状态缺少 machine_id，已丢弃")
            return
        if self.vla_debug_service is None:
            return
        status = payload.get("status")
        if not isinstance(status, dict):
            status = payload
        await self.vla_debug_service.handle_live_camera_status(
            machine_id=machine_id,
            status=status,
        )

    async def _handle_live_camera_status_from_hello(
        self,
        machine_id: str,
        payload: dict[str, Any],
    ) -> None:
        if self.vla_debug_service is None:
            return
        runtime_status = payload.get("status")
        if not isinstance(runtime_status, dict):
            return
        status = runtime_status.get("live_camera")
        if not isinstance(status, dict):
            return
        await self.vla_debug_service.handle_live_camera_status(
            machine_id=machine_id,
            status=status,
        )

    async def _build_llm_payload(
        self,
        turn_context: TurnContext,
    ) -> dict[str, object]:
        session_info = turn_context.session_info
        orchestration_id = (
            session_info.orchestration_id if session_info is not None else None
        )
        if not orchestration_id or self.orchestration_service is None:
            return {"kb_id": []}

        robot_id = (
            self._device_binding.robot_id
            if self._device_binding is not None
            else None
        )
        try:
            return await self.orchestration_service.build_llm_extra_payload(
                orchestration_id,
                robot_id=robot_id,
            )
        except Exception as exc:
            logger.warning(
                "构建 LLM 请求 payload 失败，回退到空知识库列表 | orchestration_id={} err={}",
                orchestration_id,
                exc,
            )
            return {"kb_id": []}

    async def finish_current_turn(self, *, flush_vad: bool) -> None:
        """结束当前一轮上行音频，并提交到后续处理。"""
        if self.processing_task is not None or self.tts_task is not None:
            logger.info(
                "当前轮次仍在处理中，忽略本次 finish | processing_active={} tts_active={}",
                self.processing_task is not None,
                self.tts_task is not None,
            )
            return

        utterance = await self._finish_current_input_stream(flush_vad=flush_vad)
        self._submit_turn_for_processing(utterance)

    async def _finish_current_input_stream(self, *, flush_vad: bool) -> np.ndarray:
        """结束当前输入流并取回完整音频。"""
        logger.debug(
            "结束当前输入流 | stream_id={} flush_vad={}",
            self.audio_stream_id,
            flush_vad,
        )
        self.accepting_audio = False
        utterance = await asyncio.wrap_future(
            self.audio_worker.finish_stream(
                self.audio_stream_id,
                flush_vad=flush_vad,
            )
        )
        logger.debug(
            "当前输入流已完成 | stream_id={} samples={}",
            self.audio_stream_id,
            utterance.size,
        )
        session_id = self.session_info.session_id if self.session_info else None
        log_turn_trace(
            stage="AUDIO",
            event="stop",
            session_id=session_id,
            stream_id=self.audio_stream_id,
            flush_vad=flush_vad,
            samples=utterance.size,
            audio_ms=(utterance.size / 16000) * 1000 if utterance.size else 0.0,
        )
        return utterance

    def _submit_turn_for_processing(self, utterance: np.ndarray) -> None:
        """把完整语音提交给单轮后处理链。"""
        if self.websocket is None:
            raise RuntimeError("ConnectionHandler 尚未绑定 websocket")
        vad_elapsed_ms = None
        if self._input_started_at is not None:
            vad_elapsed_ms = (perf_counter() - self._input_started_at) * 1000
        turn_context = TurnContext(
            session_info=self.session_info,
            send_json=self._send_json,
            vad_elapsed_ms=vad_elapsed_ms,
            audio_duration_ms=(utterance.size / 16000) * 1000
            if utterance.size
            else 0.0,
        )
        self._input_started_at = None
        self.processing_task = asyncio.create_task(
            self.turn_processor.process_turn(
                utterance,
                turn_context=turn_context,
                start_tts_reply=self._start_tts_reply,
                start_tts_reply_stream=self._start_tts_reply_stream,
            ),
            name="nexus-session-process-utterance",
        )
        self.processing_task.add_done_callback(self._clear_turn_tasks)

    def _start_tts_reply(self, reply_text: str) -> asyncio.Task[TtsStreamMetrics]:
        if self.tts_session is None:
            raise RuntimeError("ConnectionHandler 尚未初始化 TTS 会话执行器")
        self.tts_task = asyncio.create_task(
            self.tts_session.stream_text(reply_text),
            name="nexus-session-send-tts",
        )
        return self.tts_task

    def _start_tts_reply_stream(
        self,
        chunk_queue: asyncio.Queue[str | None],
    ) -> asyncio.Task[TtsStreamMetrics]:
        if self.tts_session is None:
            raise RuntimeError("ConnectionHandler 尚未初始化 TTS 会话执行器")
        self.tts_task = asyncio.create_task(
            self.tts_session.stream_text_queue(chunk_queue),
            name="nexus-session-send-tts-stream",
        )
        return self.tts_task

    def _clear_turn_tasks(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            logger.info("当前轮次处理已取消")
        else:
            exc = task.exception()
            if exc is not None:
                logger.opt(exception=exc).error("当前轮次处理失败")
        if self.processing_task is task:
            self.processing_task = None
        if self.tts_task is not None and self.tts_task.done():
            self.tts_task = None

    async def _prepare_for_new_turn(self) -> None:
        """为新的上行轮次做准备。

        该路径用于首次说话、完整播报后的续聊等正常开始场景：
        - 停掉残留的 processing / TTS
        - 切到新的 input stream 上下文
        """
        await self._stop_turn_processing(notify_tts_interrupted=False)
        self._begin_next_input_stream()

    async def _abort_current_turn(self, *, notify_tts_interrupted: bool) -> None:
        """终止当前轮次。

        该路径用于 interrupt 或连接关闭等显式打断场景：
        - 停掉 processing / TTS
        - 丢弃当前 input stream
        """
        await self._stop_turn_processing(notify_tts_interrupted=notify_tts_interrupted)
        self._reset_current_input_stream()

    async def _shutdown_connection(self) -> None:
        """关闭整个连接并释放本地资源。"""
        await self._cancel_control_tasks()
        self._fail_pending_tool_invocations("connection_closed")
        await self._abort_current_turn(notify_tts_interrupted=False)
        await asyncio.to_thread(self.audio_worker.close)
        await self._unregister_machine_connection()
        self._session_info = None
        if self.tts_session is not None:
            await self.tts_session.close()
        self.tts_session = None
        self.websocket = None

    async def _cancel_control_tasks(self) -> None:
        tasks = set(self._control_tasks)
        self._control_tasks.clear()
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _stop_turn_processing(self, *, notify_tts_interrupted: bool) -> None:
        await self._cancel_processing_task()
        await self._cancel_tts_task(
            notify_client=notify_tts_interrupted,
        )

    def _start_input_stream(self, stream_config: dict[str, Any]) -> None:
        self.accepting_audio = True
        self.stream_config = dict(stream_config)
        self._input_started_at = perf_counter()
        self.audio_worker.start_stream(self.audio_stream_id, self.stream_config)

    def _begin_next_input_stream(self) -> None:
        self.audio_stream_id += 1
        self.stream_config = {}
        self.accepting_audio = False

    def _reset_current_input_stream(self) -> None:
        current_stream_id = self.audio_stream_id
        self._begin_next_input_stream()
        self._input_started_at = None
        self.audio_worker.reset_stream(current_stream_id)

    async def _cancel_processing_task(self) -> None:
        task = self.processing_task
        self.processing_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _cancel_tts_task(self, *, notify_client: bool) -> None:
        task = self.tts_task
        self.tts_task = None
        if self.tts_session is not None:
            try:
                await asyncio.wait_for(
                    self.tts_session.cancel_active_stream(),
                    timeout=INTERRUPT_CANCEL_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                logger.warning("取消 TTS 流超时")
            except Exception as exc:
                logger.warning("取消 TTS 流失败: {}", exc)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if notify_client:
            if self.tts_session is None:
                return
            try:
                await self.tts_session.notify_interrupted(
                    self.session_info.session_id if self.session_info else None
                )
            except ConnectionClosed:
                raise
            except Exception as exc:
                logger.warning("通知客户端 TTS 已中断失败: {}", exc)

    def _activate_session(
        self,
        *,
        requested_session_id: str,
        machine_id: str,
        orchestration_id: str | None,
    ) -> tuple[SessionInfo, bool]:
        effective_session_id = requested_session_id or str(uuid.uuid4())
        generated = not requested_session_id
        session_info = SessionInfo(
            session_id=effective_session_id,
            machine_id=machine_id,
            orchestration_id=orchestration_id,
        )
        self._session_info = session_info
        logger.debug(
            "会话已绑定 | session_id={} machine_id={} orchestration_id={} generated={}",
            session_info.session_id,
            session_info.machine_id,
            session_info.orchestration_id,
            generated,
        )
        return session_info, generated

    async def invoke_edge_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None,
        session_id: str | None,
        timeout_ms: int = EDGE_TOOL_DEFAULT_TIMEOUT_MS,
    ) -> dict[str, Any]:
        if self.websocket is None:
            raise RuntimeError("ConnectionHandler 尚未绑定 websocket")

        invocation_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_tool_invocations[invocation_id] = future
        try:
            await self._send_json(
                {
                    "type": ServerMessageType.TOOL_INVOKE.value,
                    "invocation_id": invocation_id,
                    "tool_name": tool_name,
                    "session_id": session_id,
                    "arguments": arguments or {},
                    "timeout_ms": timeout_ms,
                }
            )
            return await asyncio.wait_for(
                future,
                timeout=timeout_ms / 1000 if timeout_ms > 0 else None,
            )
        finally:
            self._pending_tool_invocations.pop(invocation_id, None)

    async def start_edge_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None,
        session_id: str | None,
        timeout_ms: int = 0,
    ) -> str:
        if self.websocket is None:
            raise RuntimeError("ConnectionHandler 尚未绑定 websocket")
        invocation_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self._pending_tool_acks[invocation_id] = future
        try:
            await self._send_json(
                {
                    "type": ServerMessageType.TOOL_INVOKE.value,
                    "invocation_id": invocation_id,
                    "tool_name": tool_name,
                    "session_id": session_id,
                    "arguments": arguments or {},
                    "timeout_ms": timeout_ms,
                }
            )
            await asyncio.wait_for(future, timeout=EDGE_TOOL_ACK_TIMEOUT_MS / 1000)
            return invocation_id
        finally:
            self._pending_tool_acks.pop(invocation_id, None)

    async def cancel_edge_tool(
        self,
        *,
        invocation_id: str,
        tool_name: str,
    ) -> None:
        await self._send_json(
            {
                "type": ServerMessageType.TOOL_CANCEL.value,
                "invocation_id": invocation_id,
                "tool_name": tool_name,
            }
        )

    async def send_vla_debug_camera_subscription(
        self,
        *,
        subscription_id: str,
        fps: float,
        subscribe: bool,
    ) -> None:
        await self._send_json(
            {
                "type": (
                    ServerMessageType.VLA_DEBUG_CAMERA_SUBSCRIBE.value
                    if subscribe
                    else ServerMessageType.VLA_DEBUG_CAMERA_UNSUBSCRIBE.value
                ),
                "subscription_id": subscription_id,
                "fps": fps,
            }
        )

    async def _maybe_handle_edge_tool(
        self,
        transcript: str,
        turn_context: TurnContext,
    ) -> str | None:
        if not _should_invoke_device_status_tool(transcript):
            return None
        binding = self._device_binding
        if binding is None or EDGE_TOOL_DEVICE_STATUS not in binding.allowed_tools:
            return None

        session_id = (
            turn_context.session_info.session_id
            if turn_context.session_info is not None
            else None
        )
        await turn_context.send_json(
            _build_edge_tool_call_event(
                session_id=session_id,
                name="设备状态",
                status="running",
                status_label="进行中",
                summary="正在查询设备状态",
                result="",
                event="tool.invoke",
            )
        )
        try:
            result = await self.invoke_edge_tool(
                tool_name=EDGE_TOOL_DEVICE_STATUS,
                arguments={"include_allowed_tools": True},
                session_id=session_id,
            )
        except asyncio.TimeoutError:
            logger.warning("边端工具调用超时 | tool={}", EDGE_TOOL_DEVICE_STATUS)
            await turn_context.send_json(
                _build_edge_tool_call_event(
                    session_id=session_id,
                    name="设备状态",
                    status="failed",
                    status_label="失败",
                    summary="设备状态查询超时",
                    result="边端未在预期时间内返回结果",
                    event="tool.timeout",
                )
            )
            return "我暂时没有拿到当前设备状态，请稍后再试。"
        except EdgeToolInvocationError as exc:
            logger.warning(
                "边端工具调用失败 | tool={} code={} message={}",
                EDGE_TOOL_DEVICE_STATUS,
                exc.code,
                exc.message,
            )
            await turn_context.send_json(
                _build_edge_tool_call_event(
                    session_id=session_id,
                    name="设备状态",
                    status="failed",
                    status_label="失败",
                    summary="设备状态查询失败",
                    result=exc.message,
                    event="tool.error",
                )
            )
            return "我暂时无法读取当前设备状态，请稍后再试。"

        reply_text = _build_device_status_reply(result)
        await turn_context.send_json(
            _build_edge_tool_call_event(
                session_id=session_id,
                name="设备状态",
                status="completed",
                status_label="已完成",
                summary="设备状态查询已完成",
                result=reply_text,
                event="tool.result",
            )
        )
        return reply_text

    async def _handle_llm_actions(
        self,
        actions: tuple[str, ...],
        turn_context: TurnContext,
    ) -> None:
        gesture_tools = tuple(
            action for action in actions if action in EDGE_GESTURE_TOOL_NAMES
        )
        if not gesture_tools:
            return
        await self._handle_gesture_tools(gesture_tools, turn_context)

    async def _handle_gesture_tools(
        self,
        gesture_tools: tuple[str, ...],
        turn_context: TurnContext,
    ) -> str:
        binding = self._device_binding
        session_id = (
            turn_context.session_info.session_id
            if turn_context.session_info is not None
            else None
        )
        if binding is None:
            log_turn_trace(
                stage="ACTION",
                event="blocked_unbound",
                session_id=session_id,
                tools=gesture_tools,
            )
            logger.warning("LLM 手势动作未执行：设备未绑定 | tools={}", gesture_tools)
            return "当前设备还没有绑定编排应用，暂时不能执行手势。"

        unavailable = [
            tool_name
            for tool_name in gesture_tools
            if tool_name not in binding.allowed_tools
        ]
        if unavailable:
            log_turn_trace(
                stage="ACTION",
                event="blocked_permission",
                session_id=session_id,
                requested=gesture_tools,
                unavailable=tuple(unavailable),
                allowed_tools=binding.allowed_tools,
                machine_id=getattr(binding, "machine_id", None),
                orchestration_id=getattr(binding, "orchestration_id", None),
            )
            logger.warning(
                (
                    "LLM 手势动作被编排权限拦截 | requested={} unavailable={} "
                    "allowed_tools={} machine_id={} orchestration_id={}"
                ),
                gesture_tools,
                tuple(unavailable),
                binding.allowed_tools,
                getattr(binding, "machine_id", None),
                getattr(binding, "orchestration_id", None),
            )
            names = "、".join(_gesture_tool_display_name(item) for item in unavailable)
            return f"当前编排未启用{names}，暂时不能执行这些手势。"

        completed: list[str] = []
        for tool_name in gesture_tools:
            display_name = _gesture_tool_display_name(tool_name)
            logger.info(
                "LLM 手势动作准备下发 | tool={} session_id={} machine_id={} orchestration_id={}",
                tool_name,
                session_id,
                getattr(binding, "machine_id", None),
                getattr(binding, "orchestration_id", None),
            )
            log_turn_trace(
                stage="ACTION",
                event="dispatch",
                session_id=session_id,
                tool=tool_name,
                machine_id=getattr(binding, "machine_id", None),
                orchestration_id=getattr(binding, "orchestration_id", None),
            )
            await turn_context.send_json(
                _build_edge_tool_call_event(
                    session_id=session_id,
                    name=display_name,
                    status="running",
                    status_label="进行中",
                    summary=f"正在执行{display_name}",
                    result="",
                    event="tool.invoke",
                )
            )
            try:
                await self.invoke_edge_tool(
                    tool_name=tool_name,
                    arguments={},
                    session_id=session_id,
                    timeout_ms=EDGE_TOOL_DEFAULT_TIMEOUT_MS,
                )
            except asyncio.TimeoutError:
                log_turn_trace(
                    stage="ACTION",
                    event="timeout",
                    session_id=session_id,
                    tool=tool_name,
                )
                logger.warning("边端手势调用超时 | tool={}", tool_name)
                await turn_context.send_json(
                    _build_edge_tool_call_event(
                        session_id=session_id,
                        name=display_name,
                        status="failed",
                        status_label="失败",
                        summary=f"{display_name}执行超时",
                        result="边端未在预期时间内返回结果",
                        event="tool.timeout",
                    )
                )
                return f"{display_name}执行超时，请稍后再试。"
            except EdgeToolInvocationError as exc:
                log_turn_trace(
                    stage="ACTION",
                    event="failed",
                    session_id=session_id,
                    tool=tool_name,
                    code=exc.code,
                    message=exc.message,
                )
                logger.warning(
                    "边端手势调用失败 | tool={} code={} message={}",
                    tool_name,
                    exc.code,
                    exc.message,
                )
                await turn_context.send_json(
                    _build_edge_tool_call_event(
                        session_id=session_id,
                        name=display_name,
                        status="failed",
                        status_label="失败",
                        summary=f"{display_name}执行失败",
                        result=exc.message,
                        event="tool.error",
                    )
                )
                return f"{display_name}执行失败：{exc.message}"

            completed.append(display_name)
            log_turn_trace(
                stage="ACTION",
                event="completed",
                session_id=session_id,
                tool=tool_name,
            )
            await turn_context.send_json(
                _build_edge_tool_call_event(
                    session_id=session_id,
                    name=display_name,
                    status="completed",
                    status_label="已完成",
                    summary=f"{display_name}执行完成",
                    result=f"{display_name}已完成",
                    event="tool.result",
                )
            )

        return f"好的，已执行{'、'.join(completed)}。"

    def _fail_pending_tool_invocations(self, reason: str) -> None:
        pending = list(self._pending_tool_invocations.values())
        pending_acks = list(self._pending_tool_acks.values())
        self._pending_tool_invocations.clear()
        self._pending_tool_acks.clear()
        for future in pending + pending_acks:
            if future.done():
                continue
            future.set_exception(ConnectionError(reason))

    async def _register_machine_connection(self, machine_id: str) -> None:
        normalized_machine_id = machine_id.strip()
        if not normalized_machine_id:
            return
        previous_machine_id = getattr(self, "_registered_machine_id", None)
        if previous_machine_id == normalized_machine_id:
            return
        if previous_machine_id:
            await self._unregister_machine_connection()
        if self.orchestration_service is None:
            self._registered_machine_id = normalized_machine_id
            return
        register_connection = getattr(
            self.orchestration_service,
            "register_machine_connection",
            None,
        )
        if callable(register_connection):
            await register_connection(normalized_machine_id, self)
        self._registered_machine_id = normalized_machine_id

    async def _unregister_machine_connection(self) -> None:
        machine_id = getattr(self, "_registered_machine_id", None)
        if not machine_id:
            return
        self._registered_machine_id = None
        if self.orchestration_service is None:
            return
        unregister_connection = getattr(
            self.orchestration_service,
            "unregister_machine_connection",
            None,
        )
        if callable(unregister_connection):
            await unregister_connection(machine_id, self)

    async def send_binding_message(self, payload: dict[str, object]) -> None:
        message_type = str(payload.get("type") or "").strip()
        machine_id = str(
            payload.get("machine_id")
            or getattr(getattr(self, "_device_binding", None), "machine_id", "")
            or self.device_id
            or ""
        ).strip()
        if message_type == ServerMessageType.DEVICE_UNBOUND.value:
            self._device_binding = None
            await self._send_json(
                {
                    "type": ServerMessageType.DEVICE_UNBOUND.value,
                    "machine_id": machine_id,
                    "binding_version": _coerce_binding_version(
                        payload.get("binding_version")
                    ),
                },
                suppress_connection_closed=True,
            )
            return

        binding = DeviceBinding(
            machine_id=machine_id,
            orchestration_id=str(payload.get("orchestration_id") or "").strip(),
            binding_version=_coerce_binding_version(payload.get("binding_version")),
            robot_id=_normalize_optional_text(payload.get("robot_id")),
            environment_id=_normalize_optional_text(payload.get("environment_id")),
            voice_id=_normalize_optional_text(payload.get("voice_id")),
            allowed_tools=_normalize_allowed_tools(payload.get("allowed_tools")),
            welcome_message=str(payload.get("welcome_message") or "").strip(),
        )
        self._device_binding = binding
        await self._send_json(
            {
                "type": (
                    message_type
                    if message_type
                    in {
                        ServerMessageType.DEVICE_BOUND.value,
                        ServerMessageType.BINDING_CHANGED.value,
                    }
                    else ServerMessageType.BINDING_CHANGED.value
                ),
                "machine_id": binding.machine_id,
                "orchestration_id": binding.orchestration_id,
                "binding_version": binding.binding_version,
                "robot_id": binding.robot_id,
                "environment_id": binding.environment_id,
                "voice_id": binding.voice_id,
                "allowed_tools": list(binding.allowed_tools),
                "welcome_message": binding.welcome_message,
            },
            suppress_connection_closed=True,
        )

    async def send_control_message(self, payload: dict[str, object]) -> None:
        await self._send_json(payload, suppress_connection_closed=True)

    async def _send_json(
        self,
        payload: dict[str, object],
        *,
        suppress_connection_closed: bool = False,
    ) -> None:
        if self.websocket is None:
            raise RuntimeError("ConnectionHandler 尚未绑定 websocket")
        packet = json.dumps(MessageEnvelope.wrap(payload), ensure_ascii=False)
        if suppress_connection_closed:
            with contextlib.suppress(ConnectionClosed):
                await self.websocket.send(packet)
            return
        await self.websocket.send(packet)


def _resolve_client_ip(
    ws: websockets.ServerConnection,
    headers: dict[str, str],
) -> str | None:
    real_ip = headers.get("x-real-ip") or headers.get("x-forwarded-for")
    if real_ip:
        return real_ip.split(",")[0].strip()

    remote_address = ws.remote_address
    if isinstance(remote_address, tuple) and remote_address:
        return str(remote_address[0])
    if remote_address is None:
        return None
    return str(remote_address)


def _coerce_binding_version(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                return int(float(stripped))
            except ValueError:
                return 0
    return 0


def _normalize_optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_allowed_tools(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    tool_names: list[str] = []
    for item in value:
        tool_name = str(item or "").strip()
        if tool_name and tool_name not in tool_names:
            tool_names.append(tool_name)
    return tuple(tool_names)


def _should_invoke_device_status_tool(transcript: str) -> bool:
    normalized = transcript.strip()
    if not normalized:
        return False
    if any(
        phrase in normalized
        for phrase in (
            "设备状态",
            "机器状态",
            "当前设备状态",
            "绑定状态",
            "当前绑定",
        )
    ):
        return True
    if "状态" in normalized and any(
        keyword in normalized
        for keyword in ("设备", "机器", "边端", "终端", "客户端", "绑定")
    ):
        return True
    if "在线" in normalized and any(
        keyword in normalized for keyword in ("设备", "机器", "边端", "终端", "客户端")
    ):
        return True
    return False


def _gesture_tool_display_name(tool_name: str) -> str:
    names = {
        EDGE_TOOL_GESTURE_OK: "OK 手势",
        EDGE_TOOL_GESTURE_EXTEND_HAND: "伸手手势",
        EDGE_TOOL_GESTURE_WAVE: "挥手手势",
        EDGE_TOOL_GESTURE_GUIDE: "指引手势",
    }
    return names.get(tool_name, tool_name)


def _build_device_status_reply(result: dict[str, Any]) -> str:
    machine_id = str(result.get("machine_id") or "unknown")
    state = str(result.get("state") or "unknown")
    binding = result.get("binding")
    if not isinstance(binding, dict):
        binding = {}
    is_bound = bool(binding.get("is_bound"))
    orchestration_id = str(binding.get("orchestration_id") or "").strip() or "未绑定"
    voice_id = str(binding.get("voice_id") or "").strip() or "未配置"
    return (
        f"当前设备 {machine_id} 处于 {state} 状态，"
        f"{'已绑定' if is_bound else '未绑定'}编排应用 {orchestration_id}，"
        f"当前语音配置是 {voice_id}。"
    )


def _build_edge_tool_call_event(
    *,
    session_id: str | None,
    name: str,
    status: str,
    status_label: str,
    summary: str,
    result: str,
    event: str,
) -> dict[str, object]:
    return {
        "type": ServerMessageType.TOOL_CALL.value,
        "session_id": session_id,
        "name": name,
        "status": status,
        "status_label": status_label,
        "summary": summary,
        "text": summary,
        "result": result,
        "event": event,
    }


__all__ = [
    "ConnectionHandler",
    "EdgeToolInvocationError",
    "_build_device_status_reply",
    "_should_invoke_device_status_tool",
]
