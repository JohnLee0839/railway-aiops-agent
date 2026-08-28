# STSRS 项目简历表述

## 仓库查验结论

- 原始数据规模：`control_center` 与 `train` 两个数据源各 10,000,000 行，合计 20,000,000 行。
- 可信对齐样本：基于 `Timestamp, TrainID, SignalID` 做跨源 1:1 对齐后，得到 9,992,612 条可信样本；重复键组 3,694 组被审计隔离。
- 标签任务：`Normal, DoS, Jamming, ReplayAttack` 四分类，未知标签行数为 0。
- 时序切分：按 `Timestamp, TrainID, SignalID` 排序后做 70%/15%/15% 划分，并启用 300 秒 purge gap；训练/验证/测试行数分别为 6,994,527 / 1,498,290 / 1,498,591。
- 特征体系：V1 使用 12 个编码后特征，V2 使用 `Distance, PacketLoss, Latency` 3 个核心特征，特征减少 75%。
- 模型训练：SGD 线性基线使用分批 `partial_fit`；HistGradientBoosting 使用 DuckDB hash 按类别确定性采样，每类 200,000、总计 800,000 条训练样本。
- 最终指标：V2 轻量模型验证集 Macro-F1 为 0.998750，测试集 Macro-F1 为 0.998903；相对 V1 12 特征模型验证/测试 Macro-F1 下降约 0.000395/0.000285。

## 修改后简历版本

**项目介绍：**

面向铁路通信网络攻击检测，基于铁路数字孪生系统生成的两源合计 2,000 万行通信时序数据，构建覆盖数据接入、Schema 校验、主键审计、跨源对齐、字段一致性校验、标签治理、时序防泄漏切分、特征工程、模型训练、模型解释与特征筛选的机器学习 Pipeline。针对 Normal、DoS、Jamming、Replay Attack 四类场景，在 9,992,612 条可信 1:1 对齐样本上完成建模验证；结合 HistGradientBoosting、Permutation Importance、SHAP 与特征消融实验，将输入特征由 12 维精简至 3 维，特征数量减少 75%，同时保持测试集 Macro-F1 0.9989 的轻量化检测效果。

**技术架构：** DuckDB、SQL、Parquet、Python、scikit-learn、HistGradientBoosting、SGDClassifier、SHAP

**职责描述：**

- 设计并实现铁路通信时序数据处理管线，统一接入 `control_center` 与 `train` 两个 1,000 万行数据源，基于 DuckDB SQL 完成 CSV 批量解析、Parquet 落盘、特征查询与报告生成，支撑 Normal、DoS、Jamming、Replay Attack 四分类任务。
- 构建 Schema 校验、主键审计、跨源 1:1 对齐、字段一致性校验、标签质量检查、特征质量审计等数据治理流程；对 `Timestamp, TrainID, SignalID` 候选主键执行空值/重复约束，识别并隔离 3,694 组重复键，产出 9,992,612 条可信对齐样本。
- 实现跨源字段一致性校验，对 Speed、Distance、PacketLoss、Latency、AttackInfo 等关键字段使用 `1e-9` 数值容差与类别一致性规则进行比对，最终可信样本字段冲突数为 0，未知标签数为 0。
- 设计 70%/15%/15% 时序数据集划分策略，引入 300 秒 purge gap、跨分割样本重叠检查与原始标识符隔离机制，生成训练/验证/测试集 6,994,527 / 1,498,290 / 1,498,591 行，降低时序任务中的数据泄漏风险。
- 搭建 SGDClassifier 线性基线与 HistGradientBoosting 树模型基线；SGD 使用分批 `partial_fit` 支持大规模训练，GBDT 基于 DuckDB hash 按类别确定性采样，每类 200,000 条、总计 800,000 条样本训练，保证采样可复现。
- 完成 7 个数值特征与 2 个类别特征的 One-Hot 编码，形成 12 维编码特征；结合 Permutation Importance、SHAP 全局/逐类/局部解释与 6 组消融实验，筛选出 `Distance, PacketLoss, Latency` 三个核心特征并固化为 V2 轻量模型。
- 构建 Accuracy、Macro-F1、Weighted-F1、Balanced Accuracy、逐类 Precision/Recall/F1 与混淆矩阵评估体系，支持 V1 全特征模型与 V2 轻量模型自动化对比；最终 V2 在验证集/测试集 Macro-F1 分别达到 0.998750 / 0.998903，相对 12 维 V1 模型仅下降 0.000395 / 0.000285。
- 结合 5 个随机种子与 sample salt 稳定性实验、验证/测试集时间分桶分析和跨分割 hash 重叠检查，验证模型结论的可复现性与泛化稳定性。

## 主要证据文件

- `reports/data_validation/schema_report.md`
- `reports/data_validation/alignment_audit_report.md`
- `reports/data_validation/field_consistency_report.md`
- `reports/data_validation/label_quality_report.md`
- `reports/data_validation/time_split_report.md`
- `reports/data_validation/feature_quality_report.md`
- `reports/model_training/v1_tree_baseline_report.md`
- `reports/model_training/v1_ablation_report.md`
- `reports/model_training/v2_compact_tree_report.md`
- `reports/model_training/v2_explainability_report.md`
- `reports/model_training/v2_generalization_validation_report.md`
