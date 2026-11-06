# 可直接读取的静态弹幕目录

本目录是公开的少量协议样例，供用户脚本从 HTTPS 远端读取。它不是把所有用户运行结果同步到 Git 的模板。

目录地址：

```text
https://raw.githubusercontent.com/AxelHu/danmaku-simulator/main/examples/catalog/catalog.json
```

| 轨道 | 内容 | 来源 |
| --- | --- | --- |
| `standard` | 63 条独立弹幕 + 2 条受控复读，共 65 条 | Sintel v2 真实生成 |
| `social` | 保留上述 65 条，再增加 3 条实际接话，共 68 条 | 可选互动阶段真实生成 |

JSON 保留原始时间和互动关系；XML/ASS 是播放器消费格式。每个文件的 SHA-256 和字节数在 catalog 中。目录使用相对链接，复制到其他静态托管地址后无需改写轨道链接。

两条轨道仅适用于 **52.208333 秒的 Sintel 预告片版本**，没有冒充其他网站视频绑定。它们不包含视频，也不会触发模型调用。准备同一视频可在仓库根目录运行 `python3 tools/prepare_sintel_demo.py`。

源视频 © Blender Foundation / Durian Open Movie Project，[CC BY 3.0](https://creativecommons.org/licenses/by/3.0/)。中文 AI 弹幕是本项目添加；原片来源与文件校验见 [SOURCE.json](../sintel/SOURCE.json)。

[协议与发布命令](../../docs/DISTRIBUTION.md) · [首次生成报告](../../docs/EXPERIMENT_2026-11-06.md) · [互动与分发记录](../../docs/INTERACTION_DISTRIBUTION_EXPERIMENT_2026-11-06.md)
