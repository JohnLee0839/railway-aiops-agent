"""
RunbookAgent — 生成处置计划

职责:
- 动态查询知识库生成处置计划
- 不允许硬编码 STSRS 攻击的固定动作

检索优先级（硬性规定）:
1. CaseKB（历史案例）
2. RunbookKB（SOP）
3. TopologyKB（拓扑影响）

正确逻辑:
- 先查询 CaseKB 中相似攻击类型的历史处置方案
- 若无相似案例，再查询 RunbookKB 的通用 SOP
- 最后结合 TopologyKB 计算影响范围与可执行建议
"""

from textwrap import dedent
from typing import Optional, List
from langchain_core.prompts import ChatPromptTemplate
from langchain_qwq import ChatQwen
from loguru import logger

from app.config import config
from app.models.incident import (
    Incident,
    TriageResult,
    RunbookPlan,
    ActionInstruction,
    Severity,
    ApprovalAction,
)


# RunbookAgent 提示词
RUNBOOK_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        dedent("""
            你是一个铁路信号系统运维操作手册专家（RunbookAgent）。

            你的任务是根据事件信息和分诊结果，生成具体的处置步骤计划。

            **严禁硬编码固定动作！** 必须基于提供的知识库内容动态生成计划：
            - STSRS Replay Attack 不能固定执行某个动作
            - DoS 不能固定执行某个动作
            - Jamming 不能固定执行某个动作

            可用 Mock 动作列表（只能选择以下 action 值）:
            1. switch_backup_link — 切换到备用链路
            2. restart_gateway — 重启网关
            3. block_suspicious_source — 封禁可疑来源IP
            4. notify_dispatcher — 通知调度员
            5. generate_ticket — 生成工单
            6. verify_network_health — 验证网络健康状态

            输出要求:
            - actions: 按顺序输出对象列表。每项必须包含 action、arguments、description。
              action 必须是上述动作名之一；arguments 是传给该动作的 JSON 对象；
              description 只用于展示和审计，绝不能作为动作识别依据。
            - source_kb: 知识来源（CaseKB / RunbookKB / TopologyKB）
            - affected_assets: 受影响的资产列表
            - rollback_actions: 回滚动作对象列表，结构与 actions 相同。
            - 当前 Mock 工具未实现 STOP_TRAIN、BLOCK_SECTION、EMERGENCY_SHUTDOWN；
              不得将它们放入 actions 或 rollback_actions。
            - restart_gateway 和 block_suspicious_source 的审批要求由工具注册表决定；
              不得自行填写或绕过 requires_approval、approval_actions。
        """).strip(),
    ),
    ("placeholder", "{messages}"),
])


