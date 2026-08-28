"""
AuditStore — 全量决策与动作审计

- 记录所有决策与动作
- SSE 事件支持全链路追踪
- trace_id / incident_id / thread_id / event_sequence 齐全
"""

import asyncio
from typing import Dict, List, Optional, AsyncGenerator, Any
from datetime import datetime
from threading import Lock
from collections import defaultdict
from loguru import logger

from app.models.incident import (
    AuditEntry,
    SSEPayload,
    SSEEventType,
    IncidentState,
)


class AuditStore:
    """
    审计存储（内存实现）。

    功能：
    - 全量审计日志
    - SSE 事件发射
    - 按 trace_id 回放全链路
    """

    def __init__(self):
        # 审计日志（按 incident_id 分组）
        self._audit_logs: Dict[str, List[AuditEntry]] = defaultdict(list)
        # 全局序列号
        self._global_sequence: int = 0
        # SSE 订阅者（thread_id → [queue]）
        self._sse_subscribers: Dict[str, List[asyncio.Queue]] = defaultdict(list)
        self._lock = Lock()

    # ================================================================
    # 审计记录
    # ================================================================

    def record(
        self,
        trace_id: str,
        incident_id: str,
        thread_id: str,
        event_type: SSEEventType,
        actor: str = "",
        action: str = "",
        detail: Optional[Dict[str, Any]] = None,
        state_from: Optional[IncidentState] = None,
        state_to: Optional[IncidentState] = None,
        message: str = "",
    ) -> AuditEntry:
        """
        记录一条审计日志，同时发射 SSE 事件。

        Args:
            trace_id: 追踪 ID
            incident_id: 事件 ID
            thread_id: LangGraph thread ID
            event_type: SSE 事件类型
            actor: 执行者（agent/user/system）
            action: 动作描述
            detail: 详细信息
            state_from: 原状态
            state_to: 新状态
            message: 消息

        Returns:
            AuditEntry
        """
        with self._lock:
            self._global_sequence += 1
            sequence = self._global_sequence

        entry = AuditEntry(
            trace_id=trace_id,
            incident_id=incident_id,
            thread_id=thread_id,
            event_sequence=sequence,
            event_type=event_type,
            timestamp=datetime.utcnow(),
            actor=actor,
            action=action,
            detail=detail or {},
            state_from=state_from,
            state_to=state_to,
        )

        self._audit_logs[incident_id].append(entry)

        # 创建 SSE Payload（包含当前状态）
        sse_payload = SSEPayload(
            trace_id=trace_id,
            incident_id=incident_id,
            thread_id=thread_id,
            event_sequence=sequence,
            event_type=event_type,
            timestamp=entry.timestamp,
            state=state_to or state_from,  # 当前业务状态
            data=detail or {},
            message=message or f"[{event_type.value}] {action}",
        )

        # 异步发射 SSE（安全：仅在运行中的 event loop 可用时发射）
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._emit_sse(thread_id, sse_payload))
            )
        except RuntimeError:
            # 无运行中的 event loop（如同步测试），跳过 SSE 发射
            pass

        logger.info(
            f"[AuditStore] #{sequence} {event_type.value} "
            f"incident={incident_id} thread={thread_id} "
            f"actor={actor} action={action}"
        )
        return entry

    async def _emit_sse(self, thread_id: str, payload: SSEPayload) -> None:
        """向所有订阅者发射 SSE 事件"""
        subscribers = self._sse_subscribers.get(thread_id, [])
        sse_dict = payload.to_sse_dict()
        for queue in subscribers:
            try:
                await queue.put(sse_dict)
            except Exception:
                pass  # 订阅者已断开

    # ================================================================
    # SSE 订阅
    # ================================================================

    async def subscribe(self, thread_id: str) -> asyncio.Queue:
        """
        订阅某个 thread 的 SSE 事件流。

        Args:
            thread_id: LangGraph thread ID

        Returns:
            asyncio.Queue（用于消费 SSE 事件）
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._sse_subscribers[thread_id].append(queue)
        logger.info(f"[AuditStore] SSE 订阅: thread_id={thread_id}")
        return queue

    def unsubscribe(self, thread_id: str, queue: asyncio.Queue) -> None:
        """取消 SSE 订阅"""
        subscribers = self._sse_subscribers.get(thread_id, [])
        if queue in subscribers:
            subscribers.remove(queue)
        if not subscribers:
            self._sse_subscribers.pop(thread_id, None)
        logger.info(f"[AuditStore] SSE 取消订阅: thread_id={thread_id}")

    async def subscribe_generator(
        self,
        thread_id: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        SSE 事件生成器（用于 FastAPI EventSourceResponse）。

        Args:
            thread_id: LangGraph thread ID

        Yields:
            SSE 事件字典
        """
        queue = await self.subscribe(thread_id)
        try:
            while True:
                event = await queue.get()
                yield event
        except asyncio.CancelledError:
            pass
        finally:
            self.unsubscribe(thread_id, queue)

    # ================================================================
    # 查询 / 回放
    # ================================================================

    def get_by_incident(self, incident_id: str) -> List[AuditEntry]:
        """按 incident_id 获取审计日志"""
        return self._audit_logs.get(incident_id, [])

    def get_by_trace(self, trace_id: str) -> List[AuditEntry]:
        """按 trace_id 获取全链路审计日志"""
        entries = []
        for logs in self._audit_logs.values():
            for entry in logs:
                if entry.trace_id == trace_id:
                    entries.append(entry)
        entries.sort(key=lambda e: e.event_sequence)
        return entries

    def get_by_thread(self, thread_id: str) -> List[AuditEntry]:
        """按 thread_id 获取审计日志"""
        entries = []
        for logs in self._audit_logs.values():
            for entry in logs:
                if entry.thread_id == thread_id:
                    entries.append(entry)
        entries.sort(key=lambda e: e.event_sequence)
        return entries

    def replay_timeline(self, incident_id: str) -> List[Dict[str, Any]]:
        """
        回放事件的完整时间线。

        Returns:
            按 event_sequence 排序的事件列表
        """
        entries = self.get_by_incident(incident_id)
        return [
            {
                "sequence": e.event_sequence,
                "type": e.event_type.value,
                "timestamp": e.timestamp.isoformat(),
                "actor": e.actor,
                "action": e.action,
                "detail": e.detail,
                "state_from": e.state_from.value if e.state_from else None,
                "state_to": e.state_to.value if e.state_to else None,
            }
            for e in entries
        ]

    # ================================================================
    # 管理
    # ================================================================

    def clear(self, incident_id: Optional[str] = None) -> None:
        """清除审计日志"""
        if incident_id:
            self._audit_logs.pop(incident_id, None)
        else:
            self._audit_logs.clear()

    @property
    def total_entries(self) -> int:
        return sum(len(logs) for logs in self._audit_logs.values())


# 全局单例
audit_store = AuditStore()


def _wire_state_machine_audit():
    """将 AuditStore.record 注册为 StateMachine 的审计回调"""
    from app.core.state_machine import state_machine as sm
    sm.set_audit_callback(audit_store.record)


# 模块加载时自动接线
_wire_state_machine_audit()
