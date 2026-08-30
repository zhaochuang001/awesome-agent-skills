# 精度问题修复手册

在致因层级已经缩小，或用户需要先恢复服务时读取本文件。目标是实施最小、可回滚、可验证的修复。若运行环境位于远程容器，先按 [remote-container-workflow.md](remote-container-workflow.md) 建立访问、快照、复现和迭代账本。

## 修复策略

### 快速恢复

选择已由消融实验验证有效、影响面最小的规避措施，例如回退到已知正常且依赖兼容的镜像、关闭唯一触发异常的功能组合、改用纯 TP、关闭问题量化产物或修正 chat template。必须同时：

- 固定镜像 digest、包版本、模型和 tokenizer revision；
- 保存变更前后的完整启动配置；
- 跑最小 bad case 与核心业务集；
- 量化吞吐、时延和显存代价；
- 给出一条可执行的回滚路径。

快速恢复不等于根因修复。不要把未经目标版本验证的环境变量批量加入生产配置。

### 根因修复

在可复现环境中找到首个错误 token、logits、rank 或算子边界，再修改最小代码路径。优先采用目标维护分支中已合入且与当前版本兼容的官方修复；需要 backport 时核对依赖 API 和相邻提交，不能只复制单个 diff 后跳过回归。

源码修复至少包含：

1. 最小复现或自动化失败用例；
2. 针对致因的实现变更；
3. 修复前失败、修复后通过的回归测试；
4. 相关执行模式的对照测试，例如 eager/graph、TP/DP/EP、同步/异步；
5. 精度和性能结果。

## 按层级解决

| 已证实层级 | 首选修复 | 必做验证 | 不充分的做法 |
| --- | --- | --- | --- |
| chat template/tokenizer | 对齐模型与 tokenizer revision；修正 template、special/stop token | 比较渲染文本、输入/输出 token IDs、chat/completion | 只改提示词或采样参数 |
| reasoning/tool/parser | 修正 parser 选择、终止 token 和流式聚合逻辑 | 保存解析前后响应，测试 stream/non-stream、工具调用 | 只检查客户端展示 |
| 版本/依赖 | 使用官方兼容矩阵组合，或升级/回退到已验证修复版本 | 固定所有版本与镜像 digest，做版本 A/B/A | 只改一个包且忽略 CANN/torch_npu |
| 权重/量化 | 重新校验/转换权重，修正量化配置与 scale，或换回已知正常产物 | 文件哈希、非量化对照、代表性数据集和长上下文 | 通过 repetition penalty 掩盖错误 |
| graph/chunked prefill | 使用无问题执行模式，或修复图构建、shape/缓存复用路径 | eager/graph、不同 batch/长度边界 | 永久关闭优化却称为根因修复 |
| DP/EP/MoE 通信 | 修正 expert map、dispatch/combine、collective 或 rank 状态；临时改纯 TP | 单卡/TP/DP/EP、跨机、确定性对照、各 rank 中间值 | 无证据地堆 HCCL 环境变量 |
| KV/cache/async/MTP | 修正 token 接受计数、batch row 映射、KV 生命周期或同步；临时关闭触发组合 | 长短混部、batch condense、KV 压力和高并发 | 只用单请求验证 |
| sampler/logits processor | 修复 output token history、stop 处理或 processor 顺序 | greedy/采样、penalty 开关、首差异 step | 单纯提高惩罚系数 |
| structured output | 修复 grammar 状态/终止逻辑，并给 schema 添加合理上界 | 有界/无界 schema、流式、达到 `max_tokens` 的边界 | 只降低 `max_tokens` |

## 源码修改流程

1. 在与问题版本完全匹配的 `vllm` 和 `vllm-ascend` commit 上复现；main 分支使用仓库记录的 verified vLLM commit（若该机制在目标版本存在）。
2. 先在最靠近症状的边界保存 token IDs 和 top-k logits，再向前二分，不要从全量逐层 dump 开始。
3. 对概率性问题构造固定前台请求和有界后台压力，记录 seed、并发、请求数与停止条件。
4. 用 feature flag、执行模式或版本二分确定最小差异后再改代码。
5. 回归测试应验证行为或张量不变量，不要只匹配一段固定生成文案。
6. 运行受影响的 UT、单卡/多卡 E2E 和精度集；硬件条件不足时明确列出未运行项目。

## 验收矩阵

至少覆盖与故障有关的维度：

| 类别 | 最低覆盖 |
| --- | --- |
| 正确性 | 原 bad case、代表性精度集、首差异 token/logits |
| 稳定性 | 多轮、多个 seed 或足量确定性重复；报告失败数/总数 |
| 负载 | 单请求和目标并发，相关时增加长短混部与 KV 压力 |
| 拓扑 | 目标 TP/DP/EP/跨机配置及一个正常对照 |
| 功能 | 与故障相关的 cache、MTP、graph、LoRA、parser 等开关组合 |
| 性能 | 吞吐、TTFT、TPOT、显存；与修复前正常基线比较 |
| 运维 | 启动成功、健康检查、灰度方案和回滚命令 |

验收阈值应来自用户 SLA、模型卡或已知正常基线。没有统一阈值时，先报告测量值和差异，不自行宣称“精度一致”。
