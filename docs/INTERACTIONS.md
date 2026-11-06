# 少量弹幕互动

互动是可选的第二个生成阶段，默认关闭。原来的观众仍然独立观看、独立生成，互相看不到弹幕；受控复读仍由原调度器处理。开启后，少数其他观众可以看见一条已经出现的父弹幕，并用当前可见证据接一句。

自动测试使用明确的测试替身，不代表模型质量或实际费用。另已完成 [Sintel 真实模型实验](INTERACTION_DISTRIBUTION_EXPERIMENT_2026-11-06.md)：原 65 条保持不变，增加 3 条接话；模型用量、参考费用与断网缓存复跑分别留有记录。

## 开启方式

在现有配置中添加：

```json
{
  "interactions": {
    "enabled": true,
    "ratio": 0.06,
    "max_comments": 4,
    "max_depth": 1,
    "read_delay": 0.4,
    "max_parent_delay": 3.0,
    "min_spacing_seconds": 8.0,
    "max_text_chars": 12,
    "concurrency": 2
  }
}
```

`history_seconds` 未填写时继承 `generation.history_seconds`，再缺省为 45 秒。上述其他值是开启时的默认值；省略整个 `interactions` 或设置 `enabled: false` 时，不增加模型调用，也不改变旧生成结果和独立采样的缓存键。

| 配置 | 含义 |
|---|---|
| `ratio` | 互动总数占已安排的独立弹幕数量的比例，向下取整，默认 0.06 |
| `max_comments` | 每个结果的互动总上限，默认 4；不是每个角色各 4 条 |
| `max_depth` | 首版固定为 1，拒绝其他值，不生成链式讨论 |
| `read_delay` | 父弹幕出现后，等待多久再读取当时可见证据，默认 0.4 秒 |
| `max_parent_delay` | 回复最迟在父弹幕出现后多久播放，默认 3 秒 |
| `min_spacing_seconds` | 两条互动之间的最短间隔，默认 8 秒 |
| `max_text_chars` | 回复字符上限，默认 12；提示词要求通常 2–10 字 |
| `history_seconds` | 提供给互动观众的证据历史长度 |
| `concurrency` | 本阶段的并行调用上限，默认 2 |

例如 63 条独立弹幕的默认预算为 `floor(63 × 0.06) = 3`，最多计划 3 次互动请求。已有互动会占用这个预算；不足 17 条独立弹幕时默认不生成互动。实际回复可以少于预算，模型沉默、证据不足或没有合适位置都不会触发额外请求来凑数。

## 对已有结果单独追加

```bash
python3 -m danmaku_sim interact --run runs/original \
  --config configs/example.json --out runs/social
```

首次输出必须是新目录或空目录。CLI 保存 `interaction-run.json`，以原视频身份、原弹幕和分析的内容摘要绑定来源；之后可以在同一目录恢复同源运行和利用缓存。其他普通 run，即使视频相同，也不能作为互动输出目录，避免覆盖结果或混入已有生成费用。替换原弹幕或分析后，应使用新目录。

Python 接口适用于复用已生成的观众弹幕：

```python
from danmaku_sim.interactions import add_interactions
from danmaku_sim.providers import ModelClient
from danmaku_sim.cli import export_run

# analysis、config、result 分别读取已有分析、启用互动的配置及原 comments.json。
# out 指向一个新的 run，cache_dir 可指向原来共用的响应缓存。
client = ModelClient(config, out, cache_dir=cache_dir)
extended = add_interactions(analysis, config, client, result)
export_run(out, extended)
```

函数返回结果字典，不修改传入的 `analysis`、`config` 或 `result`。它不调用 `generate`，不重跑 STT、视觉分析或独立观众。原 `candidates` 保留；新候选单独保存在 `interaction_candidates`。已有弹幕的文字、时间、身份和知识截止不变。

普通 `generate` 在开启此配置时，会在原生成和受控复读完成后调用同一接口。独立阶段的 system/common/variant 输入保持原样。对已经达到互动预算的结果再调用此接口，不会继续发起请求；所有新父弹幕仍只能来自原独立观众。

## 父弹幕与证据

父弹幕必须已经安排播放，且 `generation_method == "independent"`。复读、已有互动、带父链的条目以及来源和时间不一致的条目不会成为父弹幕。优先考虑疑问，以及带有不确定观察的短反应；父弹幕最多 20 字。同一父弹幕和同一来源事件在结果中各最多产生一次互动。

