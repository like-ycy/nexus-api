"""云端 LLM 服务。"""

from __future__ import annotations

import json

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from urllib import error, request

from src.utils.logging import logger


DEFAULT_EMPTY_REPLY = "我这次没有听清楚，请再说一遍。"
DEFAULT_STREAM_ANSWER_NODE_NAME = "智能问答"
LLM_SERVICE_UNAVAILABLE_MESSAGE = "抱歉，当前智能问答服务暂时不可用，请稍后再试。"
SUPPORTED_LLM_ACTIONS: tuple[str, ...] = (
    "gesture.ok",
    "gesture.extend_hand",
    "gesture.wave",
    "gesture.guide",
)


class LlmServiceUnavailableError(RuntimeError):
    """上游大模型服务不可用。"""


@dataclass(frozen=True, slots=True)
class LlmReply:
    """标准化后的大模型回复。"""

    answer: str
    actions: tuple[str, ...] = ()


class RemoteReplyGenerator:
    """调用远端 HTTP 接口生成回复。"""

    def __init__(
        self,
        *,
        endpoint: str | None,
        api_key: str | None,
        is_app_uid: bool = True,
        request_timeout_sec: float = 20.0,
        fallback_reply: str,
        streaming_enabled: bool = True,
        stream_answer_node_name: str = DEFAULT_STREAM_ANSWER_NODE_NAME,
    ) -> None:
        self._endpoint = endpoint.strip() if endpoint else ""
        self._api_key = api_key.strip() if api_key else ""
        self._is_app_uid = is_app_uid
        self._request_timeout_sec = request_timeout_sec
        self._fallback_reply = fallback_reply.strip() or DEFAULT_EMPTY_REPLY
        self._streaming_enabled = streaming_enabled
        self._stream_answer_node_name = (
            stream_answer_node_name.strip() or DEFAULT_STREAM_ANSWER_NODE_NAME
        )

    @property
    def streaming_enabled(self) -> bool:
        return self._streaming_enabled

    @property
    def stream_answer_node_name(self) -> str:
        return self._stream_answer_node_name

    def generate_reply(
        self,
        transcript: str,
        *,
        conversation_id: str,
        extra_payload: dict[str, object] | None = None,
    ) -> str:
        return self.generate_structured_reply(
            transcript,
            conversation_id=conversation_id,
            extra_payload=extra_payload,
        ).answer

    def generate_structured_reply(
        self,
        transcript: str,
        *,
        conversation_id: str,
        extra_payload: dict[str, object] | None = None,
    ) -> LlmReply:
        normalized = transcript.strip()
        if not normalized:
            return LlmReply(answer=DEFAULT_EMPTY_REPLY)

        if not self._endpoint or not self._api_key:
            logger.warning("LLM 远端配置缺失")
            raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE)

        payload = _build_request_payload(
            is_app_uid=self._is_app_uid,
            conversation_id=conversation_id,
            transcript=normalized,
            extra_payload=extra_payload,
        )
        req = request.Request(
            self._endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self._api_key,
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self._request_timeout_sec) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            logger.error(
                "LLM HTTP 请求失败 | status={} reason={} body={}",
                exc.code,
                exc.reason,
                detail,
            )
            raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE) from exc
        except error.URLError as exc:
            logger.error("LLM 网络请求失败: {}", exc)
            raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE) from exc
        except Exception as exc:
            logger.error("LLM 请求异常: {}", exc)
            raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE) from exc

        try:
            payload_obj = json.loads(body)
        except json.JSONDecodeError as exc:
            logger.error("LLM 返回非 JSON 响应: {} | body={}", exc, body)
            raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE) from exc

        if not _is_success_payload(payload_obj):
            logger.error("LLM 响应业务失败: {}", payload_obj)
            raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE)

        reply = extract_reply(payload_obj)
        if reply.answer:
            return reply

        logger.error("LLM 响应缺少 answer 字段: {}", payload_obj)
        raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE)

    def stream_reply(
        self,
        transcript: str,
        *,
        conversation_id: str,
        on_chunk: Callable[[str], None],
        on_event: Callable[[dict[str, object]], None] | None = None,
        extra_payload: dict[str, object] | None = None,
    ) -> str:
        normalized = transcript.strip()
        if not normalized:
            on_chunk(DEFAULT_EMPTY_REPLY)
            return DEFAULT_EMPTY_REPLY

        if not self._endpoint or not self._api_key:
            logger.warning("LLM 远端配置缺失")
            raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE)

        if not self._streaming_enabled:
            reply = self.generate_reply(
                normalized,
                conversation_id=conversation_id,
                extra_payload=extra_payload,
            )
            on_chunk(reply)
            return reply

        payload = _build_request_payload(
            is_app_uid=self._is_app_uid,
            conversation_id=conversation_id,
            transcript=normalized,
            extra_payload=extra_payload,
        )
        payload["__streaming"] = True
        req = request.Request(
            self._endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "x-api-key": self._api_key,
            },
            method="POST",
        )

        streamed_chunks: list[str] = []
        final_answer = ""
        try:
            with request.urlopen(req, timeout=self._request_timeout_sec) as response:
                content_type = str(response.headers.get("Content-Type") or "").lower()
                if "text/event-stream" not in content_type:
                    body = response.read().decode("utf-8", errors="replace")
                    return _handle_non_sse_response(
                        body,
                        fallback_reply=self._fallback_reply,
                        on_chunk=on_chunk,
                    )
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    event_payload = _parse_sse_payload(line[5:].strip())
                    if event_payload is None:
                        continue
                    if on_event is not None:
                        on_event(deepcopy(event_payload))
                    chunk = extract_stream_chunk(
                        event_payload,
                        answer_node_name=self._stream_answer_node_name,
                    )
                    if chunk:
                        streamed_chunks.append(chunk)
                        on_chunk(chunk)

                    answer = extract_stream_answer(
                        event_payload,
                        answer_node_name=self._stream_answer_node_name,
                    )
                    if answer:
                        final_answer = answer
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            logger.error(
                "LLM SSE 请求失败 | status={} reason={} body={}",
                exc.code,
                exc.reason,
                detail,
            )
            raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE) from exc
        except error.URLError as exc:
            logger.error("LLM SSE 网络请求失败: {}", exc)
            raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE) from exc
        except Exception as exc:
            logger.error("LLM SSE 请求异常: {}", exc)
            raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE) from exc

        if final_answer:
            return final_answer.strip()
        if streamed_chunks:
            combined_answer = "".join(streamed_chunks).strip()
            return combined_answer

        logger.error("LLM SSE 响应缺少有效 answer: transcript={}", normalized)
        raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE)


