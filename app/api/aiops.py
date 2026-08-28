"""
AIOps 智能运维接口

Metric-driven AIOps 接口:
1. POST /api/aiops/incident — 新链路 事件驱动
2. POST /api/aiops/stsrs — STSRS 专用入口
3. POST /api/aiops/metrics — [NEW] 原始监测指标输入（Metric-driven 核心入口）
4. GET /api/aiops/sse/{thread_id} — SSE 事件订阅
5. GET /api/aiops/incidents — 事件列表
6. GET /api/aiops/incidents/{id} — 事件详情
7. GET /api/aiops/incidents/{id}/timeline — 事件时间线回放
"""

import json
import uuid
from typing import Optional
from fastapi import APIRouter, Query
from sse_starlette.sse import EventSourceResponse
from loguru import logger

from app.models.incident import IncidentSource, RawIncidentRequest
from app.services.aiops_service import aiops_service
from app.core.incident_store import incident_store
from app.core.audit_store import audit_store

router = APIRouter()


# ================================================================
# 新链路: 事件驱动
# ================================================================

@router.post("/aiops/incident")
async def process_incident_stream(
    payload: dict,
    source: str = Query(default=None, description="[兼容旧格式] 事件来源: prometheus/mcp/stsrs/manual"),
    session_id: str = Query(default=None, description="会话ID"),
):
    """
    事件驱动 AIOps 处理接口（新链路）。

    支持两种请求格式:

    **新统一格式（推荐）:**
    ```json
    {
      "source": "STSRS",
      "raw_event": {
        "attack_code": "STSRS-1003",
        "train_id": "Train-1H66",
        ...
      }
    }
    ```

    **旧扁平格式（兼容）:**
    ```json
    {
      "attack_type": "DoS",
      "source_ip": "192.168.1.100",
      ...
    }
    ```
    旧格式需通过 query param `?source=manual` 指定来源。

    链路: IncidentRouter → TriageAgent → RunbookAgent
          → ActionOrchestrator → Verifier → Replanner
    """
    sid = session_id or f"session-{uuid.uuid4().hex[:8]}"

    # ---- 统一格式解析 ----
    if "raw_event" in payload and "source" in payload:
        # 新统一格式: {"source": "STSRS", "raw_event": {...}}
        try:
            req = RawIncidentRequest(**payload)
            incident_source = req.source
            raw = req.raw_event
        except Exception:
            incident_source = IncidentSource.MANUAL
            raw = payload
    elif source:
        # 旧格式 + query param
        try:
            incident_source = IncidentSource(source.lower())
        except ValueError:
            incident_source = IncidentSource.MANUAL
        raw = payload
    else:
        # 旧格式无 query param，尝试从 payload 推断
        raw_source = payload.get("source", "")
        try:
            incident_source = IncidentSource(raw_source.lower()) if raw_source else IncidentSource.MANUAL
        except ValueError:
            incident_source = IncidentSource.MANUAL
        raw = payload.get("raw_event", payload)

    logger.info(f"[会话 {sid}] 事件驱动处理, source={incident_source.value}")

    async def event_generator():
        try:
            async for event in aiops_service.process_incident(
                raw_event=raw,
                source=incident_source,
                session_id=sid,
            ):
                yield {
                    "event": "message",
                    "data": json.dumps(event, ensure_ascii=False),
                }
                if event.get("type") in ["complete", "error"]:
                    break
        except Exception as e:
            logger.error(f"[会话 {sid}] 异常: {e}", exc_info=True)
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "error",
                    "stage": "exception",
                    "message": f"事件处理异常: {str(e)}",
                }, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())


@router.post("/aiops/stsrs")
async def process_stsrs_stream(
    payload: dict,
    session_id: str = Query(default=None, description="会话ID"),
):
    """
    STSRS 列车信号安全系统专用入口。

    不硬编码动作，走完整的事件驱动链路:
    1. CaseKB 查询相似历史案例
    2. RunbookKB 查询通用 SOP
    3. TopologyKB 查询拓扑影响
    4. ActionOrchestrator 选择 Mock 动作

    **请求体示例:**
    ```json
    {
      "attack_code": "STSRS-1003",
      "description": "检测到 Replay Attack",
      "train_id": "Train-1H66",
      "signal_id": "Signal-YT546",
      "control_center": "ControlCenter-A",
      "region": "North-3",
      "line": "L5"
    }
    ```
    """
    sid = session_id or f"stsrs-{uuid.uuid4().hex[:8]}"
    logger.info(f"[会话 {sid}] 收到 STSRS 告警: {payload.get('attack_code', 'unknown')}")

    async def event_generator():
        try:
            async for event in aiops_service.process_stsrs(
                stsrs_data=payload,
                session_id=sid,
            ):
                yield {
                    "event": "message",
                    "data": json.dumps(event, ensure_ascii=False),
                }
                if event.get("type") in ["complete", "error"]:
                    break
        except Exception as e:
            logger.error(f"[会话 {sid}] 异常: {e}", exc_info=True)
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "error",
                    "stage": "exception",
                    "message": f"STSRS 处理异常: {str(e)}",
                }, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())


# ================================================================
# Metric-driven 核心入口: 原始监测指标输入
# ================================================================

