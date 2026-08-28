# ZL 项目真实请求运行故事

> 本文用两个最典型的真实“请求”讲清楚 ZL 如何从数据跑到模型再到推理：
> 1. **一次 V2 Compact Tree 训练**（最能体现数据工程 + 模型训练价值）
> 2. **一次 ModelService 推理**（最能体现与下游 `railways_V.2` 的接入点）
> 事实依据：当前源码（`src/stsrs_data_engineering/`）与配置（`configs/`），不是文档推测。

## 1. 主故事：一次 V2 Compact Tree 训练

### ① 发生了什么

研究员想把 V1 的 12 特征树模型精简成更易解释的 3 特征模型。V1 Ablation 已证明 `Distance / PacketLoss / Latency` 保留几乎全部信号，因此正式训练 V2。

### ② 输入是什么

- 数据：`data/features/encoded/train.parquet`、`validation.parquet`、`test.parquet`
- 配置：`configs/modeling/v2_compact_top3_hist_gradient_boosting.yaml`
- 参考事实：`metadata/manifests/v1_tree_baseline_manifest.json`、`v1_ablation_manifest.json`

### ③ 谁处理，做了什么

`scripts/run_v2_compact_tree.py` 调用 `v2_compact_tree.run_v2_compact_tree()`：

1. 读取配置，得到 `feature_columns = [Distance, PacketLoss, Latency]`、`sample_per_class=200000`、HGB 超参。
2. 从 V1 / V1 Ablation manifests 读取 `reference_model_manifest`、`reference_ablation_manifest` 与 `sample_hash_columns`（12 个字段）。
3. `_load_balanced_training_sample()` 用哈希从全量训练集（6,994,527 行）平衡采样 800,000 行（每类 200,000）。
4. `classifier.fit(x_train, y_train)` 训练 `HistGradientBoostingClassifier`。
5. 对 train/validation/test 三份数据分别 `_evaluate_split()`，计算 accuracy、balanced accuracy、macro precision/recall/F1，并写混淆矩阵 CSV。
6. `pickle.dump()` 保存模型文件：
   - `models/baseline/v2_compact_top3_hist_gradient_boosting.pkl`
   - 载荷：`feature_columns`、`target_mapping`、`sample_hash_columns`、`classifier`、`config` 等
7. 写报告 `reports/model_training/v2_compact_tree_report.md` 与 manifest `metadata/manifests/v2_compact_tree_manifest.json`。

### ④ 得到了什么结果

| 指标 | 值 |
| --- | --- |
| validation macro F1 | 0.9987499521249175 |
| test macro F1 | 0.9989026613118872 |
| feature_count | 3 |
| feature_reduction_ratio | 0.75（12 → 3） |
| sampled_training_rows | 800,000 |
| model_size_bytes | 1,055,664 |

### ⑤ 为什么进入下一步

模型文件 + manifest 成为 `ModelService` 的注册项（`model_service.yaml` 中 `V2` 指向 `v2_compact_tree_manifest.json`），从而可以进入推理阶段。

## 2. 次故事：一次 ModelService 推理

### ① 发生了什么

下游系统（或研究员）想用一条真实指标做攻击类型预测。

### ② 输入是什么

原始字段 dict，例如：

```json
{
  "Speed": 40.9032489504979,
  "Distance": 3202.71623806719,
  "Location": 0.202716238067185,
  "SignalStatus": "Green",
  "OverlapStatus": "Yes",
  "OverlapCount": 1.0,
  "PacketLoss": 95.9976141505673,
  "Latency": 246.873299109449,
  "Burstiness": 3.49959352295494
}
```

> 数值取自 `logs/inference/model_service.jsonl` 中真实 V2 smoke test 的 `validated_raw_input`。V2 实际只校验 `Distance, PacketLoss, Latency`，其余字段在特征构造时不会使用。

### ③ 谁处理，做了什么

`scripts/run_model_service_smoke_test.py` 或任何调用方：

1. `build_model_service(PROJECT_ROOT)` 构造 `ModelService`。
2. `service.predict(raw_input=..., model_version="V2")`。
3. `load_model("V2")` 从 registry 找到 manifest，读 pickle，缓存 `LoadedModelArtifact`。
4. `FeatureBuilder.build_features(raw_input, ["Distance","PacketLoss","Latency"])`：校验字段存在、数值有限、类别合法；构造编码特征 dict。
5. 构造 numpy 矩阵 `[[Distance, PacketLoss, Latency]]`；V2 无 scaler，直接进入 `predict_proba`。
6. 概率校验（一维、长度=4、和为 1），`argmax` 得到 `predicted_label` 与 `confidence`。
7. 返回 `PredictionResult`，并把成功日志写入 `logs/inference/model_service.jsonl`。

