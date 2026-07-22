"""编排应用配置与对话服务。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import wave

from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from src.core.connection_registry import BindingConnection, ConnectionRegistry
from src.db.machine_binding_store import (
    MachineBindingStore,
    PostgresMachineBindingStore,
)
from src.db.orchestration_store import OrchestrationStore, PostgresOrchestrationStore
from src.db.robot_skill_store import PostgresRobotSkillStore, RobotSkillStore
from src.db.environment_store import EnvironmentStore, PostgresEnvironmentStore
from src.db.equipment_store import EquipmentStore, PostgresEquipmentStore
from src.domain import MachineBindingRecord, OrchestrationRecord, RobotSkillRecord
from src.domain.session import SessionInfo
from src.protocol.edge_cloud import ServerMessageType
from src.providers.llm import LlmReply, LlmServiceUnavailableError, RemoteReplyGenerator
from src.providers.module_initializer import initialize_modules
from src.providers.tts import build_tts_provider
from src.services.conversation import ConversationService
from src.services.knowledge_base import KnowledgeBaseService
from src.services.machine_directory import MachineDirectoryClient, MachineRecord
from src.utils.logging import logger

DEFAULT_ROBOT_OPTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "robot_a",
        "name": "A 机器人",
        "description": "展厅接待区",
        "status": "ready",
    },
    {
        "id": "robot_b",
        "name": "B 机器人",
        "description": "产品讲解区",
        "status": "idle",
    },
)

DEFAULT_ENVIRONMENT_OPTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "env_exhibition",
        "name": "展厅接待区环境",
        "description": "默认展厅环境配置",
    },
)

DEFAULT_VOICE_OPTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "zh_female_qinqienvsheng_moon_bigtts",
        "name": "亲切女声",
        "description": "柔和亲和，适合导览和咨询。",
        "provider": "doubao",
        "language": "中文",
        "scene": "通用场景",
    },
    {
        "id": "zh_female_wenrouxiaoya_moon_bigtts",
        "name": "温柔小雅",
        "description": "语气温柔，适合服务型场景。",
        "provider": "doubao",
        "language": "中文",
        "scene": "客服场景",
    },
    {
        "id": "zh_female_wanwanxiaohe_moon_bigtts",
        "name": "湾湾小何",
        "description": "台湾口音路线，语气更软一些，适合需要更轻柔腔调的场景。",
        "provider": "doubao",
        "language": "中文-台湾口音",
        "scene": "通用场景",
    },
    {
        "id": "zh_male_jingqiangkanye_moon_bigtts",
        "name": "京腔侃爷",
        "description": "辨识度很高，适合更有个性的男声路线。",
        "provider": "doubao",
        "language": "中文-北京口音,美式英语",
        "scene": "通用场景",
    },
    {
        "id": "zh_male_shenyeboke_emo_v2_mars_bigtts",
        "name": "深夜播客",
        "description": "低沉稳重，适合成熟陪伴感场景。",
        "provider": "doubao",
        "language": "中文",
        "scene": "多情感",
    },
    {
        "id": "zh_female_gaolengyujie_emo_v2_mars_bigtts",
        "name": "高冷御姐",
        "description": "质感成熟利落，适合更有风格的女声路线。",
        "provider": "doubao",
        "language": "中文",
        "scene": "多情感",
    },
    {
        "id": "ICL_zh_female_qingtiantaotao_cs_tob",
        "name": "清甜桃桃",
        "description": "客服向音色，整体清楚稳定，也比较耐听。",
        "provider": "doubao",
        "language": "中文",
        "scene": "客服场景",
    },
    {
        "id": "ICL_zh_female_lixingyuanzi_cs_tob",
        "name": "理性圆子",
        "description": "偏理性中性的女声，适合不想太甜也不想太冷的路线。",
        "provider": "doubao",
        "language": "中文",
        "scene": "客服场景",
    },
)


class OrchestrationNotFoundError(KeyError):
    """编排应用不存在。"""


class MachineNotFoundError(KeyError):
    """设备不存在于真实机器人目录中。"""


class RobotNotFoundError(KeyError):
    """机器人不存在于真实机器人目录中。"""


LLM_SERVICE_UNAVAILABLE_MESSAGE = "抱歉，当前智能问答服务暂时不可用，请稍后再试。"


@dataclass(frozen=True, slots=True)
class _ResolvedChatContext:
    orchestration_id: str
    question: str
    response_mode: str
    session_id: str
    machine_id: str
    record: OrchestrationRecord
    knowledge_base: dict[str, Any] | None
    robot: dict[str, Any] | None
    environment: dict[str, Any] | None
    voice: dict[str, Any] | None
    skills: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class DeviceBinding:
    """服务端解析出的设备绑定结果。"""

    machine_id: str
    orchestration_id: str
    binding_version: int = 0
    robot_id: str | None = None
    environment_id: str | None = None
    voice_id: str | None = None
    allowed_tools: tuple[str, ...] = ()
    welcome_message: str = ""


class OrchestrationService:
    """提供编排应用创建、配置保存和对话能力。"""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        store: OrchestrationStore | None = None,
        robot_skill_store: RobotSkillStore | None = None,
        machine_binding_store: MachineBindingStore | None = None,
        machine_directory_client: MachineDirectoryClient | None = None,
        connection_registry: ConnectionRegistry | None = None,
        llm_generator: RemoteReplyGenerator | None = None,
        audio_renderer: Any | None = None,
        conversation_service: ConversationService | None = None,
        knowledge_base_service: KnowledgeBaseService | None = None,
        environment_store: EnvironmentStore | None = None,
        equipment_store: EquipmentStore | None = None,
    ) -> None:
        self._config = deepcopy(config)
        self._machine_directory = (
            machine_directory_client
            if machine_directory_client is not None
            else _build_machine_directory_client(config)
        )
        self._store = store if store is not None else _build_orchestration_store(config)
        self._robot_skill_store = (
            robot_skill_store
            if robot_skill_store is not None
            else _build_robot_skill_store(config)
        )
        self._machine_binding_store = (
            machine_binding_store
            if machine_binding_store is not None
            else _build_machine_binding_store(config)
        )
        self._environment_store = (
            environment_store
            if environment_store is not None
            else _build_environment_store(config)
        )
        self._equipment_store = (
            equipment_store
            if equipment_store is not None
            else _build_equipment_store(config)
        )
        self._connection_registry = connection_registry or ConnectionRegistry()
        self._llm_generator = (
            llm_generator if llm_generator is not None else _build_llm(config)
        )
        self._audio_renderer = audio_renderer
        self._conversation_service = conversation_service
        self._knowledge_base_service = knowledge_base_service
        self._reference_store_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._started = False

    @property
    def connection_registry(self) -> ConnectionRegistry:
        return self._connection_registry

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            await self._store.open()
            await self._robot_skill_store.open()
            await self._machine_binding_store.open()
            self._started = True

    async def close(self) -> None:
        async with self._start_lock:
            started = self._started
            self._started = False
        if started:
            await self._store.close()
            await self._robot_skill_store.close()
            await self._machine_binding_store.close()
        if self._environment_store is not None:
            await self._environment_store.close()
        if self._equipment_store is not None:
            await self._equipment_store.close()

    async def list_orchestrations(self, *, page: int, page_size: int) -> dict[str, Any]:
        await self.start()
        offset = (page - 1) * page_size
        total = await self._store.count_orchestrations()
        records = await self._store.list_orchestrations(
            limit=page_size,
            offset=offset,
        )
        return {
            "items": [_serialize_orchestration_summary(item) for item in records],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def create_orchestration(
        self, *, name: str, description: str
    ) -> dict[str, Any]:
        await self.start()
        orchestration_id = uuid4().hex
        record = await self._store.create_orchestration(
            orchestration_id=orchestration_id,
            name=name.strip(),
            description=description.strip(),
            prompt="",
            robot_id=None,
            knowledge_base_id=None,
            environment_id=None,
            voice_id=None,
            skill_ids=[],
            welcome_message="",
        )
        return {
            "orchestration": _serialize_created_orchestration(record),
        }

    async def get_orchestration(self, orchestration_id: str) -> OrchestrationRecord:
        await self.start()
        record = await self._store.get_orchestration(orchestration_id)
        if record is None:
            raise OrchestrationNotFoundError(orchestration_id)
        return record

    async def get_orchestration_config(self, orchestration_id: str) -> dict[str, Any]:
        record = await self.get_orchestration(orchestration_id)
        return {
            "orchestration": _serialize_orchestration_detail(record),
        }

    async def save_orchestration_config(
        self,
        orchestration_id: str,
        *,
        name: str,
        description: str,
        prompt: str,
        robot_id: str | None,
        knowledge_base_id: str | None,
        environment_id: str | None,
        voice_id: str | None,
        skill_ids: list[str],
        welcome_message: str,
    ) -> dict[str, Any]:
        await self.start()
        record = await self._store.update_orchestration(
            orchestration_id=orchestration_id,
            name=name.strip(),
            description=description.strip(),
            prompt=prompt.strip(),
            robot_id=_normalize_nullable_text(robot_id),
            knowledge_base_id=_normalize_nullable_text(knowledge_base_id),
            environment_id=_normalize_nullable_text(environment_id),
            voice_id=_normalize_nullable_text(voice_id),
            skill_ids=_normalize_skill_ids(skill_ids),
            welcome_message=welcome_message.strip(),
        )
        if record is None:
            raise OrchestrationNotFoundError(orchestration_id)
        await self._push_config_binding_updates(record)
        return await self.get_orchestration_config(record.id)

    async def delete_orchestration(self, orchestration_id: str) -> dict[str, Any]:
        await self.start()
        normalized_orchestration_id = orchestration_id.strip()
        if not normalized_orchestration_id:
            raise ValueError("orchestration_id is required")
        record = await self._store.get_orchestration(normalized_orchestration_id)
        if record is None:
            raise OrchestrationNotFoundError(normalized_orchestration_id)

        bindings = await self._machine_binding_store.list_bindings()
        related_bindings = [
            item
            for item in bindings
            if item.orchestration_id == normalized_orchestration_id
        ]
        released_machine_ids: list[str] = []
        for binding_record in related_bindings:
            released_machine_ids.append(binding_record.machine_id)
            updated_binding = await self._machine_binding_store.upsert_binding(
                machine_id=binding_record.machine_id,
                orchestration_id=None,
            )
            payload = await self._build_binding_push_payload(
                machine_id=binding_record.machine_id,
                binding_record=updated_binding,
                previous_orchestration_id=normalized_orchestration_id,
            )
            await self._push_binding_payload(binding_record.machine_id, payload)

        deleted = await self._store.delete_orchestration(normalized_orchestration_id)
        if not deleted:
            raise OrchestrationNotFoundError(normalized_orchestration_id)
        return {
            "orchestration_id": normalized_orchestration_id,
            "released_machine_ids": released_machine_ids,
        }

    async def list_robots(self) -> list[dict[str, Any]]:
        await self.start()
        robots = await self._list_robot_options()
        bindings = await self._machine_binding_store.list_bindings()
        orchestration_count = await self._store.count_orchestrations()
        records = (
            await self._store.list_orchestrations(
                limit=max(orchestration_count, 1),
                offset=0,
            )
            if orchestration_count
            else []
        )
        return _annotate_robot_occupancy(
            robots,
            bindings=bindings,
            orchestrations=records,
        )

    async def list_robot_skills(self, robot_id: str) -> list[dict[str, Any]]:
        normalized_robot_id = robot_id.strip()
        if not normalized_robot_id:
            raise ValueError("robot_id is required")
        robots = await self._list_robot_options()
        if not any(
            str(item.get("id") or "").strip() == normalized_robot_id for item in robots
        ):
            raise RobotNotFoundError(normalized_robot_id)
        return await self._list_robot_skill_options(normalized_robot_id)

    async def list_machines(self) -> list[dict[str, Any]]:
        await self.start()
        machine_items = (
            await self._machine_directory.list_machines()
            if self._machine_directory is not None
            else []
        )
        bindings = await self._machine_binding_store.list_bindings()
        binding_by_machine_id = {item.machine_id: item for item in bindings}
        online_machine_ids = set(await self._connection_registry.list_machine_ids())

        results: list[dict[str, Any]] = []
        for machine in machine_items:
            results.append(
                self._serialize_machine_snapshot(
                    machine.machine_id,
                    machine=machine,
                    binding_record=binding_by_machine_id.get(machine.machine_id),
                    is_online=(machine.machine_id in online_machine_ids)
                    or machine.is_online,
                )
            )
        return results

    async def get_machine_binding(self, machine_id: str) -> dict[str, Any]:
        await self.start()
        normalized_machine_id = machine_id.strip()
        if not normalized_machine_id:
            raise ValueError("machine_id is required")
        binding_record = await self._machine_binding_store.get_binding(
            normalized_machine_id
        )
        machine = await self._get_machine_record(normalized_machine_id)
        if machine is None:
            raise MachineNotFoundError(normalized_machine_id)
        is_online = await self._connection_registry.is_online(normalized_machine_id)
        is_online = is_online or machine.is_online
        return self._serialize_machine_snapshot(
            normalized_machine_id,
            machine=machine,
            binding_record=binding_record,
            is_online=is_online,
        )

    async def save_machine_binding(
        self,
        machine_id: str,
        *,
        orchestration_id: str | None,
    ) -> dict[str, Any]:
        await self.start()
        normalized_machine_id = machine_id.strip()
        if not normalized_machine_id:
            raise ValueError("machine_id is required")
        machine = await self._get_machine_record(normalized_machine_id)
        if machine is None:
            raise MachineNotFoundError(normalized_machine_id)

        normalized_orchestration_id = _normalize_nullable_text(orchestration_id)
        current_record = await self._machine_binding_store.get_binding(
            normalized_machine_id
        )
        previous_orchestration_id = (
            current_record.orchestration_id
            if current_record is not None
            else self._resolve_legacy_binding_orchestration_id(normalized_machine_id)
        )
        if (
            current_record is not None
            and current_record.orchestration_id == normalized_orchestration_id
        ):
            machine = await self._get_machine_record(normalized_machine_id)
            is_online = await self._connection_registry.is_online(normalized_machine_id)
            if machine is not None:
                is_online = is_online or machine.is_online
            result = self._serialize_machine_snapshot(
                normalized_machine_id,
                machine=machine,
                binding_record=current_record,
                is_online=is_online,
            )
            result["applied_online"] = False
            return result

        if normalized_orchestration_id is not None:
            record = await self._store.get_orchestration(normalized_orchestration_id)
            if record is None:
                raise OrchestrationNotFoundError(normalized_orchestration_id)

        binding_record = await self._machine_binding_store.upsert_binding(
            machine_id=normalized_machine_id,
            orchestration_id=normalized_orchestration_id,
        )
        payload = await self._build_binding_push_payload(
            machine_id=normalized_machine_id,
            binding_record=binding_record,
            previous_orchestration_id=previous_orchestration_id,
        )
        applied_online = await self._push_binding_payload(
            normalized_machine_id, payload
        )
        is_online = await self._connection_registry.is_online(normalized_machine_id)
        is_online = is_online or machine.is_online
        result = self._serialize_machine_snapshot(
            normalized_machine_id,
            machine=machine,
            binding_record=binding_record,
            is_online=is_online,
        )
        result["applied_online"] = applied_online
        return result

    async def register_machine_connection(
        self, machine_id: str, connection: BindingConnection
    ) -> None:
        await self._connection_registry.register(machine_id, connection)

    async def unregister_machine_connection(
        self, machine_id: str, connection: BindingConnection
    ) -> None:
        await self._connection_registry.unregister(machine_id, connection)

    async def resolve_device_binding(
        self,
        *,
        machine_id: str,
        binding_version: int = 0,
        capabilities: object = None,
    ) -> DeviceBinding | None:
        """根据设备 ID 解析当前编排绑定。"""
        await self.start()
        normalized_machine_id = machine_id.strip()
        if not normalized_machine_id:
            return None

        binding_record = await self._machine_binding_store.get_binding(
            normalized_machine_id
        )
        configured_orchestration_id = None
        effective_binding_version = binding_version
        if binding_record is not None:
            effective_binding_version = binding_record.binding_version
            configured_orchestration_id = binding_record.orchestration_id
        else:
            configured_orchestration_id = self._resolve_legacy_binding_orchestration_id(
                normalized_machine_id
            )
        if configured_orchestration_id is None:
            return None

        record = await self._store.get_orchestration(configured_orchestration_id)
        if record is None:
            return None
        return await self._build_device_binding(
            machine_id=normalized_machine_id,
            record=record,
            binding_version=effective_binding_version,
            capabilities=capabilities,
        )

    async def sync_robot_skills(
        self,
        *,
        robot_id: str,
        capabilities: object,
    ) -> None:
        """用边端完整 capability 快照覆盖当前机器人的技能定义。"""
        normalized_robot_id = robot_id.strip()
        if not normalized_robot_id:
            return
        skills = _extract_robot_skills_from_capabilities(
            robot_id=normalized_robot_id,
            capabilities=capabilities,
        )
        if not skills:
            logger.warning(
                "收到空的边端技能快照，跳过覆盖 | robot_id={}",
                normalized_robot_id,
            )
            return
        await self.start()
        await self._robot_skill_store.replace_robot_skills(
            robot_id=normalized_robot_id,
            skills=skills,
        )

    async def list_voices(self) -> list[dict[str, Any]]:
        return deepcopy(list(DEFAULT_VOICE_OPTIONS))

    async def preview_voice(
        self,
        *,
        voice_id: str,
        text: str,
    ) -> bytes:
        normalized_voice_id = (
            _normalize_nullable_text(voice_id) or self._default_voice_id()
        )
        sample_text = _normalize_nullable_text(text)
        if sample_text is None:
            raise ValueError("text is required")
        audio_bytes = await self._render_audio(
            sample_text, voice_id=normalized_voice_id
        )
        if audio_bytes is None:
            raise RuntimeError("voice preview unavailable")
        return audio_bytes

    async def chat(
        self,
        orchestration_id: str,
        *,
        question: str,
        response_mode: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        context = await self._prepare_chat_context(
            orchestration_id,
            question=question,
            response_mode=response_mode,
            session_id=session_id,
        )

        llm_reply = await self._build_llm_reply(
            context=context,
        )
        answer_text = llm_reply.answer

        audio_bytes = None
        if context.response_mode == "audio":
            audio_bytes = await self._render_audio(
                answer_text,
                voice_id=context.record.voice_id or "",
            )
        await self._record_turn(context, answer_text=answer_text)
        return self._build_chat_response(
            context,
            answer_text=answer_text,
            actions=list(llm_reply.actions),
            audio_bytes=audio_bytes,
        )

    async def stream_chat(
        self,
        orchestration_id: str,
        *,
        question: str,
        response_mode: str,
        session_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        context = await self._prepare_chat_context(
            orchestration_id,
            question=question,
            response_mode=response_mode,
            session_id=session_id,
        )
        yield_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        await yield_queue.put(
            {
                "event": "session",
                "data": {
                    "orchestration_id": context.orchestration_id,
                    "session_id": context.session_id,
                    "machine_id": context.machine_id,
                    "response_mode": context.response_mode,
                },
            }
        )

        result: dict[str, Any] = {"answer_text": "", "error": None}

        async def _emit(event_payload: dict[str, Any]) -> None:
            await yield_queue.put(event_payload)

        async def _produce() -> None:
            try:
                answer_text = await self._stream_answer_text(context, emit_event=_emit)
                audio_bytes = None
                if context.response_mode == "audio":
                    audio_bytes = await self._render_audio(
                        answer_text,
                        voice_id=context.record.voice_id or "",
                    )
                await self._record_turn(context, answer_text=answer_text)
                result["answer_text"] = answer_text
                await yield_queue.put(
                    {
                        "event": "done",
                        "data": self._build_chat_response(
                            context,
                            answer_text=answer_text,
                            actions=[],
                            audio_bytes=audio_bytes,
                        ),
                    }
                )
            except Exception as exc:
                result["error"] = exc
                await yield_queue.put(
                    {
                        "event": "error",
                        "data": {"message": str(exc) or "conversation stream failed"},
                    }
                )
            finally:
                await yield_queue.put(None)

        producer_task = asyncio.create_task(_produce())
        try:
            while True:
                item = await yield_queue.get()
                if item is None:
                    break
                yield item
        finally:
            await producer_task

    async def _list_robot_options(self) -> list[dict[str, Any]]:
        if self._machine_directory is None:
            return deepcopy(list(DEFAULT_ROBOT_OPTIONS))
        machines = await self._machine_directory.list_machines()
        if not machines:
            return []
        return [_serialize_robot_option(item) for item in machines]

    async def _list_payload_robot_options(self) -> list[dict[str, Any]]:
        if self._equipment_store is not None:
            try:
                async with self._reference_store_lock:
                    await self._equipment_store.open()
                    records = await self._equipment_store.list_equipment()
                return [_serialize_robot_option_from_db(item) for item in records]
            except Exception as exc:
                logger.warning("从 equipment_store 获取设备列表失败，回退到 HTTP | err={}", exc)
        return await self._list_robot_options()

    async def _list_knowledge_base_options(self) -> list[dict[str, Any]]:
        if self._knowledge_base_service is None:
            return []

        try:
            payload = await self._knowledge_base_service.list_knowledge_bases(
                page=1,
                page_size=1000,
            )
        except Exception:
            return []

        return _extract_knowledge_base_options(payload)

    async def _build_llm_reply(self, *, context: _ResolvedChatContext) -> LlmReply:
        if self._llm_generator is None:
            raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE)

        extra_payload = _build_llm_extra_payload(context)
        generate_structured_reply = getattr(
            self._llm_generator,
            "generate_structured_reply",
            None,
        )
        if callable(generate_structured_reply):
            reply = await asyncio.to_thread(
                generate_structured_reply,
                context.question,
                conversation_id=context.session_id,
                extra_payload=extra_payload,
            )
            if isinstance(reply, LlmReply):
                return reply
            if isinstance(reply, dict):
                answer = str(reply.get("answer") or "").strip()
                actions_obj = reply.get("actions")
                actions = (
                    tuple(item for item in actions_obj if isinstance(item, str))
                    if isinstance(actions_obj, list)
                    else ()
                )
                return LlmReply(answer=answer, actions=actions)

        answer = await asyncio.to_thread(
            self._llm_generator.generate_reply,
            context.question,
            conversation_id=context.session_id,
            extra_payload=extra_payload,
        )
        return LlmReply(answer=str(answer or "").strip())

    async def build_llm_extra_payload(
        self,
        orchestration_id: str,
        *,
        robot_id: str | None = None,
    ) -> dict[str, object]:
        """构建发给大模型的额外 payload（环境/机器人/Skill 等）。

        抽出来供 HTTP 对话接口与 WebSocket 语音链路共用，确保两条路径
        发出的模型请求结构完全一致。
        """
        record, options = await self._resolve_payload_context(
            orchestration_id, robot_id
        )
        context = _ResolvedChatContext(
            orchestration_id=orchestration_id,
            question="",
            response_mode="",
            session_id="",
            machine_id=f"orchestration:{orchestration_id}",
            record=record,
            knowledge_base=_find_option(
                options["knowledge_bases"], record.knowledge_base_id
            ),
            robot=_find_option(options["robots"], record.robot_id),
            environment=_find_option(options["environments"], record.environment_id),
            voice=_find_option(options["voices"], record.voice_id),
            skills=_find_options(options["skills"], list(record.skill_ids)),
        )
        return _build_llm_extra_payload(context)

    async def _resolve_payload_context(
        self,
        orchestration_id: str,
        robot_id: str | None = None,
    ) -> tuple[OrchestrationRecord, dict[str, list[dict[str, Any]]]]:
        """拉取编排记录与参考选项，供 payload 构建与对话上下文共用。"""
        record = await self.get_orchestration(orchestration_id)
        effective_robot_id = (
            robot_id if _normalize_nullable_text(robot_id) is not None else record.robot_id
        )
        options = await self._build_reference_options(effective_robot_id)
        return record, options

    async def _prepare_chat_context(
        self,
        orchestration_id: str,
        *,
        question: str,
        response_mode: str,
        session_id: str | None,
    ) -> _ResolvedChatContext:
        normalized_question = question.strip()
        if not normalized_question:
            raise ValueError("question is required")

        record, options = await self._resolve_payload_context(orchestration_id)
        resolved_session_id = (session_id or "").strip() or uuid4().hex
        return _ResolvedChatContext(
            orchestration_id=orchestration_id,
            question=normalized_question,
            response_mode=response_mode,
            session_id=resolved_session_id,
            machine_id=f"orchestration:{orchestration_id}",
            record=record,
            knowledge_base=_find_option(
                options["knowledge_bases"], record.knowledge_base_id
            ),
            robot=_find_option(options["robots"], record.robot_id),
            environment=_find_option(options["environments"], record.environment_id),
            voice=_find_option(options["voices"], record.voice_id),
            skills=_find_options(options["skills"], list(record.skill_ids)),
        )

    async def _build_reference_options(
        self,
        robot_id: str | None,
    ) -> dict[str, list[dict[str, Any]]]:
        robots = await self._list_payload_robot_options()
        knowledge_bases = await self._list_knowledge_base_options()
        environments = await self._list_environment_options()
        return {
            "robots": robots,
            "knowledge_bases": knowledge_bases,
            "environments": environments,
            "voices": deepcopy(list(DEFAULT_VOICE_OPTIONS)),
            "skills": await self._list_robot_skill_options(robot_id),
        }

    async def _list_environment_options(self) -> list[dict[str, Any]]:
        if self._environment_store is None:
            return deepcopy(list(DEFAULT_ENVIRONMENT_OPTIONS))
        try:
            async with self._reference_store_lock:
                await self._environment_store.open()
                records = await self._environment_store.list_environments()
            return [
                {
                    "id": record.id,
                    "name": record.name,
                    "description": record.description,
                }
                for record in records
            ]
        except Exception as exc:
            logger.warning("从 environment_store 获取环境列表失败，回退到默认值 | err={}", exc)
            return deepcopy(list(DEFAULT_ENVIRONMENT_OPTIONS))

    async def _list_robot_skill_options(
        self,
        robot_id: str | None,
    ) -> list[dict[str, Any]]:
        normalized_robot_id = _normalize_nullable_text(robot_id)
        if normalized_robot_id is None:
            return []
        await self.start()
        records = await self._robot_skill_store.list_robot_skills(normalized_robot_id)
        return [_robot_skill_record_to_option(item) for item in records]

    async def _resolve_allowed_tools(
        self,
        *,
        robot_id: str | None,
        skill_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized_robot_id = _normalize_nullable_text(robot_id)
        if normalized_robot_id is None or not skill_ids:
            return ()
        records = await self._robot_skill_store.list_robot_skills(normalized_robot_id)
        tool_names: list[str] = []
        tool_name_by_skill_id = {item.skill_id: item.tool_name for item in records}
        for skill_id in skill_ids:
            tool_name = _normalize_nullable_text(tool_name_by_skill_id.get(skill_id))
            if tool_name is None or tool_name in tool_names:
                continue
            tool_names.append(tool_name)
        return tuple(tool_names)

    async def _build_device_binding(
        self,
        *,
        machine_id: str,
        record: OrchestrationRecord,
        binding_version: int,
        capabilities: object = None,
    ) -> DeviceBinding:
        if record.robot_id is not None:
            await self.sync_robot_skills(
                robot_id=record.robot_id,
                capabilities=capabilities,
            )
        return DeviceBinding(
            machine_id=machine_id,
            orchestration_id=record.id,
            binding_version=binding_version,
            robot_id=record.robot_id,
            environment_id=record.environment_id,
            voice_id=record.voice_id,
            allowed_tools=await self._resolve_allowed_tools(
                robot_id=record.robot_id,
                skill_ids=record.skill_ids,
            ),
            welcome_message=record.welcome_message,
        )

    async def _build_binding_push_payload(
        self,
        *,
        machine_id: str,
        binding_record: MachineBindingRecord,
        previous_orchestration_id: str | None,
    ) -> dict[str, object]:
        if binding_record.orchestration_id is None:
            return {
                "type": ServerMessageType.DEVICE_UNBOUND.value,
                "machine_id": machine_id,
                "binding_version": binding_record.binding_version,
            }
        record = await self._store.get_orchestration(binding_record.orchestration_id)
        if record is None:
            return {
                "type": ServerMessageType.DEVICE_UNBOUND.value,
                "machine_id": machine_id,
                "binding_version": binding_record.binding_version,
            }
        binding = await self._build_device_binding(
            machine_id=machine_id,
            record=record,
            binding_version=binding_record.binding_version,
        )
        message_type = (
            ServerMessageType.DEVICE_BOUND.value
            if previous_orchestration_id is None
            else ServerMessageType.BINDING_CHANGED.value
        )
        return {
            "type": message_type,
            "machine_id": binding.machine_id,
            "orchestration_id": binding.orchestration_id,
            "binding_version": binding.binding_version,
            "robot_id": binding.robot_id,
            "environment_id": binding.environment_id,
            "voice_id": binding.voice_id,
            "allowed_tools": list(binding.allowed_tools),
            "welcome_message": binding.welcome_message,
        }

    async def _push_config_binding_updates(self, record: OrchestrationRecord) -> None:
        bindings = await self._machine_binding_store.list_bindings()
        for binding_record in bindings:
            if binding_record.orchestration_id != record.id:
                continue
            payload = await self._build_binding_push_payload(
                machine_id=binding_record.machine_id,
                binding_record=binding_record,
                previous_orchestration_id=record.id,
            )
            await self._push_binding_payload(binding_record.machine_id, payload)

    async def _push_binding_payload(
        self,
        machine_id: str,
        payload: dict[str, object],
    ) -> bool:
        try:
            return await self._connection_registry.push(machine_id, payload)
        except Exception:
            logger.exception("在线设备绑定推送失败 | machine_id={}", machine_id)
            return False

    async def _get_machine_record(self, machine_id: str) -> MachineRecord | None:
        if self._machine_directory is None:
            return None
        return await self._machine_directory.get_machine(machine_id)

    def _serialize_machine_snapshot(
        self,
        machine_id: str,
        *,
        machine: MachineRecord | None,
        binding_record: MachineBindingRecord | None,
        is_online: bool,
    ) -> dict[str, Any]:
        return {
            "machine_id": machine_id,
            "name": machine.name if machine is not None else "",
            "description": machine.description if machine is not None else "",
            "type_name": machine.type_name if machine is not None else None,
            "is_active": machine.is_active if machine is not None else False,
            "is_online": is_online,
            "binding_orchestration_id": self._resolve_binding_orchestration_id(
                machine_id,
                binding_record=binding_record,
            ),
        }

    def _resolve_binding_orchestration_id(
        self,
        machine_id: str,
        *,
        binding_record: MachineBindingRecord | None,
    ) -> str | None:
        if binding_record is not None:
            return binding_record.orchestration_id

        return self._resolve_legacy_binding_orchestration_id(machine_id)

    def _resolve_legacy_binding_orchestration_id(self, machine_id: str) -> str | None:
        binding_config = _as_dict(self._config.get("device_binding"))
        machines = _as_dict(binding_config.get("machines"))
        return _normalize_nullable_text(
            machines.get(machine_id)
        ) or _normalize_nullable_text(binding_config.get("default_orchestration_id"))

    async def _stream_answer_text(
        self,
        context: _ResolvedChatContext,
        *,
        emit_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> str:
        if self._llm_generator is None:
            raise LlmServiceUnavailableError(LLM_SERVICE_UNAVAILABLE_MESSAGE)
        llm_generator = self._llm_generator

        extra_payload = _build_llm_extra_payload(context)
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        result: dict[str, Any] = {"answer_text": "", "error": None}
        loop = asyncio.get_running_loop()

        def _push(item: dict[str, Any] | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def _on_chunk(chunk: str) -> None:
            if not chunk:
                return
            _push({"event": "delta", "data": {"text": chunk}})

        def _run_stream() -> None:
            try:
                result["answer_text"] = llm_generator.stream_reply(
                    context.question,
                    conversation_id=context.session_id,
                    on_chunk=_on_chunk,
                    extra_payload=extra_payload,
                )
            except Exception as exc:
                result["error"] = exc
            finally:
                _push(None)

        task = asyncio.create_task(asyncio.to_thread(_run_stream))
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                await emit_event(item)
        finally:
            await task

        error = result["error"]
        if error is not None:
            raise error
        answer_text = str(result["answer_text"] or "").strip()
        if answer_text:
            return answer_text
        raise RuntimeError("empty streamed answer")

    async def _record_turn(
        self,
        context: _ResolvedChatContext,
        *,
        answer_text: str,
    ) -> None:
        if self._conversation_service is None:
            return
        await self._conversation_service.record_turn(
            SessionInfo(
                session_id=context.session_id,
                machine_id=context.machine_id,
                orchestration_id=context.orchestration_id,
            ),
            user_text=context.question,
            assistant_text=answer_text,
        )

    def _build_chat_response(
        self,
        context: _ResolvedChatContext,
        *,
        answer_text: str,
        actions: list[str],
        audio_bytes: bytes | None,
    ) -> dict[str, Any]:
        response = {
            "orchestration_id": context.orchestration_id,
            "question": context.question,
            "response_mode": context.response_mode,
            "answer": answer_text,
            "actions": actions,
            "answer_text": answer_text,
            "knowledge_base": context.knowledge_base,
            "voice": context.voice,
            "session_id": context.session_id,
            "machine_id": context.machine_id,
        }
        if context.response_mode == "audio":
            response["audio"] = (
                {
                    "content_type": "audio/wav",
                    "base64": base64.b64encode(audio_bytes).decode("ascii"),
                    "bytes": len(audio_bytes),
                }
                if audio_bytes is not None
                else None
            )
        return response

    async def _render_audio(self, text: str, *, voice_id: str) -> bytes | None:
        if self._audio_renderer is not None:
            payload = await self._audio_renderer(text=text, voice_id=voice_id)
            return payload if isinstance(payload, bytes) else None

        provider_selection_config = _as_dict(self._config.get("providers"))
        tts_section = _as_dict(self._config.get("tts"))
        tts_provider_name = str(provider_selection_config.get("tts") or "").strip()
        provider_config = _as_dict(tts_section.get(tts_provider_name))
        if not tts_provider_name or not provider_config:
            return None

        driver = str(provider_config.get("driver") or "").strip()
        if not driver:
            return None

        config = deepcopy(provider_config)
        config["voice"] = voice_id or str(config.get("voice") or "").strip()

        provider = build_tts_provider(driver=driver, config=config)
        client = provider.create_stream_client()
        chunks: list[bytes] = []
        try:
            await client.start_session()
            await client.send_text(text)
            await client.finish_session()
            while True:
                payload = await client.receive_audio()
                if payload is None:
                    break
                chunks.append(payload)
        except Exception:
            return None
        finally:
            with contextlib.suppress(Exception):
                await client.close()

        pcm_bytes = b"".join(chunks)
        if not pcm_bytes:
            return None
        return _pcm_to_wav(
            pcm_bytes, sample_rate=int(getattr(client, "sample_rate", 24000))
        )

    def _default_voice_id(self) -> str:
        provider_selection_config = _as_dict(self._config.get("providers"))
        tts_section = _as_dict(self._config.get("tts"))
        tts_provider_name = str(provider_selection_config.get("tts") or "").strip()
        provider_config = _as_dict(tts_section.get(tts_provider_name))
        speaker = str(provider_config.get("speaker") or "").strip()
        return speaker or str(DEFAULT_VOICE_OPTIONS[0]["id"])


def _build_orchestration_store(config: dict[str, Any]) -> OrchestrationStore:
    database_config = _require_section(config, "database")
    dsn = str(database_config.get("dsn") or "").strip()
    if not dsn:
        raise ValueError("orchestration service requires database.dsn")
    return PostgresOrchestrationStore(
        dsn=dsn,
        min_pool_size=int(database_config.get("min_pool_size") or 1),
        max_pool_size=int(database_config.get("max_pool_size") or 5),
        command_timeout_sec=float(database_config.get("command_timeout_sec") or 10.0),
    )


def _build_robot_skill_store(config: dict[str, Any]) -> RobotSkillStore:
    database_config = _require_section(config, "database")
    dsn = str(database_config.get("dsn") or "").strip()
    if not dsn:
        raise ValueError("orchestration service requires database.dsn")
    return PostgresRobotSkillStore(
        dsn=dsn,
        min_pool_size=int(database_config.get("min_pool_size") or 1),
        max_pool_size=int(database_config.get("max_pool_size") or 5),
        command_timeout_sec=float(database_config.get("command_timeout_sec") or 10.0),
    )


def _build_machine_binding_store(config: dict[str, Any]) -> MachineBindingStore:
    database_config = _require_section(config, "database")
    dsn = str(database_config.get("dsn") or "").strip()
    if not dsn:
        raise ValueError("orchestration service requires database.dsn")
    return PostgresMachineBindingStore(
        dsn=dsn,
        min_pool_size=int(database_config.get("min_pool_size") or 1),
        max_pool_size=int(database_config.get("max_pool_size") or 5),
        command_timeout_sec=float(database_config.get("command_timeout_sec") or 10.0),
    )


def _build_environment_store(config: dict[str, Any]) -> EnvironmentStore | None:
    database_config = _as_dict(config.get("database"))
    dsn = str(database_config.get("dsn") or "").strip()
    if not dsn:
        return None
    return PostgresEnvironmentStore(
        dsn=dsn,
        min_pool_size=int(database_config.get("min_pool_size") or 1),
        max_pool_size=int(database_config.get("max_pool_size") or 5),
        command_timeout_sec=float(database_config.get("command_timeout_sec") or 10.0),
    )


def _build_equipment_store(config: dict[str, Any]) -> EquipmentStore | None:
    database_config = _as_dict(config.get("database_embodied"))
    dsn = str(database_config.get("dsn") or "").strip()
    if not dsn:
        return None
    return PostgresEquipmentStore(
        dsn=dsn,
        min_pool_size=int(database_config.get("min_pool_size") or 1),
        max_pool_size=int(database_config.get("max_pool_size") or 5),
        command_timeout_sec=float(database_config.get("command_timeout_sec") or 10.0),
    )


def _build_machine_directory_client(
    config: dict[str, Any],
) -> MachineDirectoryClient | None:
    percept_api_config = _as_dict(config.get("percept-api"))
    percept_api_endpoint = _as_optional_str(percept_api_config.get("endpoint"))
    if percept_api_endpoint is None:
        return None
    return MachineDirectoryClient(
        endpoint=f"{percept_api_endpoint.rstrip('/')}/equipment/options",
        service_key=_as_optional_str(percept_api_config.get("x-api-key")),
    )


def build_orchestration_service(
    config: dict[str, Any],
    *,
    conversation_service: ConversationService | None = None,
    knowledge_base_service: KnowledgeBaseService | None = None,
) -> OrchestrationService | None:
    database_config = config.get("database")
    if not isinstance(database_config, dict):
        return None
    dsn = str(database_config.get("dsn") or "").strip()
    if not dsn:
        return None
    return OrchestrationService(
        config,
        conversation_service=conversation_service,
        knowledge_base_service=knowledge_base_service,
    )


def _build_llm(config: dict[str, Any]) -> RemoteReplyGenerator | None:
    try:
        modules = initialize_modules(config, init_llm=True)
    except Exception as exc:
        logger.warning("初始化编排对话 LLM 失败 | err={}", exc)
        return None
    return modules.llm


def _resolve_llm_config(config: dict[str, Any]) -> dict[str, Any] | None:
    provider_selection_config = _as_dict(config.get("providers"))
    llm_name = str(provider_selection_config.get("llm") or "").strip()
    llm_section = _as_dict(config.get("llm"))
    llm_config = llm_section.get(llm_name)
    return llm_config if isinstance(llm_config, dict) else None


def _build_llm_extra_payload(context: _ResolvedChatContext) -> dict[str, object]:
    record = context.record
    knowledge_base_id = _normalize_nullable_text(record.knowledge_base_id)
    environment = context.environment
    robot = context.robot
    return {
        "kb_id": [knowledge_base_id] if knowledge_base_id is not None else [],
        "environment": None
        if environment is None
        else {
            "name": str(environment.get("name") or "").strip(),
            "description": str(environment.get("description") or "").strip(),
        },
        "robot_prompt": record.prompt,
        "robot_info": None
        if robot is None
        else {
            "name": str(robot.get("name") or "").strip(),
            "description": str(robot.get("description") or "").strip(),
        },
        "robot_skill": [
            {
                "skill_name": str(item.get("name") or "").strip(),
                "skill_name_en": str(item.get("tool_name") or "").strip(),
                "description": str(item.get("description") or "").strip(),
            }
            for item in context.skills
        ],
    }


def _serialize_orchestration_summary(record: OrchestrationRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "description": record.description,
        "updated_at": int(record.updated_at.timestamp()),
    }


def _serialize_created_orchestration(record: OrchestrationRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "description": record.description,
        "prompt": record.prompt,
        "robot_id": record.robot_id,
        "knowledge_base_id": record.knowledge_base_id,
        "environment_id": record.environment_id,
        "voice_id": record.voice_id,
        "skill_ids": list(record.skill_ids),
        "welcome_message": record.welcome_message,
        "created_at": int(record.created_at.timestamp()),
        "updated_at": int(record.updated_at.timestamp()),
    }


def _serialize_orchestration_detail(record: OrchestrationRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "description": record.description,
        "prompt": record.prompt,
        "robot_id": record.robot_id,
        "knowledge_base_id": record.knowledge_base_id,
        "environment_id": record.environment_id,
        "voice_id": record.voice_id,
        "skill_ids": list(record.skill_ids),
        "welcome_message": record.welcome_message,
        "created_at": int(record.created_at.timestamp()),
        "updated_at": int(record.updated_at.timestamp()),
    }


def _find_option(
    options: list[dict[str, Any]], option_id: object
) -> dict[str, Any] | None:
    normalized_id = _normalize_nullable_text(option_id)
    if normalized_id is None:
        return None
    for item in options:
        candidate_ids = {
            str(item.get("id") or "").strip(),
            str(item.get("uid") or "").strip(),
        }
        if normalized_id in candidate_ids:
            return deepcopy(item)
    return None


def _find_options(
    options: list[dict[str, Any]], option_ids: list[str]
) -> list[dict[str, Any]]:
    normalized_ids = {item for item in option_ids if item}
    return [
        deepcopy(item)
        for item in options
        if {
            str(item.get("id") or "").strip(),
            str(item.get("uid") or "").strip(),
        }
        & normalized_ids
    ]


def _robot_skill_record_to_option(record: RobotSkillRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": record.skill_id,
        "name": record.skill_name,
        "description": record.description,
        "tool_name": record.tool_name,
        "input_schema": deepcopy(record.input_schema),
        "supports_cancel": record.supports_cancel,
    }
    if record.output_schema is not None:
        payload["output_schema"] = deepcopy(record.output_schema)
    return payload


def _serialize_robot_option(record: MachineRecord) -> dict[str, Any]:
    return {
        "id": record.machine_id,
        "name": record.name,
        "description": record.description,
        "status": "online" if record.is_online else "offline",
    }


def _serialize_robot_option_from_db(record: Any) -> dict[str, Any]:
    return {
        "id": record.uid,
        "name": record.name,
        "description": record.desc,
        "status": "online" if record.status else "offline",
    }


def _annotate_robot_occupancy(
    robots: list[dict[str, Any]],
    *,
    bindings: list[MachineBindingRecord],
    orchestrations: list[OrchestrationRecord],
) -> list[dict[str, Any]]:
    bound_by_robot_id: dict[str, str] = {}
    for binding in bindings:
        if binding.orchestration_id is None:
            continue
        bound_by_robot_id[binding.machine_id] = binding.orchestration_id

    configured_by_robot_id: dict[str, list[str]] = {}
    for record in orchestrations:
        robot_id = _normalize_nullable_text(record.robot_id)
        if robot_id is None:
            continue
        configured_by_robot_id.setdefault(robot_id, []).append(record.id)

    annotated: list[dict[str, Any]] = []
    for item in robots:
        robot_id = str(item.get("id") or "").strip()
        binding_orchestration_id = bound_by_robot_id.get(robot_id)
        configured_orchestration_ids = configured_by_robot_id.get(robot_id, [])
        occupancy_candidates = list(configured_orchestration_ids)
        if binding_orchestration_id is not None:
            occupancy_candidates.insert(0, binding_orchestration_id)
        occupied_orchestration_ids = _dedupe_texts(occupancy_candidates)
        enriched = dict(item)
        enriched["binding_orchestration_id"] = binding_orchestration_id
        enriched["configured_orchestration_ids"] = configured_orchestration_ids
        enriched["occupied_orchestration_ids"] = occupied_orchestration_ids
        enriched["occupied"] = bool(occupied_orchestration_ids)
        annotated.append(enriched)
    return annotated


def _dedupe_texts(values: list[str]) -> list[str]:
    results: list[str] = []
    for value in values:
        normalized = _normalize_nullable_text(value)
        if normalized is None or normalized in results:
            continue
        results.append(normalized)
    return results


def _extract_robot_skills_from_capabilities(
    *,
    robot_id: str,
    capabilities: object,
) -> list[RobotSkillRecord]:
    if not isinstance(capabilities, list):
        return []

    results_by_skill_id: dict[str, RobotSkillRecord] = {}
    for item in capabilities:
        if not isinstance(item, dict):
            continue
        tool_name = _normalize_nullable_text(item.get("tool_name") or item.get("name"))
        if tool_name is None:
            continue
        skill_id = _normalize_nullable_text(item.get("skill_id")) or _fallback_skill_id(
            tool_name
        )
        skill_name = (
            _normalize_nullable_text(item.get("skill_name"))
            or _normalize_nullable_text(item.get("title"))
            or tool_name
        )
        description = str(item.get("description") or "").strip()
        input_schema = _as_dict(item.get("input_schema"))
        output_schema = item.get("output_schema")
        results_by_skill_id[skill_id] = RobotSkillRecord(
            robot_id=robot_id,
            skill_id=skill_id,
            skill_name=skill_name,
            tool_name=tool_name,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema if isinstance(output_schema, dict) else None,
            supports_cancel=bool(item.get("supports_cancel")),
        )
    return list(results_by_skill_id.values())


def _fallback_skill_id(tool_name: str) -> str:
    normalized = tool_name.strip().lower().replace("-", "_").replace(".", "_")
    return f"skill_{normalized}"


def _extract_robot_options(payload: object) -> list[dict[str, Any]]:
    candidates = _find_candidate_list(payload)
    results: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        activation = item.get("activation")
        if activation is not None and activation is not True:
            continue
        robot_id = (
            _normalize_nullable_text(item.get("id"))
            or _normalize_nullable_text(item.get("robot_id"))
            or _normalize_nullable_text(item.get("machine_id"))
            or _normalize_nullable_text(item.get("uid"))
        )
        name = (
            _normalize_nullable_text(item.get("name"))
            or _normalize_nullable_text(item.get("robot_name"))
            or _normalize_nullable_text(item.get("title"))
        )
        if not robot_id or not name:
            continue
        status = item.get("status")
        normalized_status = "ready"
        if isinstance(status, bool):
            normalized_status = "online" if status else "offline"
        elif status is not None:
            normalized_status = str(status).strip() or "ready"
        results.append(
            {
                "id": robot_id,
                "name": name,
                "description": str(
                    item.get("description")
                    or item.get("address")
                    or item.get("area")
                    or item.get("scene")
                    or ""
                ).strip(),
                "status": normalized_status,
            }
        )
    return results


def _extract_knowledge_base_options(payload: object) -> list[dict[str, Any]]:
    candidates = _find_candidate_list(payload)
    results: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        knowledge_base_uid = _normalize_nullable_text(item.get("uid"))
        name = _normalize_nullable_text(item.get("name"))
        if not knowledge_base_uid or not name:
            continue
        results.append(
            {
                "uid": knowledge_base_uid,
                "name": name,
                "description": str(
                    item.get("desc") or item.get("description") or ""
                ).strip(),
                "doc_num": item.get("doc_num", 0),
                "chunk_num": item.get("chunk_num", 0),
                "token_num": item.get("token_num", 0),
                "create_time": item.get("create_time"),
                "doc_list": item.get("doc_list")
                if isinstance(item.get("doc_list"), list)
                else [],
            }
        )
    return results


def _find_candidate_list(payload: object) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", "list", "robots", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _find_candidate_list(value)
            if nested:
                return nested
    return []


def _is_success_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return True
    code = payload.get("code")
    if code is None:
        return True
    return str(code).strip() == "200"


def _build_local_test_reply(
    *,
    record: OrchestrationRecord,
    question: str,
    knowledge_base: dict[str, Any] | None,
    robot: dict[str, Any] | None,
    environment: dict[str, Any] | None,
    voice: dict[str, Any] | None,
    skills: list[dict[str, Any]],
) -> str:
    skill_names = "、".join(
        str(item.get("name") or "").strip() for item in skills if item.get("name")
    )
    greeting = record.welcome_message.strip() or "你好，我是当前应用的测试机器人。"
    answer = [
        greeting,
        f"当前应用是“{record.name or '未命名应用'}”，你的问题是“{question}”。",
    ]
    if knowledge_base is not None:
        answer.append(
            f"我会结合“{knowledge_base.get('name') or knowledge_base.get('id')}”中的内容来组织回答。"
        )
    if robot is not None:
        answer.append(f"当前绑定机器人是“{robot.get('name') or robot.get('id')}”。")
    if environment is not None:
        answer.append(f"环境使用“{environment.get('name') or environment.get('id')}”。")
    if voice is not None:
        answer.append(f"语音输出默认使用“{voice.get('name') or voice.get('id')}”。")
    if skill_names:
        answer.append(f"已启用技能：{skill_names}。")
    if record.prompt.strip():
        answer.append(
            f"按照当前 Prompt，我会以更贴合配置的方式回复：{record.prompt.strip()}"
        )
    answer.append(
        "这是一条测试回答，用来帮助你检查当前 Prompt、知识库和机器人绑定是否符合预期。"
    )
    return " ".join(answer)


def _pcm_to_wav(payload: bytes, *, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(payload)
    return buffer.getvalue()


def _normalize_skill_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_nullable_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_optional_str(value: object) -> str | None:
    return _normalize_nullable_text(value)


def _require_section(config: dict[str, Any], section_name: str) -> dict[str, Any]:
    section = config.get(section_name)
    if not isinstance(section, dict):
        raise ValueError(f"配置段缺失或格式不正确: {section_name}")
    return section


def encode_sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


__all__ = [
    "DEFAULT_ENVIRONMENT_OPTIONS",
    "DEFAULT_ROBOT_OPTIONS",
    "DEFAULT_VOICE_OPTIONS",
    "DeviceBinding",
    "MachineNotFoundError",
    "OrchestrationNotFoundError",
    "OrchestrationService",
    "RobotNotFoundError",
    "build_orchestration_service",
    "encode_sse_event",
]