def _build_request_payload(
    *,
    is_app_uid: bool,
    conversation_id: str,
    transcript: str,
    extra_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "is_app_uid": is_app_uid,
        "CONVERSATION_ID": conversation_id,
        "QUESTION": transcript,
    }
    if extra_payload:
        payload.update(extra_payload)
    return payload


def _handle_non_sse_response(
    body: str,
    *,
    fallback_reply: str,
    on_chunk: Callable[[str], None],
) -> str:
    del fallback_reply, on_chunk
    try:
        payload_obj = json.loads(body)
    except json.JSONDecodeError as exc:
        logger.error("LLM SSE 返回了非 SSE 且非 JSON 响应: {}", body)
        raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE) from exc

    if not _is_success_payload(payload_obj):
        logger.error("LLM SSE 响应业务失败: {}", payload_obj)
        raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE)

    reply = extract_reply(payload_obj)
    if reply.answer:
        return reply.answer

    logger.error("LLM SSE 返回了非 SSE JSON，但缺少 answer 字段: {}", payload_obj)
    raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE)


def _is_success_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    if code is None:
        return True
    return str(code).strip() == "200"


def _parse_sse_payload(raw_payload: str) -> dict[str, object] | None:
    if not raw_payload:
        return None
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def extract_answer(payload: object) -> str:
    """从非流式响应体中提取 answer 文本。"""
    return extract_reply(payload).answer


def extract_reply(payload: object) -> LlmReply:
    """从非流式响应体中提取标准 answer/actions 结构。"""
    if not isinstance(payload, dict):
        return LlmReply(answer="")

    data = payload.get("data")
    if not isinstance(data, dict):
        return LlmReply(answer="")

    answer = data.get("answer")
    if not isinstance(answer, str):
        return LlmReply(answer="")

    normalized_answer = _extract_embedded_answer(answer) or answer.strip()
    return LlmReply(
        answer=normalized_answer,
        actions=_extract_actions(data.get("actions")),
    )


def _extract_actions(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()

    actions: list[str] = []
    for item in value:
        action = item.strip() if isinstance(item, str) else ""
        if action in SUPPORTED_LLM_ACTIONS and action not in actions:
            actions.append(action)
    return tuple(actions)


def extract_stream_chunk(
    payload: object,
    *,
    answer_node_name: str = DEFAULT_STREAM_ANSWER_NODE_NAME,
) -> str:
    if not isinstance(payload, dict):
        return ""
    if payload.get("event") != "on_chat_model_stream":
        return ""
    if str(payload.get("node_name") or "") != answer_node_name:
        return ""

    chunk = payload.get("data")
    if isinstance(chunk, dict):
        return _extract_answer_from_mapping(chunk)
    if not isinstance(chunk, str):
        return ""
    return _extract_embedded_answer(chunk) or chunk


def extract_stream_answer(
    payload: object,
    *,
    answer_node_name: str = DEFAULT_STREAM_ANSWER_NODE_NAME,
) -> str:
    if not isinstance(payload, dict):
        return ""

    event_name = str(payload.get("event") or "")
    node_name = str(payload.get("node_name") or "")
    if node_name != answer_node_name:
        return ""

    if event_name == "on_chat_model_end":
        answer = payload.get("data")
        if isinstance(answer, dict):
            return _extract_answer_from_mapping(answer)
        if not isinstance(answer, str):
            return ""
        return _extract_embedded_answer(answer) or answer.strip()

    if event_name == "on_chain_end":
        output_params = payload.get("output_params")
        if not isinstance(output_params, dict):
            return ""
        answer = output_params.get("output") or output_params.get("answer")
        if isinstance(answer, dict):
            return _extract_answer_from_mapping(answer)
        if not isinstance(answer, str):
            return ""
        return _extract_embedded_answer(answer) or answer.strip()

    return ""


def _extract_embedded_answer(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return _extract_answer_from_mapping(payload)


def _extract_answer_from_mapping(payload: dict[str, object]) -> str:
    answer = payload.get("answer")
    if isinstance(answer, str):
        return answer.strip()
    data = payload.get("data")
    if isinstance(data, dict):
        return _extract_answer_from_mapping(data)
    output = payload.get("output")
    if isinstance(output, dict):
        return _extract_answer_from_mapping(output)
    if isinstance(output, str):
        return _extract_embedded_answer(output)
    return ""


FixedReplyGenerator = RemoteReplyGenerator