class RunbookAgent:
    """
    RunbookAgent — 生成处置计划

    检索优先级:
    1. CaseKB → 历史处置方案
    2. RunbookKB → 通用 SOP
    3. TopologyKB → 影响范围
    """

    def __init__(self):
        self.llm = ChatQwen(
            model=config.rag_model,
            api_key=config.dashscope_api_key,
            temperature=0,
        )
        self.chain = RUNBOOK_PROMPT | self.llm.with_structured_output(RunbookPlan)

    @staticmethod
    def _retrieve_knowledge_tool():
        from app.tools import retrieve_knowledge

        return retrieve_knowledge

    async def generate_plan(
        self,
        incident: Incident,
        triage_result: TriageResult,
    ) -> RunbookPlan:
        """
        生成处置计划（Metric-driven 版本）。

        KB 检索基于 TriageResult 的诊断结论（attack_type + root_cause），
        而非 Incident.attack_type（因为输入时未知）。

        Args:
            incident: 原始事件
            triage_result: TriageAgent 诊断结果（含 attack_type, root_cause）

        Returns:
            RunbookPlan（处置步骤、知识来源、审批要求）
        """
        # TriageResult 中的 attack_type 是诊断结论
        diagnosed_type = triage_result.attack_type or "UNKNOWN"
        logger.info(
            f"[RunbookAgent] 开始生成处置计划: incident_id={incident.incident_id}, "
            f"diagnosed_type={diagnosed_type}, "
            f"root_cause={triage_result.root_cause}"
        )

        # === 检索优先级 1: CaseKB ===
        casekb_context = await self._query_casekb(incident, triage_result)
        source_kb = "CaseKB" if casekb_context else ""

        # === 检索优先级 2: RunbookKB（若无案例） ===
        runbook_context = ""
        if not casekb_context:
            runbook_context = await self._query_runbookkb(incident, triage_result)
            source_kb = "RunbookKB" if runbook_context else source_kb

        # === 检索优先级 3: TopologyKB ===
        topology_context = await self._query_topologykb(incident, triage_result)

        # === 构建输入 ===
        plan_input = self._build_plan_input(
            incident, triage_result,
            casekb_context, runbook_context, topology_context,
        )

        # === LLM 生成计划 ===
        try:
            result = await self.chain.ainvoke({
                "messages": [("user", plan_input)],
            })

            if isinstance(result, RunbookPlan):
                plan = result
            else:
                plan = RunbookPlan(**result)

            # 设置知识来源
            if not plan.source_kb:
                plan.source_kb = source_kb or "TopologyKB"
            self._apply_registry_metadata(plan)

            logger.info(
                f"[RunbookAgent] 计划生成完成: {len(plan.steps)} 步骤, "
                f"来源={plan.source_kb}, 需审批={plan.requires_approval}"
            )
            return plan

        except Exception as e:
            logger.error(f"[RunbookAgent] LLM 调用失败: {e}", exc_info=True)
            return self._fallback_plan(incident, triage_result)

    @staticmethod
    def _apply_registry_metadata(plan: RunbookPlan) -> None:
        """以工具注册表为准生成审批展示字段，忽略 LLM 的自报结果。"""
        from app.tools.mock_actions import get_action_metadata

        approval_actions = []
        for instruction in plan.actions:
            metadata = get_action_metadata(instruction.action.value)
            if metadata and metadata.requires_approval:
                approval_actions.append(ApprovalAction(instruction.action.value))
        plan.requires_approval = bool(approval_actions)
        plan.approval_actions = approval_actions

    # ================================================================
    # 知识库检索（严格按优先级 — Metric-driven 版本）
    # ================================================================

    async def _query_casekb(
        self,
        incident: Incident,
        triage_result: TriageResult,
    ) -> str:
        """
        优先级 1: 查询 CaseKB 中相似诊断结论的历史处置方案。

        Metric-driven: 基于 TriageResult 的诊断结论查询，而非 Incident.attack_type。
        """
        try:
            diagnosed_type = triage_result.attack_type or ""
            root_cause = triage_result.root_cause or ""

            # 构建查询: 优先使用诊断结论
            query_parts = ["历史案例"]
            if diagnosed_type:
                query_parts.append(diagnosed_type)
            if root_cause:
                query_parts.append(root_cause)
            query_parts.append("处置方案 恢复步骤")
            query = " ".join(query_parts)

            context = await self._retrieve_knowledge_tool().ainvoke({"query": query})
            if context and context.strip():
                logger.info(f"[RunbookAgent] CaseKB 命中，长度: {len(context)}")
                return context
            logger.info("[RunbookAgent] CaseKB 未命中")
        except Exception as e:
            logger.warning(f"[RunbookAgent] CaseKB 查询异常: {e}")
        return ""

    async def _query_runbookkb(
        self,
        incident: Incident,
        triage_result: TriageResult,
    ) -> str:
        """
        优先级 2: 查询 RunbookKB 的通用 SOP。
        仅在 CaseKB 无结果时调用。

        Metric-driven: 基于 TriageResult 诊断结论 + Severity 查询。
        """
        try:
            diagnosed_type = triage_result.attack_type or incident.attack_type.value
            query = (
                f"SOP 操作手册 {diagnosed_type} "
                f"故障处理流程 标准恢复策略 级别{incident.severity.value}"
            )
            context = await self._retrieve_knowledge_tool().ainvoke({"query": query})
            if context and context.strip():
                logger.info(f"[RunbookAgent] RunbookKB 命中，长度: {len(context)}")
                return context
            logger.info("[RunbookAgent] RunbookKB 未命中")
        except Exception as e:
            logger.warning(f"[RunbookAgent] RunbookKB 查询异常: {e}")
        return ""

    async def _query_topologykb(
        self,
        incident: Incident,
        triage_result: TriageResult,
    ) -> str:
        """
        优先级 3: 查询 TopologyKB 计算影响范围。

        Metric-driven: 基于诊断出的 attack_type + 资产列表查询。
        """
        try:
            assets = (
                triage_result.upstream_assets
                + triage_result.downstream_assets
                + triage_result.impact_scope
            )
            diagnosed_type = triage_result.attack_type or ""
            asset_query = " ".join(assets[:5]) if assets else diagnosed_type
            if not asset_query:
                asset_query = "铁路信号系统 拓扑"
            query = f"拓扑依赖 {asset_query} 链路 区段 上下游"
            context = await self._retrieve_knowledge_tool().ainvoke({"query": query})
            if context and context.strip():
                logger.info(f"[RunbookAgent] TopologyKB 命中，长度: {len(context)}")
                return context
        except Exception as e:
            logger.warning(f"[RunbookAgent] TopologyKB 查询异常: {e}")
        return ""

    # ================================================================
    # 辅助方法
    # ================================================================

    def _build_plan_input(
        self,
        incident: Incident,
        triage: TriageResult,
        casekb: str,
        runbook: str,
        topology: str,
    ) -> str:
        """构建计划生成的输入消息（Metric-driven 版本）"""
        parts = [
            "## 事件信息",
            f"- incident_id: {incident.incident_id}",
            f"- 来源: {incident.source.value}",
            f"- 严重级别: {incident.severity.value}",
            f"- 描述: {incident.description or '无'}",
            "",
            "## 诊断结果（TriageAgent）",
            f"- 诊断攻击/故障类型: {triage.attack_type or 'UNKNOWN'}",
            f"- 根因: {triage.root_cause}",
            f"- 级别: {triage.severity.value}",
            f"- 影响范围: {', '.join(triage.impact_scope) if triage.impact_scope else '未确定'}",
            f"- 上游资产: {', '.join(triage.upstream_assets) if triage.upstream_assets else '未确定'}",
            f"- 下游资产: {', '.join(triage.downstream_assets) if triage.downstream_assets else '未确定'}",
            f"- 置信度: {triage.confidence:.0%}",
        ]
        if triage.evidence:
            parts.append(f"- 证据: {'; '.join(triage.evidence[:5])}")

        if casekb:
            parts.append(f"\n## CaseKB 历史案例（优先参考）\n{casekb}")
        if runbook:
            parts.append(f"\n## RunbookKB 通用 SOP\n{runbook}")
        if topology:
            parts.append(f"\n## TopologyKB 拓扑信息\n{topology}")

        parts.append(
            "\n请基于以上信息生成处置计划 steps，并确定是否需要审批。"
            "确保每个步骤都可映射到可用 Mock 动作。"
        )
        return "\n".join(parts)

    def _fallback_plan(
        self,
        incident: Incident,
        triage: TriageResult,
    ) -> RunbookPlan:
        """
        基于规则的回退计划（当 LLM 不可用时）。

        Metric-driven: 基于 TriageResult 的诊断结论选择动作，
        而非直接基于 incident.attack_type。
        """
        diagnosed_type = (triage.attack_type or "").upper()
        root_cause = (triage.root_cause or "").lower()

        # 基于诊断结论的回退策略
        is_dos = "dos" in diagnosed_type or "dos" in root_cause
        is_jamming = "jamming" in diagnosed_type or "jamming" in root_cause
        is_replay = "replay" in diagnosed_type or "replay" in root_cause
        is_spoofing = "spoofing" in diagnosed_type or "spoofing" in root_cause
        is_signal_interference = "signal interference" in diagnosed_type or "interference" in root_cause

        if is_dos or is_jamming:
            actions = [
                ActionInstruction(action="block_suspicious_source", description="封禁可疑来源 IP"),
                ActionInstruction(action="switch_backup_link", description="切换到备用链路"),
                ActionInstruction(action="notify_dispatcher", description="通知调度员"),
                ActionInstruction(action="generate_ticket", description="生成工单"),
                ActionInstruction(action="verify_network_health", description="验证网络健康"),
            ]
            rollback_actions = [
                ActionInstruction(
                    action="rollback_block_suspicious_source", description="解除 IP 封禁"
                ),
                ActionInstruction(
                    action="rollback_switch_backup_link", description="恢复原始链路"
                ),
            ]
        elif is_replay or is_spoofing:
            actions = [
                ActionInstruction(action="block_suspicious_source", description="封禁可疑来源 IP"),
                ActionInstruction(action="restart_gateway", description="重启网关"),
                ActionInstruction(action="notify_dispatcher", description="通知调度员"),
                ActionInstruction(action="verify_network_health", description="验证网络健康"),
            ]
            rollback_actions = [
                ActionInstruction(
                    action="rollback_block_suspicious_source", description="解除 IP 封禁"
                ),
            ]
        elif is_signal_interference:
            actions = [
                ActionInstruction(action="switch_backup_link", description="切换到备用链路"),
                ActionInstruction(action="notify_dispatcher", description="通知调度员"),
                ActionInstruction(action="generate_ticket", description="生成工单"),
                ActionInstruction(action="verify_network_health", description="验证网络健康"),
            ]
            rollback_actions = [
                ActionInstruction(
                    action="rollback_switch_backup_link", description="恢复原始链路"
                ),
            ]
        else:
            # UNKNOWN 或其他 — 保守处置
            actions = [
                ActionInstruction(action="notify_dispatcher", description="通知调度员"),
                ActionInstruction(action="generate_ticket", description="生成工单"),
                ActionInstruction(action="verify_network_health", description="验证网络健康"),
            ]
            rollback_actions = []

        plan = RunbookPlan(
            actions=actions,
            source_kb="Fallback",
            affected_assets=(
                triage.impact_scope
                + triage.upstream_assets
                + triage.downstream_assets
            ),
            requires_approval=False,
            approval_actions=[],
            rollback_actions=rollback_actions,
        )
        self._apply_registry_metadata(plan)
        return plan