接话角色从 `audience` 中另选一个正权重的 persona，不能与父弹幕的 persona 相同。每次模型调用只包含：

- 一条父弹幕的文字、ID、出现时间和来源事件。
- 截至本次检查点已可见、且在历史范围内的事件。
- 当前接话角色及固定种子。

不提供其他观众的弹幕、未来弹幕、片名或全局剧情摘要。单条父弹幕仅作为别人的看法，不能作为新的事实证据。

模型可以返回 `reply`、`correction` 或 `agreement`，也可以返回空数组。每条回复必须引用上下文内的 `evidence_event_ids`。未知或未来证据、改回其他父弹幕、照抄父弹幕、超长文字或网址会被过滤。

`uncertain=true`，以及包含“像、状、疑似”等表述的观察，不能被当成确定事实。纠正若只引用这类证据，还必须保留疑问或不确定措辞，否则过滤。这个检查约束时间、来源和措辞，不等同于证明自然语言内容正确；首次真实模型输出仍需对照画面抽查。

## 时间与密度

设父弹幕在 `t` 秒出现，默认互动检查点为 `ceil_ms(t + 0.4)`。这 0.4 秒用于看见并读取父弹幕，检查点之后仍须等待原调度器的 `reaction_min`。

当 `reaction_min = 0.35` 时，默认最早回复约为 `t + 0.75` 秒，且：

```text
evidence.available_at <= knowledge_cutoff
parent_time < knowledge_cutoff
knowledge_cutoff + reaction_min <= reply.time
reply.time <= parent_time + max_parent_delay
```

回复还受 `knowledge_cutoff + scheduler.max_delay` 和视频结束时间限制。使用原调度器“任意半开一秒区间”的密度规则，预留位置时计入所有已有独立弹幕、复读、互动及本次其他预留回复。不会移动旧弹幕来挤出位置；如配置 `target_per_minute`，互动只能使用其剩余数量预算。

这些位置在模型调用前检查。没有合法位置、没有另一角色、父事件已不在历史中或已接近片尾，就不会发起付费请求。之后模型若沉默或失败，预留位置空着，不再补发。

## 结果、费用与失败

新增弹幕保留普通播放字段，另含：

| 字段 | 含义 |
|---|---|
| `generation_method` / `kind` | 均为 `interaction` |
| `parent_id` / `reply_to` | 同一个父弹幕 ID，兼容两种消费方式 |
| `parent_text` / `parent_time` | 实际看见的父弹幕文字与出现时间 |
| `depth` | 固定为 1 |
| `interaction_type` | `reply`、`correction`、`agreement` |
| `source_event` | 父弹幕来源事件的规范化记录，保留 `uncertain` |
| `evidence_event_ids` | 本次回复引用的已可见证据 |
| `knowledge_cutoff` | 本次回复真正使用的上下文截止 |
| `scheduled_time` / `latest_time` | 预留的播放时间及截止期限 |

所有请求经 `ModelClient.complete(..., stage="interaction")` 记账。该阶段有稳定的提示词、父弹幕和上下文缓存键，可复用已有缓存；本地缓存命中仍按客户端原有规则记录为零新请求。此函数不读写或复制旧用量账本，新 run 的客户端只记录这次新增互动的费用。

网络错误、无效 JSON 或非空回复全部不合格时，本阶段记为 `partial`，整份结果也保留 `partial`，原独立弹幕和复读全部保留。全部互动失败也不会启用 mock 或丢弃原结果。合法 `comments=[]` 是成功沉默。无效结构的缓存会请求失效处理，不会在同一轮自动重试。

详情在 `diagnostics.interactions`：预算、预定/成功/失败调用、本地缓存命中、沉默次数、跳过原因、过滤原因与错误类型。`diagnostics.comments_generated` 是总弹幕数；原 `diagnostics.scheduler` 继续描述互动之前的调度阶段。

## 验证

```bash
python3 -m unittest discover -s tests -p 'test_interactions.py' -v
```

测试覆盖未来信息隔离、另一角色接话、父链深度、比例与绝对预算、全局密度和目标数量限制、确定性缓存、不确定纠正、合法沉默，以及部分或全部失败后保留原结果。测试替身不连接任何模型服务。