### ④ 得到了什么结果

```json
{
  "request_id": "...",
  "model_version": "V2",
  "model_name": "v2_compact_top3_hist_gradient_boosting",
  "predicted_label": "DoS",
  "predicted_label_id": 1,
  "confidence": 0.999999,
  "probabilities": {"Normal": 0.000000, "DoS": 0.999999, "Jamming": 0.000000, "ReplayAttack": 0.000000},
  "encoded_features": {"Distance": 3202.71623806719, "PacketLoss": 95.9976141505673, "Latency": 246.873299109449}
}
```

## 3. 源码级调用链

### 3.1 V2 训练

```text
scripts/run_v2_compact_tree.py
  → stsrs_data_engineering.v2_compact_tree.run_v2_compact_tree()
      → v1_ablation._build_hist_gradient_boosting_classifier()
      → v1_ablation._load_balanced_training_sample()
      → classifier.fit()
      → v1_ablation._evaluate_split() × train/validation/test
      → pickle.dump(models/baseline/v2_compact_top3_hist_gradient_boosting.pkl)
      → 写 reports/model_training/v2_compact_tree_report.md
      → 写 metadata/manifests/v2_compact_tree_manifest.json
```

### 3.2 ModelService 推理

```text
scripts/run_model_service_smoke_test.py（或下游调用）
  → model_service.build_model_service()
  → ModelService.predict(raw_input, model_version)
      → ModelService.load_model("V2")
          → model_service.yaml registry → v2_compact_tree_manifest.json
          → pickle.load(v2_compact_top3_hist_gradient_boosting.pkl)
      → FeatureBuilder.build_features(raw_input, feature_columns)
      → np.asarray([...])
      → predict_proba() / decision_function + softmax
      → _validate_probabilities()
      → PredictionResult
      → _log_inference() → logs/inference/model_service.jsonl
```

## 4. 异常路径

| 场景 | 行为 |
| --- | --- |
| 原始字段缺失（如缺 Distance） | `FeatureBuilder` 抛 `ValueError("Missing required input field: ...")`，`ModelService` 写 failure 日志后重新抛出 |
| 数值字段是 bool / 非有限值 | 抛 `ValueError`，不静默转换 |
| 类别值不在 allowed_values | 抛 `ValueError`，列出允许值 |
| 模型版本不存在 | `load_model` 抛 `ValueError("Unknown model version ...")` |
| pickle 文件缺失 / 损坏 | `pickle.load` 异常，`ModelService` 写 failure 日志后抛出 |
| 概率和不为 1 / 长度不对 | `_validate_probabilities` 抛 `ValueError` |
| 训练数据为空 | `time_split` 抛 `ValueError("The trusted labeled dataset is empty; ...")` |
| 标签无法映射 | `label_quality` 把行写入 unknown 样本；阈值 `unknown_label_ratio_max=0.0` 时视为失败 |
| alignment / key 审计不通过 | 对应 `run_*` 返回非 0 退出码，不生成可信数据 |

## 5. 哪些模块没有参与

- **ModelService 推理**不需要：schema validation、key audit、alignment、time split、feature engineering（只读编码 manifest 与 schema 做特征构造）、训练。
- **V2 训练**不参与：SHAP explainability、generalization validation（它们是训练后的独立分析）。
- **V1 diagnostics** 不参与 V2 训练（V2 只读取 V1 的 manifest 作为 reference）。

## 6. 事实核对清单

- 真实训练入口：`scripts/run_v2_compact_tree.py` → `run_v2_compact_tree()`。
- 真实推理入口：`ModelService.predict()`（非 HTTP）。
- V2 特征：`Distance, PacketLoss, Latency`。
- V2 标签映射：`Normal=0, DoS=1, Jamming=2, ReplayAttack=3`。
- V2 pickle 无 scaler。
- 推理日志：`logs/inference/model_service.jsonl`（JSONL）。
- 下游接入点：`railways_V.2` 的 `ZLAttackDetector` 加载同一 pickle 并复现特征顺序。