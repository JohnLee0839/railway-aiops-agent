# 模型产物

本目录保存经过训练并用于在线推理的模型文件。模型由 `ml/src/stsrs_data_engineering/` 中的训练流水线生成，配套元数据位于 `ml/metadata/manifests/`。

当前注册的模型版本：

- `V0`：SGD 线性基线模型。
- `V1`：使用完整编码特征的 HistGradientBoosting 模型。
- `V2`：使用 `Distance`、`PacketLoss`、`Latency` 三个特征的精简模型，应用默认使用此版本。

模型文件采用 pickle 格式，仅加载来源可信的模型文件。重新训练模型后，应同步更新对应 manifest，并执行 `tests/test_zl_attack_detector.py` 中的真实模型 smoke test。