@router.post("/aiops/metrics")
async def process_metrics_stream(
    payload: dict,
    session_id: str = Query(default=None, description="会话ID"),
):
    """
    [NEW] Metric-driven AIOps 核心入口 — 接收原始监测指标数据。

    不要求提供 attack_type 或 attack_code — 系统只知道原始监测指标。
    attack_type 由 TriageAgent 基于指标异常模式自动诊断。

    请求格式:
    ```json
    {
      "train_id": "T001",
      "signal_id": "S001",
      "timestamp": "2024-01-01T12:00:00",
      "metrics": {
        "speed": 120.0,
        "packet_loss": 0.35,
        "latency": 250.0,
        "renewal_interval": 6000.0,
        "burstiness": 0.8,
        "signal_status": "RED",
        "overlap_status": "NORMAL"
      }
    }
    ```

    也支持 PrometheusMetricSnapshot 格式:
    ```json
    {
      "labels": {"train": "T001", "signal": "S001"},
      "metrics": {"rail_packet_loss": 0.35, "rail_latency": 250.0}
    }
    ```

    内部流程:
    1. 通过 EventNormalizer.normalize_metric() 生成 Incident (attack_type=UNKNOWN)
    2. SeverityEngine 基于指标影响初步分级
    3. TriageAgent 诊断攻击类型
    4. RunbookAgent → Action → Verify
    """
    sid = session_id or f"metrics-{uuid.uuid4().hex[:8]}"
    logger.info(f"[会话 {sid}] 收到原始监测指标: train={payload.get('train_id')}, signal={payload.get('signal_id')}")

    async def event_generator():
        try:
            # 使用 normalize_metric 入口（attack_type=UNKNOWN）
            async for event in aiops_service.process_metrics(
                metrics_payload=payload,
                session_id=sid,
            ):
                yield {
                    "event": "message",
                    "data": json.dumps(event, ensure_ascii=False),
                }
                if event.get("type") in ["complete", "error"]:
                    break
        except Exception as e:
            logger.error(f"[会话 {sid}] 异常: {e}", exc_info=True)
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "error",
                    "stage": "exception",
                    "message": f"指标处理异常: {str(e)}",
                }, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())


# ================================================================
# SSE 事件订阅
# ================================================================

@router.get("/aiops/sse/{thread_id}")
async def subscribe_sse(thread_id: str):
    """
    SSE 事件订阅接口。

    订阅某个 thread 的全链路事件流，支持前端实时展示处理进度。

    Args:
        thread_id: LangGraph thread ID
    """
    logger.info(f"SSE 订阅: thread_id={thread_id}")

    async def event_generator():
        try:
            async for event in aiops_service.subscribe_sse(thread_id):
                yield {
                    "event": "message",
                    "data": json.dumps(event, ensure_ascii=False),
                }
        except Exception as e:
            logger.error(f"SSE 订阅异常: {e}")

    return EventSourceResponse(event_generator())


# ================================================================
# 事件查询接口
# ================================================================

@router.get("/aiops/incidents")
async def list_incidents(state: Optional[str] = Query(default=None, description="按状态过滤")):
    """
    列出所有事件。
    """
    if state:
        from app.models.incident import IncidentState
        try:
            s = IncidentState(state.upper())
            records = incident_store.list_by_state(s)
        except ValueError:
            return {"code": 400, "message": f"无效状态: {state}"}
    else:
        records = incident_store.list_all()

    return {
        "code": 200,
        "message": "success",
        "data": {
            "total": len(records),
            "incidents": [
                {
                    "incident_id": r.incident_id,
                    "thread_id": r.thread_id,
                    "state": r.state.value,
                    "attack_type": r.incident.attack_type.value,
                    "severity": r.incident.severity.value,
                    "created_at": r.created_at.isoformat(),
                    "updated_at": r.updated_at.isoformat(),
                }
                for r in records
            ],
        },
    }


@router.get("/aiops/incidents/{incident_id}")
async def get_incident(incident_id: str):
    """
    获取事件详情。
    """
    record = incident_store.get(incident_id)
    if not record:
        return {"code": 404, "message": f"事件不存在: {incident_id}"}

    return {
        "code": 200,
        "message": "success",
        "data": {
            "incident_id": record.incident_id,
            "thread_id": record.thread_id,
            "state": record.state.value,
            "incident": record.incident.model_dump(mode="json"),
            "triage_result": record.triage_result,
            "plan": record.plan,
            "execution_results": record.execution_results,
            "verification_result": record.verification_result,
            "state_history": [
                {
                    "from": h.from_state.value,
                    "to": h.to_state.value,
                    "timestamp": h.timestamp.isoformat(),
                    "reason": h.reason,
                    "triggered_by": h.triggered_by,
                }
                for h in record.state_history
            ],
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "resolved_at": record.resolved_at.isoformat() if record.resolved_at else None,
        },
    }


@router.get("/aiops/incidents/{incident_id}/timeline")
async def get_incident_timeline(incident_id: str):
    """
    获取事件完整时间线（审计回放）。
    """
    timeline = audit_store.replay_timeline(incident_id)
    return {
        "code": 200,
        "message": "success",
        "data": {
            "incident_id": incident_id,
            "total_events": len(timeline),
            "timeline": timeline,
        },
    }


@router.get("/aiops/stats")
async def get_stats():
    """获取统计信息"""
    counts = incident_store.count_by_state()
    return {
        "code": 200,
        "message": "success",
        "data": {
            "incident_counts": counts,
            "total_audit_entries": audit_store.total_entries,
        },
    }
