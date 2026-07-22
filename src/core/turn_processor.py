"""单轮语音结果的后处理链。"""

from __future__ import annotations

import asyncio
import json
from time import perf_counter

from collections.abc import Awaitable, Callable
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass

import numpy as np

from src.core.control_intent import ControlIntent, ControlKeywordSet
from src.core.turn_trace import log_turn_trace
from src.domain.session import ConversationRecorder, SessionInfo
from src.protocol.edge_cloud import ServerMessageType
from src.providers import InferenceModules, TtsStreamMetrics
from src.providers.llm import LlmServiceUnavailableError, SUPPORTED_LLM_ACTIONS
from src.utils.logging import logger

DEFAULT_EMPTY_REPLY = "我这次没有听清楚，请再说一遍。"
LLM_ACTION_NODE_NAME = "动作指令"
_NON_MEANINGFUL_CHARS = frozenset(" \t\r\n,，.。!！?？;；:：、'\"()[]{}<>《》【】~-_…")
_VISIBLE_STAGE_NAMES = {
    "问题改写": "理解问题",
    "文本召回（v2）": "检索知识",
    "智能问答": "生成回答",
}


@dataclass(frozen=True, slots=True)
class TurnContext:
    """单轮处理所需的连接快照。"""

    session_info: SessionInfo | None
    send_json: Callable[[dict[str, object]], Awaitable[None]]
    vad_elapsed_ms: float | None = None
    audio_duration_ms: float | None = None


@dataclass(frozen=True, slots=True)
class LlmStreamMetrics:
    first_delta_ms: float | None
    total_elapsed_ms: float
    chunk_count: int
    chunk_chars: int


EdgeToolHandler = Callable[[str, TurnContext], Awaitable[str | None]]
LlmActionHandler = Callable[[tuple[str, ...], TurnContext], Awaitable[None]]
LlmPayloadBuilder = Callable[[TurnContext], Awaitable[dict[str, object]]]


