# ZL 项目新手解释体系

> 适用读者：刚学完 Python、有基础 AI 知识、第一次接触 ZL（STSRS Data Engineering）的开发者。
> 目的：只解释当前项目真实出现、且对理解系统有帮助的术语。没有 Web 概念，就不要硬塞 Web 概念。

## 1. 术语速查表（一句话）

| 术语 | 一句话理解 |
| --- | --- |
| 原始数据 | 项目根目录的两份 STSRS 文本监测数据（Control Center / Train） |
| Staging | 原始文本被校验后转成的类型化 Parquet |
| Schema 契约 | 一份 YAML，规定每个字段叫什么、什么类型、什么角色 |
| Key（主键） | 用来唯一定位一行数据的字段组合（Timestamp+TrainID+SignalID） |
| Alignment（对齐） | 检查两个数据源能否用 key 一一对应 |
| Trusted Dataset | 通过所有审计、且标签可信的行集合 |
| Label Quality | 把原始标签归一化成四类标准标签并隔离 unknown |
| Time Split | 按时间顺序把数据切成 train/validation/test |
| Purge Gap | 切分边界附近挖掉一段时间，防止时序泄漏 |
| Canonical Features | 按 schema 白名单选出的规范特征 |
| Encoded Features | 模型能直接吃的数值特征（数值直通 + 类别 one-hot） |
| One-hot | 把类别值变成 0/1 多列 |
| Manifest | 每次运行的 JSON 元数据（路径、指标、参数） |
| Report | 给人看的 Markdown / CSV 报告 |
| Pickle | 把训练好的模型对象序列化到文件 |
| ModelService | 进程内统一加载模型并推理的对象 |
| Registry | 模型版本到 manifest 的注册表 |
| predict_proba | 模型输出每个类别的概率 |
| Softmax | 把一组分数变成和为 1 的概率 |
| Scaler | 把特征缩放到同一量纲的预处理器 |
| JSONL | 每行一个 JSON 的日志文件 |

## 2. 术语详解

### 2.1 Schema（数据契约）

**一句话理解：** 一份“表长什么样”的说明书。

**专业定义：** YAML 定义每个字段的 `logical_type`、`physical_type`、`nullable`、`role`（key/feature/raw_label）以及 key 定义和时间格式。

**在本项目中的作用：** `schema_validation.py` 用它校验原始文件表头、类型解析和时间戳格式，然后写出 staging Parquet；`feature_engineering.py` 也用 `role == "feature"` 决定哪些列进入特征集。

**它不是什么：** 不是数据库 DDL，也不是代码里的类定义；它是流水线的单一事实来源。

**与其他模块的关系：** 被几乎所有阶段读取（`load_pipeline_config()` 统一加载）。

**源码入口：** `configs/schema/stsrs_schema.yaml`、`src/stsrs_data_engineering/config.py`。

### 2.2 Staging（暂存数据）

**一句话理解：** 原始文本第一次变成结构化 Parquet 的地方。

**专业定义：** Schema Validation 通过后，把原始 CSV 文本投影成带类型的 Parquet，并加上 `SourceDataset` 列。

**在本项目中的作用：** `data/staging/control_center.parquet` 与 `data/staging/train.parquet` 是后续所有审计的输入。

**它不是什么：** 不是最终可信数据，它只保证“格式对”，不保证“内容可信”。

**源码入口：** `src/stsrs_data_engineering/schema_validation.py`。

### 2.3 Key Audit（主键审计）

**一句话理解：** 检查主键能不能唯一标识一行。

**专业定义：** 对 key 列组合统计空值、重复、最大重复次数，输出 null/duplicate 样本，与阈值（0）比较决定是否通过。

**在本项目中的作用：** `key_audit.py` 是数据可信的第一道质量门禁。

**它不是什么：** 不判断两个表能否互相匹配（那是 Alignment）。

**源码入口：** `src/stsrs_data_engineering/key_audit.py`、`configs/quality_thresholds/data_quality.yaml`。

### 2.4 Alignment（跨数据集对齐）

**一句话理解：** 检查 Control Center 和 Train 两张表能不能按 key 一一对上。

