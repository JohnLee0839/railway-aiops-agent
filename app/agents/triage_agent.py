"""
TriageAgent — 监督学习模型结果解释 + RAG 诊断 Agent

Metric-driven AIOps 职责分工:
- AttackDetector (监督学习): 回答 "What happened?" (分类预测)
- TriageAgent (LLM + RAG):    回答 "Why? Impact? How to fix?" (解释 + 诊断报告)

职责:
1. 接收 AttackPrediction (监督学习模型输出)
2. 解释模型预测 — 为什么是这个攻击类型？
3. 分析 metrics_snapshot 中的具体指标异常证据
4. 查询 TopologyKB / CaseKB / RunbookKB 获取上下文
5. 评估影响范围 + 推荐处置方案
6. 输出结构化 TriageResult
"""

from textwrap import dedent
from typing import Optional, Dict, Any, List
from langchain_core.prompts import ChatPromptTemplate
from langchain_qwq import ChatQwen
from loguru import logger

from app.config import config
from app.models.incident import Incident, TriageResult, Severity, AttackType


# TriageAgent 提示词（监督学习 + LLM 协作版本）
TRIAGE_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        dedent("""
            你是一个铁路信号系统安全分析专家（TriageAgent）。

            ## 你的角色定位

            你与 AttackDetector（监督学习模型）协作工作：
            - **AttackDetector** 回答 "What happened?" — 基于结构化特征预测攻击类型
            - **你（TriageAgent）** 回答 "Why? Impact? How to fix?" — 解释模型结果 + 诊断报告

            ## 核心任务

            1. **验证模型预测** — 检查模型预测的攻击类型是否与指标异常模式一致
            2. **解释根因** — 基于指标证据说明为什么发生了该攻击
            3. **评估影响** — 结合 TopologyKB 判断影响范围（列车/信号设备/区段）
            4. **推荐处置** — 基于 RunbookKB / CaseKB 推荐最佳响应方案
            5. **评估置信度** — 综合模型置信度 + 指标证据充分程度 + KB 匹配度

            ## 攻击类型解释指南

            当模型预测某攻击类型时，你应该从指标角度解释为什么：

            | 攻击类型 | 典型指标证据 | 根因解释方向 |
            |---------|------------|------------|
            | DoS | PacketLoss极高 + Latency极高 + Burstiness异常 | 通信信道被大量无效请求淹没 |
            | Jamming | PacketLoss持续升高 + SignalStatus异常 | 无线信号被干扰，信噪比急剧下降 |
            | Replay | RenewalInterval来源冲突 + 状态不一致 | 控制中心与列车观测值不匹配 |
            | Spoofing | Speed/Distance明显异常 | 伪造的列车位置/速度数据 |
            | Signal Interference | SignalStatus RED/OFFLINE + OverlapStatus ABNORMAL | 信号设备物理/逻辑故障 |

            **重要**: 如果模型预测为 UNKNOWN 或置信度极低，且指标确实异常，
            你应该基于指标模式给出你的判断，并标注 confidence 较低。
            如果模型预测与指标证据矛盾，应指出矛盾并给出你的独立判断。

            ## 严重级别评估指南

            - P1: SignalStatus=RED/DANGER, OverlapStatus=ABNORMAL, Speed 异常+信号异常
            - P2: PacketLoss > 0.5, Latency > 200ms, RenewalInterval来源冲突
            - P3: PacketLoss > 0.1, Latency > 100ms, Burstiness > 0.5
            - P4: 指标轻微偏差或无异常

            输出字段:
            - root_cause: 根因分析（详细描述，解释 Why）
            - attack_type: 最终诊断攻击类型（综合模型预测 + 独立判断）
            - severity: P1/P2/P3/P4（综合 SeverityEngine + 独立判断）
            - impact_scope: 受影响范围列表
            - upstream_assets / downstream_assets: 基于拓扑的上下游资产
            - confidence: 0.0-1.0（综合模型置信度 + 证据 + KB）
            - evidence: 诊断证据列表（每条对应一个指标异常观察）
        """).strip(),
    ),
    ("placeholder", "{messages}"),
])