class TurnProcessor:
    """负责单轮语音的 ASR / LLM / 历史记录编排。"""

    def __init__(
        self,
        modules: InferenceModules,
        *,
        conversation_recorder: ConversationRecorder | None = None,
        edge_tool_handler: EdgeToolHandler | None = None,
        llm_action_handler: LlmActionHandler | None = None,
        llm_payload_builder: LlmPayloadBuilder | None = None,
        control_keywords: ControlKeywordSet | None = None,
    ) -> None:
        self._modules = modules
        self._conversation_recorder = conversation_recorder
        self._edge_tool_handler = edge_tool_handler
        self._llm_action_handler = llm_action_handler
        self._llm_payload_builder = llm_payload_builder
        self._control_keywords = control_keywords or ControlKeywordSet()

    async def process_turn(
        self,
        utterance: np.ndarray,
        *,
        turn_context: TurnContext,
        start_tts_reply: Callable[[str], asyncio.Task[TtsStreamMetrics]],
        start_tts_reply_stream: Callable[
            [asyncio.Queue[str | None]], asyncio.Task[TtsStreamMetrics]
        ],
    ) -> None:
        session_info = turn_context.session_info
        session_id = session_info.session_id if session_info is not None else None
        turn_started_at = perf_counter()

        transcript, asr_elapsed_ms = await self._transcribe_turn(
            utterance,
            session_id=session_id,
        )
        log_turn_trace(
            stage="ASR",
            event="result",
            session_id=session_id,
            text=transcript,
            text_len=len(transcript),
            elapsed_ms=asr_elapsed_ms,
        )
        control_result = self._control_keywords.classify(transcript)
        if control_result.intent is not ControlIntent.NORMAL:
            await self._send_control_result(
                turn_context,
                intent=control_result.intent,
                keyword=control_result.keyword,
                text=transcript,
            )
            logger.info(
                "ASR 控制意图命中，跳过 LLM/TTS | session_id={} intent={} keyword={} text={}",
                session_id,
                control_result.intent.value,
                control_result.keyword,
                transcript,
            )
            return

        await self._send_asr_result(turn_context, transcript)

        if not self._is_meaningful_text(transcript):
            reply_text = DEFAULT_EMPTY_REPLY
            tts_metrics = await start_tts_reply(reply_text)
            self._log_turn_summary(
                session_id=session_id,
                vad_elapsed_ms=turn_context.vad_elapsed_ms,
                audio_duration_ms=turn_context.audio_duration_ms,
                asr_elapsed_ms=asr_elapsed_ms,
                llm_metrics=None,
                tts_metrics=tts_metrics,
                transcript=transcript,
                reply_text=reply_text,
                total_elapsed_ms=(perf_counter() - turn_started_at) * 1000,
            )
            return

        edge_tool_reply = await self._maybe_handle_edge_tool(
            transcript,
            turn_context=turn_context,
        )
        if edge_tool_reply is not None:
            tts_metrics = await start_tts_reply(edge_tool_reply)
            await self._record_turn(
                turn_context,
                transcript=transcript,
                reply_text=edge_tool_reply,
            )
            self._log_turn_summary(
                session_id=session_id,
                vad_elapsed_ms=turn_context.vad_elapsed_ms,
                audio_duration_ms=turn_context.audio_duration_ms,
                asr_elapsed_ms=asr_elapsed_ms,
                llm_metrics=None,
                tts_metrics=tts_metrics,
                transcript=transcript,
                reply_text=edge_tool_reply,
                total_elapsed_ms=(perf_counter() - turn_started_at) * 1000,
            )
            return

        try:
            reply_text, llm_metrics, tts_metrics = await self._stream_reply_and_tts(
                transcript,
                turn_context=turn_context,
                session_id=session_id,
                start_tts_reply_stream=start_tts_reply_stream,
            )
        except LlmServiceUnavailableError as exc:
            reply_text = str(exc)
            await turn_context.send_json(
                {
                    "type": ServerMessageType.LLM_DELTA.value,
                    "session_id": session_id,
                    "text": reply_text,
                }
            )
            tts_metrics = await start_tts_reply(reply_text)
            await self._record_turn(
                turn_context,
                transcript=transcript,
                reply_text=reply_text,
            )
            self._log_turn_summary(
                session_id=session_id,
                vad_elapsed_ms=turn_context.vad_elapsed_ms,
                audio_duration_ms=turn_context.audio_duration_ms,
                asr_elapsed_ms=asr_elapsed_ms,
                llm_metrics=None,
                tts_metrics=tts_metrics,
                transcript=transcript,
                reply_text=reply_text,
                total_elapsed_ms=(perf_counter() - turn_started_at) * 1000,
            )
            return
        await self._record_turn(
            turn_context,
            transcript=transcript,
            reply_text=reply_text,
        )
        self._log_turn_summary(
            session_id=session_id,
            vad_elapsed_ms=turn_context.vad_elapsed_ms,
            audio_duration_ms=turn_context.audio_duration_ms,
            asr_elapsed_ms=asr_elapsed_ms,
            llm_metrics=llm_metrics,
            tts_metrics=tts_metrics,
            transcript=transcript,
            reply_text=reply_text,
            total_elapsed_ms=(perf_counter() - turn_started_at) * 1000,
        )

    async def _transcribe_turn(
        self,
        utterance: np.ndarray,
        *,
        session_id: str | None,
    ) -> tuple[str, float]:
        started_at = perf_counter()
        if utterance.size == 0:
            return "", (perf_counter() - started_at) * 1000

        transcript = await asyncio.to_thread(self._transcribe_utterance, utterance)
        return transcript, (perf_counter() - started_at) * 1000

    async def _send_asr_result(
        self,
        turn_context: TurnContext,
        transcript: str,
    ) -> None:
        session_id = (
            turn_context.session_info.session_id
            if turn_context.session_info is not None
            else None
        )
        await turn_context.send_json(
            {
                "type": ServerMessageType.ASR_RESULT.value,
                "text": transcript,
                "session_id": session_id,
            }
        )

    async def _send_control_result(
        self,
        turn_context: TurnContext,
        *,
        intent: ControlIntent,
        keyword: str,
        text: str,
    ) -> None:
        session_id = (
            turn_context.session_info.session_id
            if turn_context.session_info is not None
            else None
        )
        await turn_context.send_json(
            {
                "type": ServerMessageType.SESSION_CONTROL.value,
                "intent": intent.value,
                "keyword": keyword,
                "text": text,
                "session_id": session_id,
            }
        )

    async def _record_turn(
        self,
        turn_context: TurnContext,
        *,
        transcript: str,
        reply_text: str,
    ) -> None:
        session_info = turn_context.session_info
        if self._conversation_recorder is None or session_info is None:
            return

        await self._conversation_recorder.record_turn(
            session_info,
            user_text=transcript,
            assistant_text=reply_text,
        )
        logger.debug(
            "当前轮次历史已记录 | session_id={} user_len={} reply_len={}",
            session_info.session_id,
            len(transcript),
            len(reply_text),
        )

    async def _stream_reply_and_tts(
        self,
        transcript: str,
        *,
        turn_context: TurnContext,
        session_id: str | None,
        start_tts_reply_stream: Callable[
            [asyncio.Queue[str | None]], asyncio.Task[TtsStreamMetrics]
        ],
    ) -> tuple[str, LlmStreamMetrics, TtsStreamMetrics]:
        if self._modules.llm is None:
            raise ValueError("TurnProcessor 推理失败: 缺少 LLM 模块")

        loop = asyncio.get_running_loop()
        chunk_queue: asyncio.Queue[str | None] = asyncio.Queue()
        tts_task = start_tts_reply_stream(chunk_queue)
        started_at = perf_counter()
        first_forwarded_chunk_at: float | None = None
        previous_chunk_at: float | None = None
        chunk_count = 0
        chunk_chars = 0
        pending_emit_tasks: set[asyncio.Future[None]] = set()
        pending_action_futures: list[ConcurrentFuture[None]] = []
        executed_actions: set[str] = set()
        stage_started_at: dict[str, float] = {}
        cancelled = False

        def schedule_emit(payload: dict[str, object]) -> None:
            def create_emit_task() -> None:
                task: asyncio.Future[None] = asyncio.ensure_future(
                    turn_context.send_json(payload),
                )
                pending_emit_tasks.add(task)
                task.add_done_callback(pending_emit_tasks.discard)

            loop.call_soon_threadsafe(create_emit_task)

        def on_chunk(chunk: str) -> None:
            nonlocal \
                first_forwarded_chunk_at, \
                previous_chunk_at, \
                chunk_count, \
                chunk_chars
            chunk_count += 1
            chunk_chars += len(chunk)
            chunk_at = perf_counter()
            if first_forwarded_chunk_at is None:
                first_forwarded_chunk_at = chunk_at
                logger.debug(
                    "LLM 首次增量下发 | session_id={} first_delta_ms={:.1f} chunk_len={}",
                    session_id,
                    (first_forwarded_chunk_at - started_at) * 1000,
                    len(chunk),
                )
            elif previous_chunk_at is not None:
                gap_ms = (chunk_at - previous_chunk_at) * 1000
                if gap_ms >= 800:
                    logger.debug(
                        "LLM 增量间隔 | session_id={} gap_ms={:.1f} chunk_index={} chunk_len={}",
                        session_id,
                        gap_ms,
                        chunk_count,
                        len(chunk),
                    )
            previous_chunk_at = chunk_at
            loop.call_soon_threadsafe(chunk_queue.put_nowait, chunk)
            schedule_emit(
                {
                    "type": ServerMessageType.LLM_DELTA.value,
                    "session_id": session_id,
                    "text": chunk,
                }
            )

        def on_event(event_payload: dict[str, object]) -> None:
            llm = self._modules.llm
            if llm is None:
                return
            node_name = str(event_payload.get("node_name") or "").strip()
            event_name = str(event_payload.get("event") or "").strip()
            actions = _extract_llm_action_tools(event_payload)
            if node_name == LLM_ACTION_NODE_NAME and event_name in {
                "on_chain_end",
                "on_chat_model_end",
            }:
                log_turn_trace(
                    stage="ACTION",
                    event="parsed" if actions else "no_actions",
                    session_id=session_id,
                    node=node_name,
                    llm_event=event_name,
                    actions=actions,
                )
            if actions:
                logger.debug(
                    "LLM 动作指令已解析 | session_id={} actions={}",
                    session_id,
                    actions,
                )
                new_actions = tuple(
                    action for action in actions if action not in executed_actions
                )
                if new_actions:
                    executed_actions.update(new_actions)
                    schedule_llm_actions(new_actions)

            tool_event = _build_tool_call_event(
                event_payload,
                session_id=session_id,
                answer_node_name=llm.stream_answer_node_name,
                stage_started_at=stage_started_at,
            )
            if tool_event is None:
                return
            schedule_emit(tool_event)

        def schedule_llm_actions(actions: tuple[str, ...]) -> None:
            future = asyncio.run_coroutine_threadsafe(
                self._handle_llm_actions(actions, turn_context),
                loop,
            )
            pending_action_futures.append(future)

        try:
            extra_payload = await self._build_llm_payload(turn_context)
            log_turn_trace(
                stage="LLM",
                event="request",
                session_id=session_id,
                input=transcript,
                input_len=len(transcript),
                extra_payload=extra_payload,
            )
            reply_text = await asyncio.to_thread(
                self._modules.llm.stream_reply,
                transcript,
                conversation_id=session_id or "nexus-default-session",
                on_chunk=on_chunk,
                on_event=on_event,
                extra_payload=extra_payload,
            )
            log_turn_trace(
                stage="LLM",
                event="response",
                session_id=session_id,
                output=reply_text,
                output_len=len(reply_text),
                chunks=chunk_count,
            )
        except asyncio.CancelledError:
            cancelled = True
            reply_text = ""
        except Exception:
            tts_task.cancel()
            raise
        finally:
            loop.call_soon_threadsafe(chunk_queue.put_nowait, None)
            tts_metrics: TtsStreamMetrics | None = None
            try:
                tts_metrics = await tts_task
            except asyncio.CancelledError:
                cancelled = True
            if pending_emit_tasks:
                await asyncio.gather(*list(pending_emit_tasks), return_exceptions=True)
            if pending_action_futures:
                await asyncio.gather(
                    *[asyncio.wrap_future(future) for future in pending_action_futures],
                    return_exceptions=True,
                )
        if cancelled:
            raise asyncio.CancelledError
        llm_metrics = LlmStreamMetrics(
            first_delta_ms=None
            if first_forwarded_chunk_at is None
            else (first_forwarded_chunk_at - started_at) * 1000,
            total_elapsed_ms=(perf_counter() - started_at) * 1000,
            chunk_count=chunk_count,
            chunk_chars=chunk_chars,
        )
        if tts_metrics is None:
            raise RuntimeError("TTS 任务未返回有效统计信息")
        return reply_text, llm_metrics, tts_metrics

    def _transcribe_utterance(self, utterance: np.ndarray) -> str:
        if self._modules.asr is None:
            raise ValueError("TurnProcessor 推理失败: 缺少 ASR 模块")
        with self._modules.inference_lock:
            return self._modules.asr.transcribe(utterance).strip()

    def _generate_reply(self, transcript: str, *, conversation_id: str) -> str:
        if self._modules.llm is None:
            raise ValueError("TurnProcessor 推理失败: 缺少 LLM 模块")
        return self._modules.llm.generate_reply(
            transcript,
            conversation_id=conversation_id,
        )

    async def _build_llm_payload(
        self,
        turn_context: TurnContext,
    ) -> dict[str, object]:
        if self._llm_payload_builder is None:
            return {}
        payload = await self._llm_payload_builder(turn_context)
        return payload if isinstance(payload, dict) else {}

    async def _maybe_handle_edge_tool(
        self,
        transcript: str,
        *,
        turn_context: TurnContext,
    ) -> str | None:
        if self._edge_tool_handler is None:
            return None
        return await self._edge_tool_handler(transcript, turn_context)

    async def _handle_llm_actions(
        self,
        actions: tuple[str, ...],
        turn_context: TurnContext,
    ) -> None:
        if self._llm_action_handler is None:
            return
        await self._llm_action_handler(actions, turn_context)

    @staticmethod
    def _is_meaningful_text(text: str) -> bool:
        normalized = text.strip()
        if not normalized:
            return False
        return any(char not in _NON_MEANINGFUL_CHARS for char in normalized)

    def _log_turn_summary(
        self,
        *,
        session_id: str | None,
        vad_elapsed_ms: float | None,
        audio_duration_ms: float | None,
        asr_elapsed_ms: float,
        llm_metrics: LlmStreamMetrics | None,
        tts_metrics: TtsStreamMetrics,
        transcript: str,
        reply_text: str,
        total_elapsed_ms: float,
    ) -> None:
        logger.debug(
            "单轮摘要 | session_id={} vad_ms={} audio_ms={} asr_ms={:.1f} llm_first_ms={} llm_total_ms={} llm_chunks={} tts_first_ms={} tts_total_ms={:.1f} total_ms={:.1f} user_len={} reply_len={}",
            session_id,
            _fmt_ms(vad_elapsed_ms),
            _fmt_ms(audio_duration_ms),
            asr_elapsed_ms,
            _fmt_ms(None if llm_metrics is None else llm_metrics.first_delta_ms),
            _fmt_ms(None if llm_metrics is None else llm_metrics.total_elapsed_ms),
            0 if llm_metrics is None else llm_metrics.chunk_count,
            _fmt_ms(tts_metrics.first_packet_ms),
            tts_metrics.total_elapsed_ms,
            total_elapsed_ms,
            len(transcript),
            len(reply_text),
        )
        log_turn_trace(
            stage="TTS",
            event="done",
            session_id=session_id,
            first_packet_ms=_fmt_ms(tts_metrics.first_packet_ms),
            total_ms=tts_metrics.total_elapsed_ms,
            frames=tts_metrics.frame_count,
            text_len=tts_metrics.text_len,
            segments=tts_metrics.segment_count,
            mode=tts_metrics.mode,
        )
        log_turn_trace(
            stage="SUMMARY",
            event="turn_done",
            session_id=session_id,
            vad_ms=_fmt_ms(vad_elapsed_ms),
            audio_ms=_fmt_ms(audio_duration_ms),
            asr_ms=asr_elapsed_ms,
            llm_first_ms=_fmt_ms(
                None if llm_metrics is None else llm_metrics.first_delta_ms
            ),
            llm_total_ms=_fmt_ms(
                None if llm_metrics is None else llm_metrics.total_elapsed_ms
            ),
            llm_chunks=0 if llm_metrics is None else llm_metrics.chunk_count,
            tts_first_ms=_fmt_ms(tts_metrics.first_packet_ms),
            tts_total_ms=tts_metrics.total_elapsed_ms,
            total_ms=total_elapsed_ms,
            user=transcript,
            reply=reply_text,
        )