**专业定义：** 对两个表做 key 级 FULL OUTER JOIN，把 key 分成 trusted（双方各出现 1 次）、left_only、right_only、ambiguous，并统计比例与阈值比较。

**在本项目中的作用：** 只有 `trusted_alignment_keys` 才能进入可信数据集，避免同一 key 多行导致的字段冲突。

**它不是什么：** 不是字段级对比，那是 Field Consistency。

**源码入口：** `src/stsrs_data_engineering/alignment_audit.py`。

### 2.5 Field Consistency（字段一致性）

**一句话理解：** 同一行在两个数据源里对应字段是否一致。

**专业定义：** 在 trusted 1:1 行上比较左右字段，计算匹配率；数值用绝对/相对容差，`RenewalInterval` 被排除；冲突行写样本。

**在本项目中的作用：** 保证合并后的字段值是可信的，而不是两边冲突时随便取一个。

**源码入口：** `src/stsrs_data_engineering/field_consistency.py`。

### 2.6 Label Quality（标签质量）

**一句话理解：** 把原始 AttackInfo 字符串变成四个标准攻击标签。

**专业定义：** 按 `attack_label_mapping.yaml` 归一化（trim、压缩空格、按 `/` 拆段），映射为 `Normal / DoS / Jamming / ReplayAttack`；无法映射的行写入 unknown 样本并隔离，`unknown_label_ratio_max=0.0`。

**在本项目中的作用：** 生成 `data/validated/merged/trusted_labeled_dataset.parquet`，这是训练的源头。

**源码入口：** `src/stsrs_data_engineering/label_quality.py`、`configs/labels/attack_label_mapping.yaml`。

### 2.7 Time Split（时序切分）

**一句话理解：** 按时间先后把数据切成训练、验证、测试，而不是随机切。

**专业定义：** 按 `Timestamp, TrainID, SignalID` 排序，按 70/15/15 目标行数找边界时间戳；启用 purge gap 时把边界前后 300 秒的行置空，防止相邻时间的相似样本泄漏到不同 split。

**在本项目中的作用：** `data/serving/{train,validation,test}/*.parquet` 是特征工程输入。

**它不是什么：** 不是随机切分；`leakage_controls` 明确禁止随机切分和把目标列当特征。

**源码入口：** `src/stsrs_data_engineering/time_split.py`、`configs/split_policy/time_split.yaml`。

### 2.8 Canonical Features（规范特征）

**一句话理解：** 按 schema 从白名单里选出的“干净特征”。

**专业定义：** 只保留 `role == "feature"` 的列，排除 key、raw_label 等；写出 `data/features/canonical/*.parquet` 并统计空值率。

**源码入口：** `src/stsrs_data_engineering/feature_engineering.py`。

### 2.9 Encoded Features（编码特征）

**一句话理解：** 把文字特征变成模型能算的数字。

**专业定义：** 数值列直通；类别列按 allowed_values 生成 `列名__is_值` 的 one-hot 列（如 `SignalStatus__is_Green`）；目标列 `AttackLabel` 映射成 `AttackLabelId`。

**在本项目中的作用：** `data/features/encoded/*.parquet` 直接喂给 sklearn。

**源码入口：** `src/stsrs_data_engineering/encoded_features.py`。

### 2.10 Manifest（运行元数据）

**一句话理解：** 每次运行的“存档”。

**专业定义：** JSON 文件记录 `generated_at`、`report_path`、参数、结果指标、产物路径，供后续阶段读取（例如 V2 读取 V1 manifest 获取 reference 指标）。

**它不是什么：** 不是给人读的报告；是给代码读的机器事实。

**源码入口：** `metadata/manifests/*.json`。

### 2.11 Pickle（模型文件）

**一句话理解：** Python 把训练好的模型“打包存盘”。

**专业定义：** `pickle.dump` 保存 `classifier`、`feature_columns`、`target_mapping`、`config` 等；V2 还有 `sample_hash_columns`，无 `scaler`。

**在本项目中的作用：** `models/baseline/v2_compact_top3_hist_gradient_boosting.pkl` 是最终交付物，也是 `railways_V.2` 加载的真实模型。

