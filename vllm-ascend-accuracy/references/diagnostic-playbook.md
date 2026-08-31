# 精度异常诊断手册

## 症状路由

### 乱码、无意义 token、语言相关异常

先区分响应编码问题与模型 token 本身异常。保存原始响应字节、文本和 token IDs；用同一 tokenizer 单独 decode。然后比较：

- completion 正常、chat 异常：重点检查 chat template、special/stop token、reasoning/tool parser；
- 英文正常、中文异常：比较中英文 token IDs、embedding 路径和长度，不要假定调采样参数有效；
- 首 token 正常、随后崩坏：优先比较 decode/KV cache、MTP 接受 token、async batch 变更和通信；
- 单卡正常、多卡异常：对比 TP-only、DP、EP，关注 all-reduce/all-gather 与 MoE expert routing；
- 非量化正常、量化异常：验证权重转换、量化配置、scale/zero-point、模型架构支持和产物哈希。

### 复读

先判断重复来自输入模板、模型解码、structured output 约束、sampler/logits processor，还是服务端流式拼接。

- 同时保存每个 delta 与聚合文本，排除客户端重复拼接；
- greedy 仍稳定复读时，不要先提高 `repetition_penalty`；
- 若仅启用 `repetition_penalty`、LoRA、reasoning parser 或 structured output 后出现，检查对应 logits/stop/parser 路径；
- 若只在高并发、长上下文或 batch condense 下出现，重点复现调度、KV cache、MTP/prefix cache 的组合；
- JSON Schema 中无界数组/字符串应补充业务上合理的 `maxItems`/长度约束，但这只是约束层规避，仍需确认实现是否异常。

### 空输出、截断、标签或工具调用丢失

核对 `finish_reason`、`stop_reason`、终止 token ID、最大 token 数、reasoning/tool parser 的原始输入和解析后输出。分别测试 stream 与 non-stream；不要只看客户端最终展示。

### 基准精度不达标

先让参考后端和 Ascend 端使用完全相同的输入 token IDs，并对齐 dataset revision、few-shot、chat template、generation config、stop strings、答案抽取和评分脚本。保存逐题结果，区分：

- prompt/评测器差异；
- 少量灾难性 bad case；
- 全局小幅 logits 偏差；
- 特定长度、batch、语言、模型子结构或硬件拓扑退化。

## 推荐消融矩阵

按风险和成本选择，不要求所有模型都能单卡或非量化运行。

| 维度 | 基线 | 逐步恢复 |
| --- | --- | --- |
| 解码 | greedy，固定 seed | 业务 temperature/top-p、logits processors |
| API | offline 或 plain completion | chat、stream、reasoning/tool parser |
| 并发 | batch=1、concurrency=1 | 固定 batch → 动态高并发 |
| 上下文 | 短输入、短输出 | 长 prefill、长 decode、混合长短请求 |
| 执行 | eager | piecewise/full ACL Graph、chunked prefill |
| 并行 | 单卡或纯 TP | DP、EP、跨机、多流通信优化 |
| KV/调度 | 关闭 prefix cache、async scheduling | 分别启用，再测试组合 |
| 推测解码 | 关闭 | MTP/speculative decoding，再与 async/cache 组合 |
| 权重 | 已知正常官方/原精度 | FP8、W8A8、W4A8 等量化产物 |
| 扩展功能 | 无 LoRA、无 structured output | 单独启用 LoRA、grammar/parser |
| 版本 | 官方兼容组合或已知正常镜像 | 目标版本；再做相邻版本二分 |

每个实验记录：配置 ID、镜像/commit、启动参数与环境变量、拓扑、请求集哈希、总次数、失败次数、首差异 token、任务分数、吞吐/时延。一次只改变一个维度；必须测试组合时，明确写成二因素或多因素实验。

## 层级化取证

### 1. 输入与解析

导出 chat template 渲染后的文本和 token IDs，验证 tokenizer revision 与权重匹配。对 stop token、tool/reasoning parser，保留解析前后的响应。

### 2. 模型前向

在同一输入上比较参考后端与目标端：

- prefill 最后位置 logits；
- 每个 decode step 的 top-k token/logits；
- 首个 token 排序或数值显著偏离的位置；
- 必要时逐层或逐算子缩小范围。

浮点实现允许小误差；关注误差是否改变 top-k、expert selection 或最终 token，而不是要求逐位一致。

### 3. 并行和 MoE

优先比较纯 TP 与 DP/EP。若只有 EP/跨机异常，检查 expert map、dispatch/combine、all-gather/all-reduce、通信确定性及不同 rank 的中间结果。启用确定性计算可以作为诊断实验，但若有明显性能代价，不应直接作为默认生产修复。

### 4. 调度、KV cache 与推测解码

使用固定前台请求，并以后台长短混合请求制造 batch churn 和 KV 压力。分别关闭 async scheduling、prefix caching、MTP，再测试两两组合，避免把交互缺陷归到单一开关。

## 常见模式与判定边界

以下是历史 issue 展示的模式，只能作为生成假设的线索，不能脱离目标版本直接套用：

- DP+EP 的 MoE 通信实现曾导致混乱字符，而纯 TP 可规避；
- full graph + chunked prefill 在特定 DP 配置曾出现显著分数下降；
- FlashComm 与 MTP 组合、以及 async + MTP + prefix cache 在高并发下曾出现间歇性乱码/重复；
- 启用 LoRA 路径曾与错误 stop token、截断、复读及 parser 异常同时出现；
- structured output 的无界 schema 可能导致重复直到 `max_tokens`；
- 某些偶现 MoE 问题在确定性通信实验中消失，但这不自动证明最终根因，也不代表可无代价上线。

应以“哪个单变量或交互项能稳定开启/关闭故障”来更新假设。某开关关闭后不再复现，只能证明关联或规避，除非进一步证据定位到错误实现。

## 官方资料入口

使用时核对目标版本的最新状态与结论：

- 版本策略与兼容矩阵：https://vllm-ascend.readthedocs.io/en/latest/developer_guide/versioning_policy.html
- vLLM Ascend 仓库：https://github.com/vllm-project/vllm-ascend
- DP+EP 精度案例：https://github.com/vllm-project/vllm-ascend/issues/2767
- full graph/chunked prefill 案例：https://github.com/vllm-project/vllm-ascend/issues/3444
- MoE 偶现通信/确定性案例：https://github.com/vllm-project/vllm-ascend/issues/8359
- FlashComm + MTP 案例：https://github.com/vllm-project/vllm-ascend/issues/8989
- async + MTP + prefix cache 案例：https://github.com/vllm-project/vllm-ascend/issues/13863
- LoRA/stop token/parser 案例：https://github.com/vllm-project/vllm-ascend/issues/8863
- structured output 复读案例：https://github.com/vllm-project/vllm-ascend/issues/13646

搜索 issue 时组合模型架构、vLLM Ascend 版本、NPU 型号和最小触发功能，不只搜索“accuracy”。