def _fmt_ms(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}"


def _build_tool_call_event(
    payload: dict[str, object],
    *,
    session_id: str | None,
    answer_node_name: str,
    stage_started_at: dict[str, float],
) -> dict[str, object] | None:
    event_name = str(payload.get("event") or "").strip()
    node_name = str(payload.get("node_name") or "").strip()
    if not event_name or not node_name:
        return None
    if event_name not in {"on_chain_start", "on_chain_end"}:
        return None
    display_name = _VISIBLE_STAGE_NAMES.get(node_name)
    if display_name is None:
        return None

    status = _resolve_event_status(event_name)
    if status is None:
        return None
    now = perf_counter()
    duration_ms: float | None = None
    if status == "running":
        stage_started_at[display_name] = now
    elif status in {"completed", "failed"}:
        started_at = stage_started_at.pop(display_name, None)
        if started_at is not None:
            duration_ms = round((now - started_at) * 1000, 1)

    summary = _build_stage_summary(
        display_name=display_name,
        status=status,
        is_answer_stage=node_name == answer_node_name,
    )
    payload_out: dict[str, object] = {
        "type": ServerMessageType.TOOL_CALL.value,
        "session_id": session_id,
        "name": display_name,
        "event": event_name,
        "status": status,
        "status_label": _status_label(status),
        "summary": summary,
        "text": summary,
        "result": _build_stage_result(
            display_name=display_name,
            status=status,
            is_answer_stage=node_name == answer_node_name,
        ),
    }
    if duration_ms is not None:
        payload_out["duration_ms"] = duration_ms
    return payload_out


