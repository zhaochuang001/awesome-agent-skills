# 精度问题修复手册

致因层级已缩小或需要恢复服务时读取本文件。目标是实施最小、可回滚、可验证的修复。

## 结果分类

- **根因修复**：证据定位到错误实现，修复该实现并通过回切与回归。
- **候选修复**：修改已对部分样本有效，但尚未覆盖用户验收范围；只能作为中间状态。
- **已规避**：通过回退版本、关闭功能、改用纯 TP 或确定性计算消除现象，但根因未修复；默认继续定位。
- **已解决**：根因修复已通过用户验收标准及目标拓扑回归。
- **受阻**：没有可接受规避方案，且缺少硬件、源码、权限或其他外部条件而无法继续。

## 状态转换规则

1. 任何有效修改先标记为候选修复，并扩大到用户要求的数据集、负载和拓扑。
2. 候选修复未满足验收时继续迭代；不得因少量 bad case 通过而结束。
3. 仅靠回退或关闭功能满足验收时标记为已规避，并询问用户是否接受。用户不接受则继续定位根因。
4. 用户明确接受规避方案，或根因修复受外部条件阻塞且用户同意收尾时，已规避才可成为终态。
5. 只有定位并修复根因、完成回切验证且满足用户验收时，才转换为已解决。

优先采用目标维护分支中已合入且与当前版本兼容的官方修复。Backport 时核对依赖 API 和相邻提交，不只复制单个 diff。确定性计算等高性能代价方案仅作为诊断或临时规避，不默认生产化。

## 按层级解决

| 已证实层级 | 首选修复 | 必做验证 | 不充分的做法 |
| --- | --- | --- | --- |
| chat template/tokenizer | 对齐模型与 tokenizer revision；修正 template、special/stop token | 渲染文本、输入/输出 token IDs、chat/completion | 只改提示词或采样参数 |
| reasoning/tool/parser | 修正 parser、终止 token 和流式聚合 | 解析前后响应、stream/non-stream、工具调用 | 只检查客户端展示 |
| 版本/依赖 | 使用官方兼容组合，或升级/回退到已验证版本 | 固定镜像与版本，做版本 A/B/A | 只改一个包且忽略 CANN/torch_npu |
| 权重/量化 | 重新校验/转换权重，修正 scale 或换回正常产物 | 文件哈希、非量化对照、精度集和长上下文 | 用 repetition penalty 掩盖错误 |
| graph/chunked prefill | 修复图构建、shape 或缓存复用；临时切换执行模式 | eager/graph、batch/长度边界 | 永久关闭优化却称为根因修复 |
| DP/EP/MoE 通信 | 修正 expert map、dispatch/combine、collective 或 rank 状态；临时纯 TP | 单卡/TP/DP/EP、跨机、各 rank 中间值 | 无证据堆 HCCL 环境变量 |
| KV/cache/async/MTP | 修正接受计数、batch row 映射、KV 生命周期或同步 | 长短混部、batch condense、KV 压力和高并发 | 只用单请求验证 |
| sampler/logits processor | 修正 token history、stop 处理或 processor 顺序 | greedy/采样、penalty 开关、首差异 step | 单纯提高惩罚系数 |
| structured output | 修正 grammar 状态/终止逻辑并设置合理 schema 上界 | 有界/无界 schema、流式、`max_tokens` 边界 | 只降低 `max_tokens` |

## 源码修复约束

1. 在与问题版本匹配的 `vllm`、`vllm-ascend` commit 上复现；main 分支存在 verified vLLM commit 机制时使用对应 revision。
2. 从输出 token/top-k logits 开始向前二分首个错误层级，不从全量逐层 dump 开始。
3. 概率问题使用固定前台请求和有界后台压力，记录 seed、并发、请求数和停止条件。
4. 修改最小代码路径，并添加修复前失败、修复后通过的行为或张量不变量测试。
5. 运行受影响的 UT、单卡/多卡 E2E、用户精度集和性能对照；无法运行的项目及原因写入最终报告。

关闭某个功能后不再复现只证明关联或规避；除非证据定位到错误实现，不得宣称根因已修复。
