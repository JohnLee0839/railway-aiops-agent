"""PostgreSQL persistence for Incident business context and state history."""

from __future__ import annotations

import uuid
from datetime import datetime
from datetime import timedelta
from typing import Iterable, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.models.incident import (
    ActionAttemptRecord,
    ActionJournalRecord,
    ActionJournalStatus,
    ExecutionOutcome,
    ExecutionResult,
    Incident,
    IncidentRecord,
    IncidentState,
    MockActionResult,
    RecoveryCandidate,
    RecoveryLease,
    WorkflowCursor,
    WorkflowCursorStatus,
    RunbookPlan,
    StateTransition,
)


class Base(DeclarativeBase):
    """Base class for the PostgreSQL durable Incident schema."""


class IncidentRow(Base):
    __tablename__ = "incidents"

    incident_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(255), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    incident_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    execution_results_payload: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    verification_result_payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)


class TriageResultRow(Base):
    __tablename__ = "triage_results"

    incident_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("incidents.incident_id", ondelete="CASCADE"), primary_key=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class RunbookPlanRow(Base):
    """Latest-plan compatibility snapshot for an incident."""

    __tablename__ = "runbook_plans"

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("incidents.incident_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class RunbookPlanRevisionRow(Base):
    """An immutable execution-identity namespace for one plan revision."""

    __tablename__ = "runbook_plan_revisions"

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("incidents.incident_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class RunbookPlanStepRow(Base):
    """Database-enforced identities for executable forward and rollback steps."""

    __tablename__ = "runbook_plan_steps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "plan_revision"],
            ["runbook_plan_revisions.plan_id", "runbook_plan_revisions.plan_revision"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("plan_id", "plan_revision", "step_id", name="uq_plan_revision_step"),
        UniqueConstraint("action_id", name="uq_runbook_action_id"),
        UniqueConstraint("idempotency_key", name="uq_runbook_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    step_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    action_name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_rollback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class ActionJournalRow(Base):
    __tablename__ = "action_journal"

    action_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("runbook_plan_steps.action_id", ondelete="CASCADE"),
        primary_key=True,
    )
    incident_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    step_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    action_name: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    journal_status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    current_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retryable: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    retry_exhausted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    side_effect_possible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    request_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    response_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class ActionAttemptRow(Base):
    __tablename__ = "action_attempts"
    __table_args__ = (UniqueConstraint("action_id", "attempt_no", name="uq_action_attempt_number"),)

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("action_journal.action_id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    success: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    retryable: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    retry_exhausted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    side_effect_possible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    request_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    response_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class WorkflowCursorRow(Base):
    """One durable execution position for an incident's selected plan revision."""

    __tablename__ = "workflow_cursors"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "plan_revision"],
            ["runbook_plan_revisions.plan_id", "runbook_plan_revisions.plan_revision"],
            ondelete="CASCADE",
        ),
    )

    incident_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("incidents.incident_id", ondelete="CASCADE"), primary_key=True
    )
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    current_step_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    current_action_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    cursor_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class RecoveryLeaseRow(Base):
    """PostgreSQL-owned recovery claim. A lease is not an exactly-once guarantee."""

    __tablename__ = "recovery_leases"

    action_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("action_journal.action_id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False, index=True)


class StateTransitionRow(Base):
    __tablename__ = "state_transitions"
    __table_args__ = (UniqueConstraint("incident_id", "sequence", name="uq_state_transition_sequence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("incidents.incident_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str] = mapped_column(String(32), nullable=False)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    triggered_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    transition_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class IncidentRepository:
    """Repository that reconstructs Pydantic IncidentRecord values from PostgreSQL."""

    def __init__(self, database_url: str) -> None:
        if not database_url.startswith("postgresql+"):
            raise ValueError("DATABASE_URL must use a PostgreSQL SQLAlchemy dialect")
        self.engine: Engine = create_engine(database_url, future=True, pool_pre_ping=True)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self._ensure_phase_two_schema()
        self._backfill_legacy_plans()

    def create(self, incident: Incident, thread_id: str) -> IncidentRecord:
        now = datetime.utcnow()
        record = IncidentRecord(
            incident_id=incident.incident_id,
            thread_id=thread_id,
            state=IncidentState.NEW,
            incident=incident,
            created_at=now,
            updated_at=now,
        )
        with self.session_factory.begin() as session:
            if session.get(IncidentRow, record.incident_id) is not None:
                raise ValueError(f"Incident already exists: {record.incident_id}")
            session.add(self._incident_row(record))
        return record

    def get(self, incident_id: str) -> Optional[IncidentRecord]:
        with self.session_factory() as session:
            row = session.get(IncidentRow, incident_id)
            return self._to_record(session, row) if row else None

    def get_by_thread(self, thread_id: str) -> Optional[IncidentRecord]:
        with self.session_factory() as session:
            row = session.scalar(
                select(IncidentRow).where(IncidentRow.thread_id == thread_id).order_by(
                    IncidentRow.created_at.desc()
                )
            )
            return self._to_record(session, row) if row else None

    def list_all(self) -> list[IncidentRecord]:
        with self.session_factory() as session:
            rows = session.scalars(select(IncidentRow).order_by(IncidentRow.created_at)).all()
            return [self._to_record(session, row) for row in rows]

    def list_by_state(self, state: IncidentState) -> list[IncidentRecord]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(IncidentRow)
                .where(IncidentRow.state == state.value)
                .order_by(IncidentRow.created_at)
            ).all()
            return [self._to_record(session, row) for row in rows]

    def update(self, record: IncidentRecord) -> IncidentRecord:
        with self.session_factory.begin() as session:
            row = session.get(IncidentRow, record.incident_id)
            if row is None:
                raise KeyError(f"Incident not found: {record.incident_id}")
            self._apply_record(row, record)
            self._upsert_triage(session, record)
            self._upsert_plan(session, record)
            self._replace_transitions(session, record.incident_id, record.state_history)
        return record

    def delete(self, incident_id: str) -> None:
        with self.session_factory.begin() as session:
            row = session.get(IncidentRow, incident_id)
            if row is not None:
                session.delete(row)

    def get_plan(
        self,
        incident_id: str,
        plan_revision: Optional[int] = None,
    ) -> Optional[RunbookPlan]:
        with self.session_factory() as session:
            statement = select(RunbookPlanRevisionRow).where(
                RunbookPlanRevisionRow.incident_id == incident_id
            )
            if plan_revision is not None:
                statement = statement.where(RunbookPlanRevisionRow.plan_revision == plan_revision)
            row = session.scalar(statement.order_by(RunbookPlanRevisionRow.plan_revision.desc()))
            return RunbookPlan(**row.payload) if row else None

    def start_action_journal(
        self,
        *,
        incident_id: str,
        plan_id: str,
        plan_revision: int,
        step_id: str,
        action_id: str,
        idempotency_key: str,
        action_name: str,
        target: Optional[str],
        request_metadata: Optional[dict] = None,
    ) -> ActionJournalRecord:
        now = datetime.utcnow()
        with self.session_factory.begin() as session:
            if session.get(ActionJournalRow, action_id) is not None:
                raise ValueError(f"Action journal already exists: {action_id}")
            session.add(
                ActionJournalRow(
                    action_id=action_id,
                    incident_id=incident_id,
                    plan_id=plan_id,
                    plan_revision=plan_revision,
                    step_id=step_id,
                    idempotency_key=idempotency_key,
                    action_name=action_name,
                    target=target,
                    journal_status=ActionJournalStatus.STARTED.value,
                    request_metadata=request_metadata or {},
                    response_metadata={},
                    started_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            cursor = session.get(WorkflowCursorRow, incident_id)
            if cursor is not None and cursor.current_action_id == action_id:
                cursor.cursor_status = WorkflowCursorStatus.EXECUTING.value
                cursor.updated_at = now
        return self.get_action_journal(action_id)  # type: ignore[return-value]

    def start_action_attempt(
        self,
        action_id: str,
        attempt_no: int,
        request_metadata: Optional[dict] = None,
    ) -> ActionAttemptRecord:
        now = datetime.utcnow()
        with self.session_factory.begin() as session:
            journal = session.get(ActionJournalRow, action_id)
            if journal is None:
                raise KeyError(f"Action journal not found: {action_id}")
            if journal.journal_status != ActionJournalStatus.STARTED.value:
                raise ValueError(f"Action journal is terminal: {action_id}")
            session.add(
                ActionAttemptRow(
                    attempt_id=f"attempt-{uuid.uuid4().hex}",
                    action_id=action_id,
                    attempt_no=attempt_no,
                    status=ActionJournalStatus.STARTED.value,
                    started_at=now,
                    request_metadata=request_metadata or {},
                    response_metadata={},
                )
            )
            journal.current_attempt = attempt_no
            journal.updated_at = now
        return self.get_action_attempt(action_id, attempt_no)  # type: ignore[return-value]

    def finish_action_attempt(
        self,
        action_id: str,
        attempt_no: int,
        result: ExecutionResult,
        response_metadata: Optional[dict] = None,
    ) -> ActionAttemptRecord:
        now = datetime.utcnow()
        with self.session_factory.begin() as session:
            row = session.scalar(
                select(ActionAttemptRow).where(
                    ActionAttemptRow.action_id == action_id,
                    ActionAttemptRow.attempt_no == attempt_no,
                )
            )
            if row is None:
                raise KeyError(f"Action attempt not found: {action_id}/{attempt_no}")
            row.status = ActionJournalStatus.TERMINAL.value
            row.finished_at = now
            row.outcome = result.outcome.value
            row.success = result.success
            row.retryable = result.retryable
            row.retry_exhausted = result.retry_exhausted
            row.side_effect_possible = result.side_effect_possible
            row.error_type = result.error_type
            row.error_message = result.error_message or result.error
            row.response_metadata = response_metadata or {}
        return self.get_action_attempt(action_id, attempt_no)  # type: ignore[return-value]

    def finish_action_journal(
        self,
        action_id: str,
        result: MockActionResult,
        response_metadata: Optional[dict] = None,
    ) -> ActionJournalRecord:
        return self._complete_action(
            action_id,
            result,
            response_metadata=response_metadata,
        )

    def get_action_journal(self, action_id: str) -> Optional[ActionJournalRecord]:
        with self.session_factory() as session:
            row = session.get(ActionJournalRow, action_id)
            return self._to_action_journal(row) if row else None

    def get_workflow_cursor(self, incident_id: str) -> Optional[WorkflowCursor]:
        with self.session_factory() as session:
            row = session.get(WorkflowCursorRow, incident_id)
            return self._to_workflow_cursor(row) if row else None

    def list_resumable_workflow_cursors(self) -> list[WorkflowCursor]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(WorkflowCursorRow)
                .where(WorkflowCursorRow.cursor_status == WorkflowCursorStatus.NOT_STARTED.value)
                .order_by(WorkflowCursorRow.updated_at)
            ).all()
            return [self._to_workflow_cursor(row) for row in rows]

    def block_workflow_cursor(self, incident_id: str) -> Optional[WorkflowCursor]:
        now = datetime.utcnow()
        with self.session_factory.begin() as session:
            row = session.get(WorkflowCursorRow, incident_id)
            if row is None:
                return None
            row.cursor_status = WorkflowCursorStatus.BLOCKED.value
            row.updated_at = now
        return self.get_workflow_cursor(incident_id)

    def acquire_recovery_lease(
        self,
        action_id: str,
        owner_id: str,
        lease_seconds: int,
    ) -> Optional[RecoveryLease]:
        """Atomically claim a non-terminal action or reclaim an expired lease."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        token = f"lease-{uuid.uuid4().hex}"
        with self.session_factory.begin() as session:
            journal = session.scalar(
                select(ActionJournalRow)
                .where(ActionJournalRow.action_id == action_id)
                .with_for_update()
            )
            if journal is None or journal.journal_status == ActionJournalStatus.TERMINAL.value:
                return None
            db_now = session.scalar(select(func.now()))
            if getattr(db_now, "tzinfo", None) is not None:
                db_now = db_now.replace(tzinfo=None)
            expires_at = db_now + timedelta(seconds=lease_seconds)
            statement = (
                pg_insert(RecoveryLeaseRow)
                .values(
                    action_id=action_id,
                    owner_id=owner_id,
                    lease_token=token,
                    acquired_at=db_now,
                    expires_at=expires_at,
                )
                .on_conflict_do_update(
                    index_elements=[RecoveryLeaseRow.action_id],
                    set_={
                        "owner_id": owner_id,
                        "lease_token": token,
                        "acquired_at": db_now,
                        "expires_at": expires_at,
                    },
                    where=RecoveryLeaseRow.expires_at <= db_now,
                )
                .returning(
                    RecoveryLeaseRow.action_id,
                    RecoveryLeaseRow.owner_id,
                    RecoveryLeaseRow.lease_token,
                    RecoveryLeaseRow.acquired_at,
                    RecoveryLeaseRow.expires_at,
                )
            )
            row = session.execute(statement).one_or_none()
            if row is None:
                return None
            return RecoveryLease(**row._mapping)

    def release_recovery_lease(self, action_id: str, owner_id: str, lease_token: str) -> bool:
        """Release only the exact owner/token pair; another worker cannot release it."""
        with self.session_factory.begin() as session:
            result = session.execute(
                delete(RecoveryLeaseRow).where(
                    RecoveryLeaseRow.action_id == action_id,
                    RecoveryLeaseRow.owner_id == owner_id,
                    RecoveryLeaseRow.lease_token == lease_token,
                )
            )
            return result.rowcount == 1

    def list_recovery_candidates(self) -> list[RecoveryCandidate]:
        """Return durable non-terminal actions; execution snapshots are not consulted."""
        with self.session_factory() as session:
            journals = session.scalars(
                select(ActionJournalRow)
                .where(ActionJournalRow.journal_status != ActionJournalStatus.TERMINAL.value)
                .order_by(ActionJournalRow.started_at)
            ).all()
            candidates: list[RecoveryCandidate] = []
            for journal in journals:
                latest_attempt = session.scalar(
                    select(ActionAttemptRow)
                    .where(ActionAttemptRow.action_id == journal.action_id)
                    .order_by(ActionAttemptRow.attempt_no.desc())
                )
                candidates.append(
                    RecoveryCandidate(
                        incident_id=journal.incident_id,
                        plan_id=journal.plan_id,
                        plan_revision=journal.plan_revision,
                        step_id=journal.step_id,
                        action_id=journal.action_id,
                        idempotency_key=journal.idempotency_key,
                        action_name=journal.action_name,
                        target=journal.target,
                        journal_status=ActionJournalStatus(journal.journal_status),
                        latest_attempt_no=latest_attempt.attempt_no if latest_attempt else None,
                        latest_attempt_status=(
                            ActionJournalStatus(latest_attempt.status) if latest_attempt else None
                        ),
                        latest_outcome=(
                            ExecutionOutcome(latest_attempt.outcome)
                            if latest_attempt and latest_attempt.outcome
                            else ExecutionOutcome(journal.outcome) if journal.outcome else None
                        ),
                        side_effect_possible=(
                            journal.side_effect_possible
                            or bool(latest_attempt and latest_attempt.side_effect_possible)
                        ),
                        retry_exhausted=(
                            journal.retry_exhausted
                            or bool(latest_attempt and latest_attempt.retry_exhausted)
                        ),
                        request_metadata=journal.request_metadata or {},
                    )
                )
            return candidates

    def complete_recovered_action(
        self,
        action_id: str,
        result: MockActionResult,
        recovery_metadata: Optional[dict] = None,
        owner_id: Optional[str] = None,
        lease_token: Optional[str] = None,
    ) -> ActionJournalRecord:
        """Atomically terminalize Journal, snapshot, cursor, and owned recovery lease."""
        return self._complete_action(
            action_id,
            result,
            response_metadata=recovery_metadata,
            owner_id=owner_id,
            lease_token=lease_token,
        )

    def _complete_action(
        self,
        action_id: str,
        result: MockActionResult,
        *,
        response_metadata: Optional[dict] = None,
        owner_id: Optional[str] = None,
        lease_token: Optional[str] = None,
    ) -> ActionJournalRecord:
        """The sole terminal write path for actions that participate in a workflow cursor."""
        if (owner_id is None) != (lease_token is None):
            raise ValueError("owner_id and lease_token must be supplied together")
        now = datetime.utcnow()
        with self.session_factory.begin() as session:
            journal = session.scalar(
                select(ActionJournalRow)
                .where(ActionJournalRow.action_id == action_id)
                .with_for_update()
            )
            if journal is None:
                raise KeyError(f"Action journal not found: {action_id}")
            if journal.journal_status == ActionJournalStatus.TERMINAL.value:
                raise ValueError(f"Action journal is already terminal: {action_id}")
            lease = None
            if owner_id is not None:
                lease = session.get(RecoveryLeaseRow, action_id)
                if lease is None or lease.owner_id != owner_id or lease.lease_token != lease_token:
                    raise PermissionError("Recovery lease is not owned by this worker")
                db_now = session.scalar(select(func.now()))
                if getattr(db_now, "tzinfo", None) is not None:
                    db_now = db_now.replace(tzinfo=None)
                if lease.expires_at <= db_now:
                    raise PermissionError("Recovery lease has expired")

            journal.journal_status = ActionJournalStatus.TERMINAL.value
            journal.outcome = result.outcome or (
                ExecutionOutcome.SUCCESS.value if result.success else ExecutionOutcome.FAILED.value
            )
            journal.target = result.target or journal.target
            journal.retryable = result.retryable
            journal.retry_exhausted = result.retry_exhausted
            journal.side_effect_possible = result.side_effect_possible
            journal.error_type = result.error_type
            journal.error_message = result.message if not result.success else None
            journal.response_metadata = {**result.metadata, **(response_metadata or {})}
            journal.finished_at = now
            journal.updated_at = now

            incident = session.get(IncidentRow, journal.incident_id)
            if incident is None:
                raise KeyError(f"Incident not found: {journal.incident_id}")
            snapshots = list(incident.execution_results_payload or [])
            if not any(snapshot.get("action_id") == action_id for snapshot in snapshots):
                snapshots.append(
                    {
                        **result.model_dump(mode="json"),
                        "action_id": action_id,
                        "step_id": journal.step_id,
                        "plan_id": journal.plan_id,
                        "plan_revision": journal.plan_revision,
                        "recovered": True,
                    }
                )
                incident.execution_results_payload = snapshots
                incident.updated_at = now
            cursor = session.get(WorkflowCursorRow, journal.incident_id)
            if (
                cursor is not None
                and cursor.plan_id == journal.plan_id
                and cursor.plan_revision == journal.plan_revision
                and cursor.current_action_id == action_id
            ):
                if journal.outcome == ExecutionOutcome.SUCCESS.value:
                    self._advance_cursor_locked(session, cursor)
                else:
                    cursor.cursor_status = WorkflowCursorStatus.BLOCKED.value
                    cursor.updated_at = now
            if lease is not None:
                session.delete(lease)
        return self.get_action_journal(action_id)  # type: ignore[return-value]

    def list_action_attempts(self, action_id: str) -> list[ActionAttemptRecord]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ActionAttemptRow)
                .where(ActionAttemptRow.action_id == action_id)
                .order_by(ActionAttemptRow.attempt_no)
            ).all()
            return [self._to_action_attempt(row) for row in rows]

    def get_action_attempt(self, action_id: str, attempt_no: int) -> Optional[ActionAttemptRecord]:
        with self.session_factory() as session:
            row = session.scalar(
                select(ActionAttemptRow).where(
                    ActionAttemptRow.action_id == action_id,
                    ActionAttemptRow.attempt_no == attempt_no,
                )
            )
            return self._to_action_attempt(row) if row else None

    def _ensure_phase_two_schema(self) -> None:
        """Add the latest-snapshot revision field for databases created in Phase 1."""
        if self.engine.dialect.name != "postgresql":
            return
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE runbook_plans "
                    "ADD COLUMN IF NOT EXISTS plan_revision INTEGER NOT NULL DEFAULT 1"
                )
            )

    @staticmethod
    def _to_action_journal(row: ActionJournalRow) -> ActionJournalRecord:
        return ActionJournalRecord(
            action_id=row.action_id,
            incident_id=row.incident_id,
            plan_id=row.plan_id,
            plan_revision=row.plan_revision,
            step_id=row.step_id,
            idempotency_key=row.idempotency_key,
            action_name=row.action_name,
            target=row.target,
            journal_status=ActionJournalStatus(row.journal_status),
            outcome=ExecutionOutcome(row.outcome) if row.outcome else None,
            current_attempt=row.current_attempt,
            retryable=row.retryable,
            retry_exhausted=row.retry_exhausted,
            side_effect_possible=row.side_effect_possible,
            error_type=row.error_type,
            error_message=row.error_message,
            started_at=row.started_at,
            finished_at=row.finished_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            request_metadata=row.request_metadata or {},
            response_metadata=row.response_metadata or {},
        )

    @staticmethod
    def _to_action_attempt(row: ActionAttemptRow) -> ActionAttemptRecord:
        return ActionAttemptRecord(
            attempt_id=row.attempt_id,
            action_id=row.action_id,
            attempt_no=row.attempt_no,
            status=ActionJournalStatus(row.status),
            started_at=row.started_at,
            finished_at=row.finished_at,
            outcome=ExecutionOutcome(row.outcome) if row.outcome else None,
            success=row.success,
            retryable=row.retryable,
            retry_exhausted=row.retry_exhausted,
            side_effect_possible=row.side_effect_possible,
            error_type=row.error_type,
            error_message=row.error_message,
            request_metadata=row.request_metadata or {},
            response_metadata=row.response_metadata or {},
        )

    @staticmethod
    def _to_workflow_cursor(row: WorkflowCursorRow) -> WorkflowCursor:
        return WorkflowCursor(
            incident_id=row.incident_id,
            plan_id=row.plan_id,
            plan_revision=row.plan_revision,
            current_step_id=row.current_step_id,
            current_action_id=row.current_action_id,
            cursor_status=WorkflowCursorStatus(row.cursor_status),
            updated_at=row.updated_at,
        )

    def _backfill_legacy_plans(self) -> None:
        """Persist Phase 1 JSONB plans into revision and identity tables exactly once."""
        with self.session_factory.begin() as session:
            legacy_rows = session.scalars(select(RunbookPlanRow)).all()
            for legacy in legacy_rows:
                plan = RunbookPlan(**legacy.payload)
                plan.plan_id = legacy.plan_id
                plan.plan_revision = legacy.plan_revision or 1
                plan.bind_execution_identities(legacy.incident_id)

                revision = session.get(
                    RunbookPlanRevisionRow,
                    (plan.plan_id, plan.plan_revision),
                )
                if revision is None:
                    revision = RunbookPlanRevisionRow(
                        plan_id=plan.plan_id,
                        plan_revision=plan.plan_revision,
                        incident_id=legacy.incident_id,
                        payload=plan.model_dump(mode="json"),
                        created_at=legacy.created_at,
                        updated_at=legacy.updated_at,
                    )
                    session.add(revision)
                    session.flush()
                    self._replace_plan_steps(session, plan)
                elif revision.incident_id != legacy.incident_id:
                    raise ValueError(
                        "Persisted plan_id and plan_revision already belong to another incident"
                    )
                else:
                    revision.payload = plan.model_dump(mode="json")
                    revision.updated_at = legacy.updated_at
                    self._replace_plan_steps(session, plan)

                legacy.plan_revision = plan.plan_revision
                legacy.payload = plan.model_dump(mode="json")
                self._ensure_workflow_cursor(session, legacy.incident_id, plan)

    @staticmethod
    def _incident_row(record: IncidentRecord) -> IncidentRow:
        return IncidentRow(
            incident_id=record.incident_id,
            thread_id=record.thread_id,
            state=record.state.value,
            incident_payload=record.incident.model_dump(mode="json"),
            execution_results_payload=record.execution_results,
            verification_result_payload=record.verification_result,
            created_at=record.created_at,
            updated_at=record.updated_at,
            resolved_at=record.resolved_at,
        )

    @staticmethod
    def _apply_record(row: IncidentRow, record: IncidentRecord) -> None:
        record.updated_at = datetime.utcnow()
        row.thread_id = record.thread_id
        row.state = record.state.value
        row.incident_payload = record.incident.model_dump(mode="json")
        row.execution_results_payload = record.execution_results
        row.verification_result_payload = record.verification_result
        row.updated_at = record.updated_at
        row.resolved_at = record.resolved_at

    @staticmethod
    def _upsert_triage(session: Session, record: IncidentRecord) -> None:
        if record.triage_result is None:
            return
        row = session.get(TriageResultRow, record.incident_id)
        if row is None:
            session.add(
                TriageResultRow(
                    incident_id=record.incident_id,
                    payload=record.triage_result,
                    updated_at=datetime.utcnow(),
                )
            )
            return
        row.payload = record.triage_result
        row.updated_at = datetime.utcnow()

    @staticmethod
    def _upsert_plan(session: Session, record: IncidentRecord) -> None:
        if record.runbook_plan is None:
            return
        plan = record.runbook_plan
        plan.bind_execution_identities(record.incident_id)
        payload = plan.model_dump(mode="json")
        now = datetime.utcnow()

        revision = session.get(
            RunbookPlanRevisionRow,
            (plan.plan_id, plan.plan_revision),
        )
        if revision is None:
            revision = RunbookPlanRevisionRow(
                plan_id=plan.plan_id,
                plan_revision=plan.plan_revision,
                incident_id=record.incident_id,
                payload=payload,
                created_at=now,
                updated_at=now,
            )
            session.add(revision)
            session.flush()
        else:
            if revision.incident_id != record.incident_id:
                raise ValueError(
                    "Persisted plan_id and plan_revision already belong to another incident"
                )
            revision.payload = payload
            revision.updated_at = now

        IncidentRepository._replace_plan_steps(session, plan)
        IncidentRepository._ensure_workflow_cursor(session, record.incident_id, plan)

        # Preserve the Phase 1 table as an incident's latest-plan snapshot.
        existing = session.scalar(
            select(RunbookPlanRow).where(RunbookPlanRow.incident_id == record.incident_id)
        )
        if existing is None:
            session.add(
                RunbookPlanRow(
                    plan_id=plan.plan_id,
                    incident_id=record.incident_id,
                    plan_revision=plan.plan_revision,
                    payload=payload,
                    created_at=now,
                    updated_at=now,
                )
            )
            return
        existing.plan_id = plan.plan_id
        existing.plan_revision = plan.plan_revision
        existing.payload = payload
        existing.updated_at = now

    @staticmethod
    def _replace_plan_steps(session: Session, plan: RunbookPlan) -> None:
        """Append identities and allow only display-order changes within a revision."""
        expected = [
            (position, False, instruction)
            for position, instruction in enumerate(plan.actions)
        ] + [
            (position, True, instruction)
            for position, instruction in enumerate(plan.rollback_actions)
        ]
        existing_rows = session.scalars(
            select(RunbookPlanStepRow).where(
                RunbookPlanStepRow.plan_id == plan.plan_id,
                RunbookPlanStepRow.plan_revision == plan.plan_revision,
            )
        ).all()
        existing_by_step = {row.step_id: row for row in existing_rows}
        expected_step_ids = {instruction.step_id for _, _, instruction in expected}

        removed_step_ids = set(existing_by_step) - expected_step_ids
        if removed_step_ids:
            raise ValueError("Plan revision steps are append-only; create a new revision to remove a step")

        for position, is_rollback, instruction in expected:
            if instruction.idempotency_key is None:
                raise ValueError("Runbook action identity must be bound before persistence")
            row = existing_by_step.get(instruction.step_id)
            if row is None:
                session.add(
                    RunbookPlanStepRow(
                        plan_id=plan.plan_id,
                        plan_revision=plan.plan_revision,
                        step_id=instruction.step_id,
                        action_id=instruction.action_id,
                        idempotency_key=instruction.idempotency_key,
                        action_name=instruction.action.value,
                        is_rollback=is_rollback,
                        position=position,
                    )
                )
                continue
            if (
                row.action_id != instruction.action_id
                or row.idempotency_key != instruction.idempotency_key
                or row.is_rollback != is_rollback
            ):
                raise ValueError("Plan revision cannot change a persisted step identity")
            row.action_name = instruction.action.value
            row.position = position

    @staticmethod
    def _ensure_workflow_cursor(
        session: Session,
        incident_id: str,
        plan: RunbookPlan,
    ) -> None:
        """Initialize a revision-bound cursor without mutating a live revision."""
        cursor = session.get(WorkflowCursorRow, incident_id)
        first = plan.actions[0] if plan.actions else None
        now = datetime.utcnow()
        if cursor is not None and cursor.plan_id == plan.plan_id and cursor.plan_revision == plan.plan_revision:
            return
        if cursor is None:
            session.add(
                WorkflowCursorRow(
                    incident_id=incident_id,
                    plan_id=plan.plan_id,
                    plan_revision=plan.plan_revision,
                    current_step_id=first.step_id if first else None,
                    current_action_id=first.action_id if first else None,
                    cursor_status=(
                        WorkflowCursorStatus.NOT_STARTED.value
                        if first
                        else WorkflowCursorStatus.COMPLETED.value
                    ),
                    updated_at=now,
                )
            )
            return

        # A newly persisted plan revision is an explicit planning decision. The
        # cursor moves to its first durable action; historical action identities
        # and attempts remain untouched in their prior revision.
        cursor.plan_id = plan.plan_id
        cursor.plan_revision = plan.plan_revision
        cursor.current_step_id = first.step_id if first else None
        cursor.current_action_id = first.action_id if first else None
        cursor.cursor_status = (
            WorkflowCursorStatus.NOT_STARTED.value
            if first
            else WorkflowCursorStatus.COMPLETED.value
        )
        cursor.updated_at = now

    @staticmethod
    def _advance_cursor_locked(session: Session, cursor: WorkflowCursorRow) -> None:
        """Advance using persisted plan position, never arithmetic on step IDs."""
        current = session.scalar(
            select(RunbookPlanStepRow).where(
                RunbookPlanStepRow.plan_id == cursor.plan_id,
                RunbookPlanStepRow.plan_revision == cursor.plan_revision,
                RunbookPlanStepRow.action_id == cursor.current_action_id,
                RunbookPlanStepRow.is_rollback.is_(False),
            )
        )
        if current is None:
            raise ValueError("Workflow cursor does not reference a forward plan step")
        next_step = session.scalar(
            select(RunbookPlanStepRow)
            .where(
                RunbookPlanStepRow.plan_id == cursor.plan_id,
                RunbookPlanStepRow.plan_revision == cursor.plan_revision,
                RunbookPlanStepRow.is_rollback.is_(False),
                RunbookPlanStepRow.position > current.position,
            )
            .order_by(RunbookPlanStepRow.position)
        )
        cursor.updated_at = datetime.utcnow()
        if next_step is None:
            cursor.current_step_id = None
            cursor.current_action_id = None
            cursor.cursor_status = WorkflowCursorStatus.COMPLETED.value
            return
        cursor.current_step_id = next_step.step_id
        cursor.current_action_id = next_step.action_id
        cursor.cursor_status = WorkflowCursorStatus.NOT_STARTED.value

    @staticmethod
    def _replace_transitions(
        session: Session,
        incident_id: str,
        transitions: Iterable[StateTransition],
    ) -> None:
        expected = list(transitions)
        existing = session.scalars(
            select(StateTransitionRow)
            .where(StateTransitionRow.incident_id == incident_id)
            .order_by(StateTransitionRow.sequence)
        ).all()
        for row, transition in zip(existing, expected):
            if (
                row.from_state != transition.from_state.value
                or row.to_state != transition.to_state.value
                or row.timestamp != transition.timestamp
            ):
                raise ValueError("State transition history is append-only")
        if len(existing) > len(expected):
            raise ValueError("State transition history cannot be truncated")
        for sequence, transition in enumerate(expected[len(existing):], start=len(existing)):
            session.add(
                StateTransitionRow(
                    incident_id=incident_id,
                    sequence=sequence,
                    from_state=transition.from_state.value,
                    to_state=transition.to_state.value,
                    timestamp=transition.timestamp,
                    reason=transition.reason,
                    triggered_by=transition.triggered_by,
                    transition_metadata=transition.metadata,
                )
            )

    @staticmethod
    def _to_record(session: Session, row: IncidentRow) -> IncidentRecord:
        triage = session.get(TriageResultRow, row.incident_id)
        plan = session.scalar(
            select(RunbookPlanRevisionRow)
            .where(RunbookPlanRevisionRow.incident_id == row.incident_id)
            .order_by(RunbookPlanRevisionRow.plan_revision.desc())
        )
        transitions = session.scalars(
            select(StateTransitionRow)
            .where(StateTransitionRow.incident_id == row.incident_id)
            .order_by(StateTransitionRow.sequence)
        ).all()
        runbook_plan = RunbookPlan(**plan.payload) if plan else None
        return IncidentRecord(
            incident_id=row.incident_id,
            thread_id=row.thread_id,
            state=IncidentState(row.state),
            incident=Incident(**row.incident_payload),
            triage_result=triage.payload if triage else None,
            plan=runbook_plan.steps if runbook_plan else None,
            runbook_plan=runbook_plan,
            execution_results=row.execution_results_payload or [],
            verification_result=row.verification_result_payload,
            state_history=[
                StateTransition(
                    from_state=IncidentState(item.from_state),
                    to_state=IncidentState(item.to_state),
                    timestamp=item.timestamp,
                    reason=item.reason,
                    triggered_by=item.triggered_by,
                    metadata=item.transition_metadata or {},
                )
                for item in transitions
            ],
            created_at=row.created_at,
            updated_at=row.updated_at,
            resolved_at=row.resolved_at,
        )