class TriageAgent:
    """
    TriageAgent — 监督学习模型结果解释 + RAG 诊断 Agent。

    协作模式:
    - AttackDetector 提供 attack_prediction (What happened?)
    - TriageAgent 解释 + 诊断 (Why? Impact? How to fix?)

    核心变化（从纯 LLM 诊断 → 模型 + LLM 协作）:
    - 不再从零诊断 attack_type
    - 接收 AttackPrediction 作为输入
    - LLM 负责解释模型结果、查找证据、评估影响、推荐方案
    """

    def __init__(self):
        self.llm = ChatQwen(
            model=config.rag_model,
            api_key=config.dashscope_api_key,
            temperature=0,
        )
        self.chain = TRIAGE_PROMPT | self.llm.with_structured_output(TriageResult)

    @staticmethod
    def _retrieve_knowledge_tool():
        from app.tools import retrieve_knowledge

        return retrieve_knowledge

    async def triage(self, incident: Incident) -> TriageResult:
        """
        解释模型预测 + 综合诊断 → TriageResult。

        协作流程:
        1. 提取 metrics_snapshot + attack_prediction
        2. 分析指标异常模式（验证模型预测的一致性）
        3. 多维度 KB 查询（基于预测的 attack_type + 异常模式）
        4. LLM 综合诊断（解释 Why + 评估 Impact + 推荐 How）

        Args:
            incident: 归一化后的事件（含 attack_prediction）

        Returns:
            TriageResult（含 attack_type, root_cause, severity, confidence, evidence）
        """
        prediction = incident.attack_prediction
        pred_type = prediction.get("attack_type", "UNKNOWN") if prediction else "UNKNOWN"
        pred_confidence = prediction.get("confidence", 0.0) if prediction else 0.0

        logger.info(
            f"[TriageAgent] 开始诊断: incident_id={incident.incident_id}, "
            f"model_prediction={pred_type}, model_confidence={pred_confidence}, "
            f"has_metrics={incident.metrics_snapshot is not None}, "
            f"train_id={incident.metadata.train_id}, "
            f"signal_id={incident.metadata.signal_id}"
        )

        # 1. 分析指标异常模式
        anomaly_analysis = self._analyze_metric_anomalies(incident)

        # 2. 查询 TopologyKB（基于 TrainID/SignalID）
        topology_context = await self._query_topology(incident)

        # 3. 查询 CaseKB（基于模型预测的 attack_type + 异常模式）
        case_context = await self._query_casekb_by_prediction(
            prediction, anomaly_analysis, incident
        )

        # 4. 构建诊断输入（含模型预测）
        diagnosis_input = self._build_diagnosis_input(
            incident, prediction, anomaly_analysis, topology_context, case_context
        )

        # 5. 调用 LLM 生成诊断结果
        try:
            result = await self.chain.ainvoke({
                "messages": [("user", diagnosis_input)],
            })

            if isinstance(result, TriageResult):
                triage_result = result
            else:
                triage_result = TriageResult(**result)

            logger.info(
                f"[TriageAgent] 诊断完成: "
                f"attack_type={triage_result.attack_type or 'UNKNOWN'}, "
                f"root_cause={triage_result.root_cause}, "
                f"severity={triage_result.severity.value if triage_result.severity else 'UNKNOWN'}, "
                f"confidence={triage_result.confidence}"
            )
            return triage_result

        except Exception as e:
            logger.error(f"[TriageAgent] LLM 调用失败: {e}", exc_info=True)
            return self._fallback_triage(incident)

    # ================================================================
    # 指标异常分析（新增 — Metric-driven 核心）
    # ================================================================

    def _analyze_metric_anomalies(self, incident: Incident) -> Dict[str, Any]:
        """
        分析 metrics_snapshot 中的指标异常模式。

        Returns:
            异常分析结果字典:
            {
                "abnormal_metrics": ["packet_loss", "latency", ...],
                "patterns": ["high_packet_loss", "high_latency", ...],
                "summary": "PacketLoss 50%, Latency 250ms — 典型的通信异常模式",
                "raw_metrics": {...},
            }
        """
        metrics_snapshot = incident.metrics_snapshot
        if not metrics_snapshot:
            return {
                "abnormal_metrics": [],
                "patterns": ["no_metrics_available"],
                "summary": "无监测指标数据",
                "raw_metrics": {},
            }

        # 提取扁平化指标
        metrics = self._flatten_metrics(metrics_snapshot)

        abnormal_metrics = []
        patterns = []

        # 分析每个指标
        packet_loss = self._get_float(metrics, "packet_loss")
        latency = self._get_float(metrics, "latency")
        renewal = self._get_float(metrics, "renewal_interval")
        burstiness = self._get_float(metrics, "burstiness")
        speed = self._get_float(metrics, "speed")
        signal_status = str(metrics.get("signal_status", "")).strip()
        overlap_status = str(metrics.get("overlap_status", "")).strip()

        # 丢包异常
        if packet_loss is not None:
            if packet_loss > 0.5:
                abnormal_metrics.append("packet_loss")
                patterns.append("critical_packet_loss")
            elif packet_loss > 0.1:
                abnormal_metrics.append("packet_loss")
                patterns.append("moderate_packet_loss")

        # 延迟异常
        if latency is not None:
            if latency > 200:
                abnormal_metrics.append("latency")
                patterns.append("critical_latency")
            elif latency > 100:
                abnormal_metrics.append("latency")
                patterns.append("moderate_latency")

        # 续期间隔异常
        if renewal is not None and renewal > 5000:
            abnormal_metrics.append("renewal_interval")
            patterns.append("abnormal_renewal_interval")

        # 突发度异常
        if burstiness is not None and burstiness > 0.5:
            abnormal_metrics.append("burstiness")
            patterns.append("high_burstiness")

        # 信号状态异常
        if signal_status.upper() in ("RED", "DANGER", "OFFLINE"):
            abnormal_metrics.append("signal_status")
            patterns.append("signal_status_abnormal")

        # 联锁状态异常
        if overlap_status.upper() in ("ABNORMAL", "CONFLICT", "ERROR"):
            abnormal_metrics.append("overlap_status")
            patterns.append("overlap_status_abnormal")

        # 速度异常
        if speed is not None and (speed < 0 or speed > 350):
            abnormal_metrics.append("speed")
            patterns.append("abnormal_speed")

        # ---- 来源冲突检测（Multi-Source 异常证据） ----
        source_conflicts = self._detect_source_conflicts(metrics)
        if source_conflicts:
            abnormal_metrics.append("source_conflict")
            patterns.append("source_metric_conflict")

        # 生成摘要
        summary_parts = []
        if patterns:
            metric_details = []
            for m in abnormal_metrics:
                val = self._get_float(metrics, m) if m not in ("signal_status", "overlap_status") else metrics.get(m, "N/A")
                if val is not None:
                    metric_details.append(f"{m}={val}")
            summary_parts.append(f"异常指标: {', '.join(metric_details)}")
            summary_parts.append(f"异常模式: {', '.join(patterns)}")
        else:
            summary_parts.append("所有指标在正常范围内")

        # 来源冲突详情
        if source_conflicts:
            conflict_detail = "; ".join(source_conflicts)
            summary_parts.append(f"来源冲突: {conflict_detail}")

        summary = "; ".join(summary_parts) if summary_parts else "指标正常"

        return {
            "abnormal_metrics": abnormal_metrics,
            "patterns": patterns,
            "summary": summary,
            "raw_metrics": metrics,
            "source_conflicts": source_conflicts,
        }

    # ================================================================
    # 知识库查询（基于异常模式）
    # ================================================================

    async def _query_topology(self, incident: Incident) -> str:
        """查询 TopologyKB 获取拓扑影响信息"""
        try:
            query_parts = []
            if incident.metadata.train_id:
                query_parts.append(f"train {incident.metadata.train_id}")
            if incident.metadata.signal_id:
                query_parts.append(f"signal {incident.metadata.signal_id}")
            if not query_parts:
                return ""
            query = f"拓扑信息 信号设备 上下游依赖 {' '.join(query_parts)}"
            context = await self._retrieve_knowledge_tool().ainvoke({"query": query})
            if context and context.strip():
                return context
        except Exception as e:
            logger.warning(f"[TriageAgent] TopologyKB 查询失败: {e}")
        return ""

    async def _query_casekb_by_prediction(
        self,
        prediction: Optional[Dict[str, Any]],
        anomaly_analysis: Dict[str, Any],
        incident: Incident,
    ) -> str:
        """
        基于模型预测 + 异常模式查询 CaseKB。

        优先使用模型预测的 attack_type 进行精确匹配，
        回退到异常模式关键词模糊查询。
        """
        patterns = anomaly_analysis.get("patterns", [])

        # 优先: 基于模型预测的 attack_type 精确查询
        if prediction and prediction.get("attack_type") not in (None, "UNKNOWN", ""):
            pred_type = prediction["attack_type"]
            try:
                query = f"历史案例 铁路信号安全 {pred_type} 攻击 诊断 处置"
                if incident.metadata.signal_id:
                    query += f" 信号设备 {incident.metadata.signal_id}"
                context = await self._retrieve_knowledge_tool().ainvoke({"query": query})
                if context and context.strip():
                    logger.info(
                        f"[TriageAgent] CaseKB 命中 (基于模型预测 {pred_type}), "
                        f"长度: {len(context)}"
                    )
                    return context
            except Exception as e:
                logger.warning(f"[TriageAgent] CaseKB 精确查询失败: {e}")

        # 回退: 基于异常模式模糊查询
        if not patterns or patterns == ["no_metrics_available"]:
            try:
                query = f"历史案例 铁路信号系统 故障"
                if incident.metadata.train_id:
                    query += f" 列车 {incident.metadata.train_id}"
                context = await self._retrieve_knowledge_tool().ainvoke({"query": query})
                if context and context.strip():
                    return context
            except Exception as e:
                logger.warning(f"[TriageAgent] CaseKB 模糊查询失败: {e}")
            return ""

        pattern_keywords = {
            "critical_packet_loss": "高丢包率",
            "moderate_packet_loss": "丢包",
            "critical_latency": "高延迟",
            "moderate_latency": "延迟",
            "abnormal_renewal_interval": "信号续期间隔异常",
            "high_burstiness": "流量突发",
            "signal_status_abnormal": "信号状态异常",
            "overlap_status_abnormal": "联锁状态异常",
            "abnormal_speed": "速度异常",
            "source_metric_conflict": "来源冲突",
        }

        query_keywords = []
        for p in patterns:
            kw = pattern_keywords.get(p, "")
            if kw and kw not in query_keywords:
                query_keywords.append(kw)

        if incident.metadata.signal_id:
            query_keywords.append(f"信号设备 {incident.metadata.signal_id}")

        query = f"历史案例 铁路信号安全 故障诊断 {' '.join(query_keywords[:5])}"

        try:
            context = await self._retrieve_knowledge_tool().ainvoke({"query": query})
            if context and context.strip():
                logger.info(f"[TriageAgent] CaseKB 命中 (基于异常模式), 长度: {len(context)}")
                return context
            logger.info("[TriageAgent] CaseKB 未命中")
        except Exception as e:
            logger.warning(f"[TriageAgent] CaseKB 查询异常: {e}")
        return ""

    # ================================================================
    # 诊断输入构建
    # ================================================================

    def _build_diagnosis_input(
        self,
        incident: Incident,
        prediction: Optional[Dict[str, Any]],
        anomaly_analysis: Dict[str, Any],
        topology_context: str,
        case_context: str,
    ) -> str:
        """构建 LLM 诊断输入（监督学习 + LLM 协作版本）"""
        parts = [
            "## 事件信息",
            f"- incident_id: {incident.incident_id}",
            f"- 来源: {incident.source.value}",
            f"- 时间: {incident.timestamp.isoformat()}",
            f"- 描述: {incident.description or '无'}",
        ]

        meta = incident.metadata
        if meta.train_id:
            parts.append(f"- 列车ID: {meta.train_id}")
        if meta.signal_id:
            parts.append(f"- 信号ID: {meta.signal_id}")
        if meta.control_center:
            parts.append(f"- 控制中心: {meta.control_center}")
        if meta.source_ip:
            parts.append(f"- 源IP: {meta.source_ip}")

        # ---- 监督学习模型预测（核心新增） ----
        if prediction and prediction.get("attack_type") not in (None, "UNKNOWN", ""):
            parts.append(f"\n## 监督学习模型预测 (AttackDetector)")
            parts.append(f"- 预测攻击类型: **{prediction['attack_type']}**")
            parts.append(f"- 模型置信度: {prediction.get('confidence', 0):.1%}")
            parts.append(f"- 模型版本: {prediction.get('model_version', 'unknown')}")
            probs = prediction.get("probabilities", {})
            if probs:
                parts.append(f"- 各类别概率: {probs}")
            parts.append(f"\n请基于以上模型预测，结合指标异常模式进行验证和解释。")
        elif prediction:
            parts.append(f"\n## 监督学习模型预测 (AttackDetector)")
            parts.append(f"- 预测: **UNKNOWN** (置信度: {prediction.get('confidence', 0):.1%})")
            parts.append(f"- 模型无法确定攻击类型，请你基于指标进行独立判断")
        else:
            parts.append(f"\n## 监督学习模型预测")
            parts.append(f"- 无模型预测结果，请基于指标进行独立判断")

        # 指标异常分析
        parts.append(f"\n## 监测指标异常分析")
        parts.append(f"- {anomaly_analysis['summary']}")

        # 详细指标值
        raw_metrics = anomaly_analysis.get("raw_metrics", {})
        if raw_metrics:
            parts.append("\n### 原始指标值")
            for key, value in sorted(raw_metrics.items()):
                if value is not None and key != "source_metrics":
                    parts.append(f"- {key}: {value}")

        if anomaly_analysis.get("abnormal_metrics"):
            parts.append(f"\n### 异常指标列表")
            parts.append(f"- {', '.join(anomaly_analysis['abnormal_metrics'])}")

        if anomaly_analysis.get("patterns"):
            parts.append(f"\n### 异常模式")
            parts.append(f"- {', '.join(anomaly_analysis['patterns'])}")

        # 来源冲突详情
        source_conflicts = anomaly_analysis.get("source_conflicts", [])
        if source_conflicts:
            parts.append(f"\n### 来源间指标冲突")
            for conflict in source_conflicts:
                parts.append(f"- {conflict}")

        # 来源特定指标
        source_metrics = raw_metrics.get("source_metrics", {})
        if source_metrics:
            parts.append(f"\n### 来源特定指标")
            for src_name, src_data in sorted(source_metrics.items()):
                if isinstance(src_data, dict):
                    items = ", ".join(f"{k}={v}" for k, v in src_data.items())
                    parts.append(f"- {src_name}: {items}")

        # KB 上下文
        if topology_context:
            parts.append(f"\n## TopologyKB 拓扑信息\n{topology_context}")

        if case_context:
            parts.append(f"\n## CaseKB 历史案例\n{case_context}")

        # 诊断指令（协作模式）
        parts.append(
            "\n## 诊断任务\n"
            "请基于以上信息进行综合诊断。注意 AttackDetector 已给出初步预测，"
            "你的角色是**验证、解释和补充**：\n\n"
            "1. **验证模型预测** — 检查 AttackDetector 预测的 attack_type "
            "是否与指标异常模式一致。如果不一致，请说明矛盾并给出你的判断。\n"
            "2. **解释根因 (root_cause)** — 基于指标证据 + KB 上下文，详细解释"
            "为什么发生了该攻击/故障。\n"
            "3. **评估严重级别 (severity)** — 综合 SeverityEngine 的评估结果"
            "和你的独立判断。\n"
            "4. **分析影响范围 (impact_scope)** — 基于 TopologyKB 判断"
            "哪些列车、信号设备、区段受影响。\n"
            "5. **列出证据 (evidence)** — 每条证据对应一个指标异常观察，"
            "建立从指标到诊断结论的逻辑链。\n"
            "6. **评估置信度 (confidence)** — 综合模型置信度、证据充分程度、"
            "KB 匹配度，给出 0.0-1.0 的综合置信度。\n\n"
            "注意: 如果模型预测为 UNKNOWN 但指标确实异常，"
            "基于指标模式给出你的独立判断。如果所有指标正常，确认无攻击。"
        )
        return "\n".join(parts)

    # ================================================================
    # Fallback 诊断
    # ================================================================

    def _fallback_triage(self, incident: Incident) -> TriageResult:
        """基于规则的回退诊断（当 LLM 不可用时）"""
        from app.events.severity_engine import SeverityEngine

        engine = SeverityEngine()
        severity = engine.evaluate(incident)

        anomaly = self._analyze_metric_anomalies(incident)
        patterns = anomaly.get("patterns", [])

        impact = []
        assets_up = []
        assets_down = []

        if incident.metadata.train_id:
            impact.append(incident.metadata.train_id)
        if incident.metadata.signal_id:
            impact.append(incident.metadata.signal_id)

        # 基于异常模式做基础判断
        root_cause = "基于规则的自动诊断 — 无法确定具体攻击类型"
        attack_type = None

        if "critical_packet_loss" in patterns and "critical_latency" in patterns:
            root_cause = "严重通信异常: 高丢包+高延迟，疑似 DoS 或 Jamming 攻击"
        elif "abnormal_renewal_interval" in patterns:
            root_cause = "信号续期间隔异常，疑似 Replay Attack 或通信故障"
        elif "signal_status_abnormal" in patterns:
            root_cause = "信号设备状态异常，影响列车运行安全"
        elif not patterns or patterns == ["no_metrics_available"]:
            root_cause = "缺少监测指标数据，无法进行自动诊断"

        return TriageResult(
            root_cause=root_cause,
            attack_type=attack_type,
            severity=severity,
            impact_scope=impact,
            upstream_assets=assets_up,
            downstream_assets=assets_down,
            confidence=0.5,
            evidence=[
                f"异常模式: {', '.join(patterns)}" if patterns else "无异常模式",
                "基于规则的自动诊断（LLM 不可用）",
            ],
        )

    # ================================================================
    # 工具函数
    # ================================================================

    @staticmethod
    def _flatten_metrics(metrics_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """从 metrics_snapshot 中提取扁平化指标字典"""
        result: Dict[str, Any] = {}

        # PrometheusMetricSnapshot 格式
        inner = metrics_snapshot.get("metrics", {})
        if isinstance(inner, dict) and inner:
            result.update(inner)
        else:
            # RailMetricRecord 格式
            for key in (
                "speed", "distance", "packet_loss", "latency",
                "renewal_interval", "burstiness", "overlap_count",
                "signal_status", "overlap_status",
            ):
                if key in metrics_snapshot and metrics_snapshot[key] is not None:
                    result[key] = metrics_snapshot[key]

        # 从 labels 中提取状态指标
        labels = metrics_snapshot.get("labels", {})
        for key in ("signal_status", "overlap_status"):
            if key in labels and labels[key] is not None:
                if key not in result:
                    result[key] = labels[key]

        # 传递 source_metrics（Multi-Source 冲突数据）
        source_metrics = metrics_snapshot.get("source_metrics", {})
        if source_metrics and isinstance(source_metrics, dict) and source_metrics:
            result["source_metrics"] = source_metrics

        return result

    @staticmethod
    def _detect_source_conflicts(metrics: Dict[str, Any]) -> List[str]:
        """
        检测 source_metrics 中的来源间指标差异。

        当同一指标 (如 renewal_interval) 在不同来源 (control_center, train)
        存在不同值时，这本身就是异常证据 — 可能表明信号篡改、Replay Attack 等。

        Returns:
            冲突描述列表，如 ["renewal_interval: control_center=0 vs train=170"]
        """
        conflicts = []
        source_metrics = metrics.get("source_metrics", {})
        if not source_metrics or not isinstance(source_metrics, dict):
            return conflicts

        for metric_name in ("renewal_interval", "packet_loss", "latency",
                            "burstiness", "signal_status", "overlap_status", "speed"):
            source_values: Dict[str, Any] = {}
            for src_name, src_data in source_metrics.items():
                if isinstance(src_data, dict) and metric_name in src_data:
                    source_values[src_name] = src_data[metric_name]

            if len(source_values) >= 2:
                unique = set(source_values.values())
                if len(unique) >= 2:
                    detail = ", ".join(f"{s}={v}" for s, v in sorted(source_values.items()))
                    conflicts.append(f"{metric_name}: {detail}")

        return conflicts

    @staticmethod
    def _get_float(metrics: Dict[str, Any], key: str) -> Optional[float]:
        """从指标字典安全提取浮点值"""
        value = metrics.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