def _extract_llm_action_tools(payload: dict[str, object]) -> tuple[str, ...]:
    node_name = str(payload.get("node_name") or "").strip()
    event_name = str(payload.get("event") or "").strip()
    if node_name != LLM_ACTION_NODE_NAME:
        return ()
    if event_name not in {"on_chain_end", "on_chat_model_end"}:
        return ()

    candidates: list[object] = []
    if "data" in payload:
        candidates.append(payload.get("data"))
    output_params = payload.get("output_params")
    if isinstance(output_params, dict):
        candidates.append(output_params)
        for key in ("output", "answer", "actions"):
            if key in output_params:
                candidates.append(output_params.get(key))

    actions: list[str] = []
    for candidate in candidates:
        for action in _extract_actions_from_value(candidate):
            if action not in actions:
                actions.append(action)
    return tuple(actions)


def _extract_actions_from_value(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return _filter_supported_actions(value)
    if isinstance(value, dict):
        candidates: list[object] = []
        if "actions" in value:
            candidates.append(value.get("actions"))
        data = value.get("data")
        if isinstance(data, dict):
            candidates.append(data)
        for key in ("output", "answer"):
            if key in value:
                candidates.append(value.get(key))

        actions: list[str] = []
        for candidate in candidates:
            for action in _extract_actions_from_value(candidate):
                if action not in actions:
                    actions.append(action)
        return tuple(actions)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        if stripped in SUPPORTED_LLM_ACTIONS:
            return (stripped,)
        split_actions = _split_action_text(stripped)
        if split_actions:
            return split_actions
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return ()
        return _extract_actions_from_value(parsed)
    return ()


def _split_action_text(value: str) -> tuple[str, ...]:
    actions: list[str] = []
    normalized = (
        value.replace("，", ",").replace("、", ",").replace(";", ",").replace("；", ",")
    )
    for item in normalized.split(","):
        action = item.strip()
        if action in SUPPORTED_LLM_ACTIONS and action not in actions:
            actions.append(action)
    return tuple(actions)


def _filter_supported_actions(values: list[object]) -> tuple[str, ...]:
    actions: list[str] = []
    for item in values:
        action = item.strip() if isinstance(item, str) else ""
        if action in SUPPORTED_LLM_ACTIONS and action not in actions:
            actions.append(action)
    return tuple(actions)


def _resolve_event_status(event_name: str) -> str | None:
    if event_name.endswith("_start"):
        return "running"
    if event_name.endswith("_end"):
        return "completed"
    if event_name.endswith("_error"):
        return "failed"
    return None


def _status_label(status: str) -> str:
    if status == "running":
        return "进行中"
    if status == "completed":
        return "已完成"
    if status == "failed":
        return "失败"
    return status


def _build_stage_summary(
    *,
    display_name: str,
    status: str,
    is_answer_stage: bool,
) -> str:
    if status == "running":
        return f"正在{display_name}"
    if status == "completed":
        return "回答已生成完成" if is_answer_stage else f"{display_name}已完成"
    return f"{display_name}失败"


def _build_stage_result(
    *,
    display_name: str,
    status: str,
    is_answer_stage: bool,
) -> str:
    if status == "running":
        return ""
    if status == "completed":
        return "已得到最终回答" if is_answer_stage else f"{display_name}完成"
    return f"{display_name}异常结束"


__all__ = ["EdgeToolHandler", "TurnContext", "TurnProcessor"]
