"""
通用 Plan-Execute-Replan 状态定义
基于 LangGraph 官方教程实现

AIOPS-FUSION: 扩展支持 Recovery Mode
- 新增 recovery_attempt 字段限制无限循环
- 新增 is_recovery 标记区分正常模式和恢复模式
"""

from typing import List, TypedDict, Annotated, Optional, Dict, Any
import operator

# 整个 Agent 运行过程中要带着的一个大文件夹  input  plan past_steps response
class PlanExecuteState(TypedDict, total=False):
    """Plan-Execute-Replan 状态（扩展版，支持 Recovery Mode）"""

    # 用户输入（任务描述） — 正常模式或 Recovery 模式共用
    input: str

    # 执行计划（步骤列表）
    plan: List[str]

    # 已执行的步骤历史
    # 使用 operator.add 实现追加式更新（而非覆盖）
    past_steps: Annotated[List[tuple], operator.add]  # [("步骤1", "结果1"), ("步骤2", "结果2")] 两个结果不覆盖

    # 最终响应/报告
    response: str

    # ================================================================
    # AIOPS-FUSION: Recovery Mode 扩展字段
    # ================================================================

    # 是否为恢复模式（由 Workflow 失败触发，而非用户主动调用）
    is_recovery: bool

    # 恢复尝试次数（0-based，由 Replanner 递增，上限 MAX_RECOVERY_ATTEMPTS）
    recovery_attempt: int

    # 失败上下文（Workflow FailureContext 序列化后的 dict）
    failure_context: Optional[Dict[str, Any]]

    # Recovery 最终结果: "recovery_success" | "recovery_failed" | "safety_rollback" | "safety_escalation"
    recovery_result: str
