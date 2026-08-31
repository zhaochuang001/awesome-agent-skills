---
name: vllm-ascend-accuracy
description: 诊断、修复并验证本地或远程服务器容器中 vLLM Ascend 推理的乱码、复读、空输出、截断、工具调用退化及基准精度不达标；适用于需要远程复现、迭代配置或源码并完成回归验证的精度问题，不用于普通性能调优。
---

# vLLM Ascend 精度诊断与修复

目标是以可复现证据找到最小致因，实施可回滚修复，并按用户确认的标准完成精度、稳定性和性能回归。不要仅凭输出表象归因，也不要用 `repetition_penalty` 或提示词改写掩盖系统性错误。

## 路由与默认边界

- 远程容器场景必须先读取 [references/remote-container-workflow.md](references/remote-container-workflow.md)，并按其中的“推荐首轮提问”收集服务器/密码/容器、可选联网代理、启动脚本、异常复现信息和用户验收标准；不要另写更长的表格。
- 默认目标是专门用于精度排障的非生产测试容器，允许直接修改、安装依赖和重启。用户声明为生产环境或限制操作时，以其约束为准。
- 出现乱码、复读、空输出或分数退化时，按 [references/diagnostic-playbook.md](references/diagnostic-playbook.md) 选择症状路由和消融维度。
- 致因缩小后，读取 [references/remediation-playbook.md](references/remediation-playbook.md) 选择最小修复层级。
- 查询版本行为时只使用与目标版本对应的官方文档、release note、GitHub issue/PR 和源码；`latest` 文档不能代表旧版本。

## 解决闭环

1. **锁定输入和验收**：确认用户验收标准；用户没有标准时，根据正常基线提出量化方案并请其确认。
2. **冻结环境**：记录镜像、版本、权重/tokenizer、启动脚本与实际进程、并行拓扑和健康状态，且不泄露凭据。
3. **复现并建立基线**：固定请求/token、chat template、采样参数、seed 和评测器；在原始配置复现，并与已知正常版本或参考后端对照。
4. **单变量定位**：依次检查输入/解析、权重/量化、执行模式、并行通信、调度/KV、MTP、sampler 等层级；概率问题使用有界的目标负载重复测试。
5. **迭代修复**：每轮备份原文件，只改一个假设对应的变量，记录 diff、启动配置、结果和回滚点；无效则恢复，有效则做回切验证。
6. **验证恢复**：运行原 bad case、代表性精度集和目标 PD/TP/DP/EP 拓扑，报告失败数/总数、任务分数、吞吐、TTFT、TPOT 和显存变化。
7. **保存成果**：将最终 diff/patch、修改文件、启动配置、回归结果和报告复制到容器外。仅在用户要求生产化交付时构建镜像或修改部署配置。

## 测量要求

- 将输出异常率与任务得分分开；乱码/复读检测不等同于业务精度。
- 对齐数据集 revision、split、few-shot、prompt/chat template、最大长度、采样参数和答案抽取器。
- 同时报告绝对分数、基线差值、样本数和逐样本结果；随机采样使用多个 seed。
- 可用 `scripts/analyze_generations.py` 初筛 JSONL 中的空输出、异常字符和 n-gram 复读，但不能用它替代任务指标或人工判定。

## 完成条件与交付

只有同时满足以下条件才报告为“已解决”：

- 达到用户确认的 bad case/数据集和分数标准；
- 在目标上下文、并发和并行拓扑下完成回归，概率问题报告测试规模；
- 通过 A/B/A、版本回切或等价证据建立因果关系；
- 已保存可执行变更、回归用例和回滚方法。

只能关闭功能、回退版本或启用确定性计算时，标记为“已规避”；缺少硬件、权重、权限或外部条件时，标记为“受阻”，不得宣称已解决。

任务达到已解决、已规避或受阻等终态时，读取 [references/final-report-template.md](references/final-report-template.md) 生成独立 Markdown 报告。用户未指定目录时写入当前工作目录，文件名使用 `vllm-ascend-accuracy-report-YYYYMMDD-HHMM.md` 且不得覆盖已有文件。报告不得包含密码、token、私钥或未脱敏业务数据；最终回复提供报告的可点击绝对路径。

需要提交公开 issue 或研发缺陷单时，再读 [references/issue-report.md](references/issue-report.md)。
