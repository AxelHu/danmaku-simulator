# Sintel 真实生成样例

本目录为 2026-11-06 的已完成实验记录。**65 条弹幕 = 63 条独立模型结果 + 2 条受控复读**。回放不需要 API 密钥；重新生成会产生新的调用。

```bash
python3 tools/prepare_sintel_demo.py
python3 -m danmaku_sim serve --port 8793
# http://127.0.0.1:8793/?run=sintel-recorded
```

`comments.json` 包含候选与最终时间，`analysis.json` 包含真实 STT 和关键帧观察，`config.json` 是该次实验配置。`recorded-usage.jsonl` 是历史请求账本，已去掉供应商请求标识和缓存键；`usage-summary.json`、`metrics.json` 和 `cache-replay.json` 记录汇总、指标口径与零网络重放验证。`baseline/` 保存改进前的第一次实验。

复制历史账本的样例准备脚本会把运行标题标为“实测回放”。要生成自己的结果，请使用新的输出目录，不向这个历史样例追加模型调用。

原片：**Sintel trailer**，© Blender Foundation / Durian Open Movie Project，[CC BY 3.0](https://creativecommons.org/licenses/by/3.0/)。[官方来源](https://durian.blender.org/download/)；视频地址、校验值与改动说明见 [SOURCE.json](SOURCE.json)。本项目增加中文 AI 弹幕和预览叠层；视频单独下载，没有随源码分发。

解释与局限见[实测报告](../../docs/EXPERIMENT_2026-11-06.md)。
