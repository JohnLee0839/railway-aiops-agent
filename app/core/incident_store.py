"""
IncidentStore — 事件存储

- 同步记录 incident_id 与 thread_id
- 两者之间必须有映射关系
- 与 LangGraph checkpoint 状态对齐
"""

from typing import Dict, Optional, List
from datetime import datetime
from threading import Lock
from loguru import logger

from app.models.incident import IncidentRecord, IncidentState, Incident


class IncidentStore:
    """
    事件存储（内存实现）。

    维护 incident_id ↔ thread_id 映射，
    确保业务状态与 LangGraph checkpoint 对齐。
    """

    def __init__(self):
        # incident_id → IncidentRecord
        self._incidents: Dict[str, IncidentRecord] = {}
        # thread_id → incident_id（反向映射）
        self._thread_map: Dict[str, str] = {}
        self._lock = Lock()

    # ================================================================
    # CRUD
    # ================================================================

    def create(self, incident: Incident, thread_id: str) -> IncidentRecord:
        """
        创建事件记录。

        Args:
            incident: Incident 对象
            thread_id: LangGraph thread_id

        Returns:
            IncidentRecord
        """
        with self._lock:
            record = IncidentRecord(
                incident_id=incident.incident_id,
                thread_id=thread_id,
                state=IncidentState.NEW,
                incident=incident,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            self._incidents[incident.incident_id] = record
            self._thread_map[thread_id] = incident.incident_id
            logger.info(
                f"[IncidentStore] 创建: incident_id={incident.incident_id}, "
                f"thread_id={thread_id}"
            )
            return record

    def get(self, incident_id: str) -> Optional[IncidentRecord]:
        """通过 incident_id 获取记录"""
        return self._incidents.get(incident_id)

    def get_by_thread(self, thread_id: str) -> Optional[IncidentRecord]:
        """通过 thread_id 获取记录"""
        incident_id = self._thread_map.get(thread_id)
        if incident_id:
            return self._incidents.get(incident_id)
        return None

    def update(self, record: IncidentRecord) -> IncidentRecord:
        """更新记录"""
        with self._lock:
            record.updated_at = datetime.utcnow()
            self._incidents[record.incident_id] = record
            self._thread_map[record.thread_id] = record.incident_id
            return record

    def delete(self, incident_id: str) -> None:
        """删除记录"""
        with self._lock:
            record = self._incidents.pop(incident_id, None)
            if record:
                self._thread_map.pop(record.thread_id, None)
                logger.info(f"[IncidentStore] 删除: incident_id={incident_id}")

    # ================================================================
    # 查询
    # ================================================================

    def list_all(self) -> List[IncidentRecord]:
        """列出所有事件"""
        return list(self._incidents.values())

    def list_by_state(self, state: IncidentState) -> List[IncidentRecord]:
        """按状态列出事件"""
        return [r for r in self._incidents.values() if r.state == state]

    def list_active(self) -> List[IncidentRecord]:
        """列出活跃事件（非终态）"""
        terminal = {IncidentState.RESOLVED, IncidentState.FAILED, IncidentState.ESCALATED}
        return [r for r in self._incidents.values() if r.state not in terminal]

    def count_by_state(self) -> Dict[str, int]:
        """按状态统计"""
        counts: Dict[str, int] = {}
        for r in self._incidents.values():
            key = r.state.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    # ================================================================
    # 状态对齐
    # ================================================================

    def sync_state(
        self,
        incident_id: str,
        new_state: IncidentState,
    ) -> Optional[IncidentRecord]:
        """
        同步业务状态（通常在 LangGraph checkpoint 更新后调用）。

        Args:
            incident_id: 事件 ID
            new_state: 新状态

        Returns:
            更新后的记录或 None
        """
        record = self._incidents.get(incident_id)
        if record:
            record.state = new_state
            record.updated_at = datetime.utcnow()
            logger.info(
                f"[IncidentStore] 状态同步: {incident_id} → {new_state.value}"
            )
            return record
        return None

    def get_thread_id(self, incident_id: str) -> Optional[str]:
        """获取关联的 thread_id"""
        record = self._incidents.get(incident_id)
        return record.thread_id if record else None

    def get_incident_id(self, thread_id: str) -> Optional[str]:
        """获取关联的 incident_id"""
        return self._thread_map.get(thread_id)


# 全局单例
incident_store = IncidentStore()
