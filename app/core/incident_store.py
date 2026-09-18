"""Durable facade for Incident context backed by PostgreSQL."""

from __future__ import annotations

from typing import Dict, List, Optional

from app.config import config
from app.models.incident import (
    ActionJournalRecord,
    ExecutionResult,
    Incident,
    IncidentRecord,
    IncidentState,
    MockActionResult,
    RecoveryCandidate,
    RecoveryLease,
    WorkflowCursor,
)
from app.repositories.incident_repository import IncidentRepository


class IncidentStore:
    """Compatibility facade whose source of truth is PostgreSQL, never a dict."""

    def __init__(self, database_url: Optional[str] = None) -> None:
        self._database_url = database_url
        self._repository: Optional[IncidentRepository] = None

    @property
    def repository(self) -> IncidentRepository:
        if self._repository is None:
            database_url = self._database_url or config.database_url
            if not database_url:
                raise RuntimeError(
                    "DATABASE_URL is required for the durable IncidentStore and must point to PostgreSQL"
                )
            self._repository = IncidentRepository(database_url)
        return self._repository

    def create(self, incident: Incident, thread_id: str) -> IncidentRecord:
        return self.repository.create(incident, thread_id)

    def get(self, incident_id: str) -> Optional[IncidentRecord]:
        return self.repository.get(incident_id)

    def get_by_thread(self, thread_id: str) -> Optional[IncidentRecord]:
        return self.repository.get_by_thread(thread_id)

    def update(self, record: IncidentRecord) -> IncidentRecord:
        return self.repository.update(record)

    def delete(self, incident_id: str) -> None:
        self.repository.delete(incident_id)

    def list_all(self) -> List[IncidentRecord]:
        return self.repository.list_all()

    def list_by_state(self, state: IncidentState) -> List[IncidentRecord]:
        return self.repository.list_by_state(state)

    def list_active(self) -> List[IncidentRecord]:
        terminal = {IncidentState.RESOLVED, IncidentState.FAILED, IncidentState.ESCALATED}
        return [record for record in self.list_all() if record.state not in terminal]

    def count_by_state(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for record in self.list_all():
            counts[record.state.value] = counts.get(record.state.value, 0) + 1
        return counts

    def sync_state(self, incident_id: str, new_state: IncidentState) -> Optional[IncidentRecord]:
        record = self.get(incident_id)
        if record is None:
            return None
        record.state = new_state
        return self.update(record)

    def get_thread_id(self, incident_id: str) -> Optional[str]:
        record = self.get(incident_id)
        return record.thread_id if record else None

    def get_incident_id(self, thread_id: str) -> Optional[str]:
        record = self.get_by_thread(thread_id)
        return record.incident_id if record else None

    def start_action_journal(self, **kwargs) -> ActionJournalRecord:
        return self.repository.start_action_journal(**kwargs)

    def start_action_attempt(self, action_id: str, attempt_no: int, request_metadata: Optional[dict] = None):
        return self.repository.start_action_attempt(action_id, attempt_no, request_metadata)

    def finish_action_attempt(
        self,
        action_id: str,
        attempt_no: int,
        result: ExecutionResult,
        response_metadata: Optional[dict] = None,
    ):
        return self.repository.finish_action_attempt(action_id, attempt_no, result, response_metadata)

    def finish_action_journal(
        self,
        action_id: str,
        result: MockActionResult,
        response_metadata: Optional[dict] = None,
    ) -> ActionJournalRecord:
        return self.repository.finish_action_journal(action_id, result, response_metadata)

    def get_action_journal(self, action_id: str) -> Optional[ActionJournalRecord]:
        return self.repository.get_action_journal(action_id)

    def list_recovery_candidates(self) -> List[RecoveryCandidate]:
        return self.repository.list_recovery_candidates()

    def get_workflow_cursor(self, incident_id: str) -> Optional[WorkflowCursor]:
        return self.repository.get_workflow_cursor(incident_id)

    def list_resumable_workflow_cursors(self) -> List[WorkflowCursor]:
        return self.repository.list_resumable_workflow_cursors()

    def block_workflow_cursor(self, incident_id: str) -> Optional[WorkflowCursor]:
        return self.repository.block_workflow_cursor(incident_id)

    def acquire_recovery_lease(
        self, action_id: str, owner_id: str, lease_seconds: int
    ) -> Optional[RecoveryLease]:
        return self.repository.acquire_recovery_lease(action_id, owner_id, lease_seconds)

    def release_recovery_lease(self, action_id: str, owner_id: str, lease_token: str) -> bool:
        return self.repository.release_recovery_lease(action_id, owner_id, lease_token)

    def complete_recovered_action(
        self,
        action_id: str,
        result: MockActionResult,
        recovery_metadata: Optional[dict] = None,
        owner_id: Optional[str] = None,
        lease_token: Optional[str] = None,
    ) -> ActionJournalRecord:
        return self.repository.complete_recovered_action(
            action_id,
            result,
            recovery_metadata,
            owner_id,
            lease_token,
        )

    def list_action_attempts(self, action_id: str):
        return self.repository.list_action_attempts(action_id)


incident_store = IncidentStore()