**它不是什么：** 不是可移植的开放格式；pickle 加载有安全风险，只能加载可信文件。

**源码入口：** `src/stsrs_data_engineering/v2_compact_tree.py`、`model_service.py`。

### 2.12 ModelService（推理服务对象）

**一句话理解：** 一个“喂原始字段，返回预测结果”的 Python 对象。

**专业定义：** 进程内对象，通过 `model_service.yaml` 注册表加载 manifest 和 pickle，用 `FeatureBuilder` 构造编码特征，执行 `predict_proba` 或 softmax，校验概率和，返回 `PredictionResult`，并写 JSONL 日志。

**它不是什么：** 不是 HTTP 服务，没有网络端口；它只负责进程内推理。

**源码入口：** `src/stsrs_data_engineering/model_service.py`、`configs/serving/model_service.yaml`。

### 2.13 predict_proba / Softmax

**一句话理解：** 模型回答“每个类别的可能性是多少”。

**专业定义：** 优先用 `predict_proba`；不支持时用 `decision_function` 后做 softmax。输出必须是一维、长度等于类别数、和为 1。

**在本项目中的作用：** `ModelService.predict()` 用 argmax 取预测标签和置信度。

**源码入口：** `src/stsrs_data_engineering/model_service.py`。

### 2.14 Scaler（特征缩放器）

**一句话理解：** 把不同尺度的数字拉到一个量纲。

**专业定义：** V0 SGD 使用 `StandardScaler`（with_mean/with_std）；V2 HistGradientBoosting 不需要 scaler，pickle 中无 scaler。

**源码入口：** `src/stsrs_data_engineering/baseline_training.py`、`model_service.py`。

### 2.15 平衡采样 / Sample Hash

**一句话理解：** 每个类别取一样多的样本，避免模型偏向多数类。

**专业定义：** 用特征 + 目标列做确定性哈希，每个类别取 `sample_per_class`（V2 为 200000）行；哈希列由 reference manifest 的 `sample_hash_columns` 固定，保证消融/重训使用同一批样本。

**源码入口：** `src/stsrs_data_engineering/v1_ablation.py`、`v2_compact_tree.py`。

### 2.16 Macro F1 / Confusion Matrix

**一句话理解：** 综合衡量模型在每个类别上的表现。

**专业定义：** Macro F1 是每个类别 F1 的算术平均；混淆矩阵统计真实标签与预测标签的交叉计数。

**在本项目中的作用：** 每个训练阶段以 `validation_macro_f1` 为主指标，并输出三份 split 的混淆矩阵 CSV。

**源码入口：** `src/stsrs_data_engineering/baseline_training.py`、`v1_tree_baseline.py`、`v1_ablation.py`、`v2_compact_tree.py`。

### 2.17 Permutation Importance / Leakage Check / SHAP

**一句话理解：** 三种“模型为什么这样判断”的检查。

**专业定义：** permutation importance 随机打乱某特征看指标下降；leakage check 比较 train 与 validation/test 的哈希重叠（exact/feature-only/conflicting）；SHAP 用 TreeExplainer 给出全局、逐类、局部贡献。

**在本项目中的作用：** V1/V2 diagnostics 与 explainability 生成 CSV 与报告，用于证明特征重要性和没有泄漏。

**源码入口：** `src/stsrs_data_engineering/v1_diagnostics.py`、`v2_diagnostics.py`、`v2_explainability.py`。

### 2.18 CLI（命令行入口）

**一句话理解：** 通过 `stsrs-data <子命令>` 跑流水线。

**专业定义：** `cli.py` 用 argparse 注册 16 个子命令，每个子命令调用对应的 `run_*` 函数，统一支持 `--project-root`。

**源码入口：** `src/stsrs_data_engineering/cli.py`。

## 3. 重点解决容易混淆的问题

### 3.1 数据工程 vs 机器学习

- 数据工程：schema、key、alignment、field consistency、label、time split、features。它**不训练模型**。
- 机器学习：V0/V1/V2 训练、评估、重要性、SHAP、泛化。它**不负责数据清洗**。
- 本项目把两者分成两个清晰阶段，且数据阶段有质量门禁。

### 3.2 离线训练 vs 在线推理

- 离线：CLI 阶段运行，产出 pickle、manifest、report。
- 在线：`ModelService.predict()` 用已经训练好的 pickle 做预测。
- 两者通过 `feature_columns` / `target_mapping` / 编码规则保持一致，保证线上特征与训练时一致。

### 3.3 Canonical vs Encoded

- Canonical：仍是人类可读的原始字段（`SignalStatus` 还是 `Green`）。
- Encoded：变成模型可算的数字（`SignalStatus__is_Green=1`）。
- 在线推理时 `FeatureBuilder` 复现的是 encoded 逻辑，而不是 canonical 文件。

### 3.4 Manifest vs Report

- Manifest：JSON，机器读，用于下一阶段找路径和指标。
- Report：Markdown/CSV，人读，用于审计和展示。
- 两者都是同一阶段产生的两种视图。

### 3.5 Pickle vs 模型

- Pickle 是“模型对象 + 契约”的容器，不是模型本身。
- 模型是里面的 `classifier`；契约是 `feature_columns`、`target_mapping` 等。

### 3.6 ModelService vs Web Service

- `ModelService` 没有网络协议，不监听端口。
- 它只是进程内对象；下游（如 `railways_V.2`）负责把它接入 HTTP/Agent 流水线。

## 4. 角色分工表

| 角色 | 初学者可以理解为 | 当前项目真实职责 | 输入 | 输出 |
| --- | --- | --- | --- | --- |
| 配置契约 | 项目的“规则书” | 定义字段、标签、阈值、模型参数 | YAML 文件 | 配置 dict |
| 数据工程师 | 清洗数据的工人 | 跑 schema/key/alignment/consistency/label/time split | 原始 TXT / staging | trusted + serving Parquet |
| 特征工程师 | 把数据变成模型输入的人 | canonical + encoded | serving Parquet | encoded Parquet + manifest |
| 算法研究员 | 训练并验证模型的人 | V0/V1/V2、消融、诊断、SHAP、泛化 | encoded + 配置 | pickle + manifest + report |
| ModelService | 模型的“店员” | 加载模型、构造特征、预测、写日志 | 原始字段 dict | `PredictionResult` / 异常 |
| 下游系统 | 模型的使用方 | 把 ZL 预测接入业务流水线 | pickle / API | 业务决策 |

## 5. 新手最容易产生的误解

1. **“原始数据很干净，直接训练就行”**：本项目一半工作都在证明数据可信；直接训练会踩 key、对齐、冲突、标签、泄漏问题。
2. **“时间切分和随机切分一样”**：不同。时间切分按排序后的行号边界并加 purge gap，防止时间相邻样本泄漏。
3. **“one-hot 是模型做的事”**：不是。本项目在 `encoded_features.py` 里显式生成 `__is_` 列。
4. **“pickle 就是模型”**：pickle 是容器，包含 classifier、feature_columns、target_mapping、config。
5. **“ModelService 是 Web 服务”**：不是，它是进程内对象，没有 HTTP 端口。
6. **“V2 用了 12 个特征”**：V2 只用 3 个（`Distance, PacketLoss, Latency`）；12 是 reference/sample_hash 的旧特征集。
7. **“macro F1 高就说明没问题”**：本项目还做 permutation importance、leakage check、SHAP、temporal/seed stability，防止“指标漂亮但不可信”。
8. **“manifest 是给人看的报告”**：manifest 是机器读的元数据，report 才是人读的。
9. **“所有模型都要 scaler”**：V0 有 StandardScaler，V2 树模型没有。
10. **“在线推理需要重新跑数据流水线”**：不需要。在线推理只走 `ModelService` 的 FeatureBuilder + 模型预测。

## 6. 如何继续深入

- 先读 `PROJECT_MENTAL_MODEL.md` 建立整体印象。
- 再读 `PROJECT_RUNTIME_STORY.md` 看一次 V2 训练与一次推理怎么跑。
- 最后读 `PROJECT_SOURCE_CODE_WALKTHROUGH.md` 深入每个模块与源码位置。
- 修改代码前，先理解 `configs/` 是“规则”，`metadata/manifests/` 是“事实”，`reports/` 是“证据”。