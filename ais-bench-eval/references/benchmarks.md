# 评测套件（suite）明细手册

本文是 `scripts/eval_run.py` 中 `SUITES` 表的人类可读版，两者同步维护（不一致以脚本为准）。
数据可用性结论基于 2026-09-03 在评测机 90.90.122.21 上的实测。

## 评测机网络可达性（决定能评谁）

90.90.122.21 是极光平台的模型评测服务器（共享）。被测模型服务必须部署在**评测机可达**的机器上（2026-09-03 实测 ping + TCP 22）：

- ✅ **可达**：80.5.17.x 全部（106-115/119/120）、80.48.29.x 全部（113/122/143/144/145）、141.61.33.11、141.61.133.x（极光平台其他服务器）
- ❌ **不可达**：80.5.9.x 全部、141.61.81.x 全部（整网段隔离，ping/SSH 都不通）

skill 预检会从评测机 curl 模型 API 做健康检查，不可达会在发起阶段报 `blocked`（不会白跑）。
注意：`aisbench-session-*` 容器 env 里的 `AISBENCH_TARGET_IP` 是历史记录（容器多为保活空壳），不代表当前可达；网络策略可能变化，以预检实测为准。

## 已收录 suite（`--suite` 直接可用）

### 精度类（accuracy）

组合式命令，非流式 chat 接口，结果取 `results/<模型abbr>/<数据集>.json` 的 `accuracy`（**百分数**，0.45 表示 0.45%）。

| suite | 数据集任务名 | 数据来源 | 快速验证 |
| --- | --- | --- | --- |
| `gsm8k` | gsm8k_gen_4_shot_cot_chat_prompt | precision 目录（覆盖 path，包内 test.jsonl 的 answer 全是 'none'） | `--num-prompts 20` |
| `mmlu` | mmlu_gen_0_shot_cot_chat_prompt | precision 目录（57 科目，`--merge-ds` 合并汇总） | `--num-prompts 100` |
| `ceval` | ceval_gen_0_shot_cot_chat_prompt | precision 目录（多科目） | `--num-prompts 50` |
| `aime2024` / `aime2025` / `aime2026` | *_gen_0_shot_chat_prompt | 2024 用 precision 目录；2025/2026 包内就绪 | 全量仅 30 条 |
| `gpqa` | gpqa_gen_0_shot_cot_chat_prompt | 包内就绪（软链） | 全量仅 ~448 条 |
| `math500` | math500_gen_0_shot_cot_chat_prompt | precision 目录 | 全量 500 条 |
| `humaneval` | humaneval_gen_0_shot | precision 目录 | 全量 164 条，代码执行评估 |
| `mmlu_pro` | mmlu_pro_gen_0_shot_str | precision 目录 | `--num-prompts 100` |
| `longbenchv2` | longbenchv2_gen | precision 目录 | 长文本 |
| `livecodebench` | livecodebench_code_generate_lite_gen_0_shot_chat | 配置已用绝对路径 | 代码生成 |

### 性能类（perf）

流式接口（性能只支持流式）+ `-m perf`，结果在 `performances/<模型abbr>/<数据集>.json`（Request Throughput / TTFT / TPOT / Token Throughput 等，一层 `{"total": 值}` 结构）与 `.csv`（单请求延迟分位表）。

| suite | 数据集任务名 | 说明 |
| --- | --- | --- |
| `perf_synthetic` | synthetic_gen_string | 随机合成，**必须 `--num-prompts N`**；输出长度由 `--max-out-len` 控制，测长输出加 `--extra-args` 里的 ignore_eos |
| `perf_gsm8k` | gsm8k_gen_0_shot_cot_str_perf | 真实数据，性能不评 gold 所以包内数据可用 |
| `perf_sharegpt` | sharegpt_gen | 多轮真实对话 trace（precision 目录覆盖） |

性能场景**必须提供 tokenizer**（default_perf 汇总靠它算 token 数）：默认取评测机配置的 `default_tokenizer`（当前为 /home/weight/qwen3-32B-w8a8-no-w_axes-1118-full），被测模型不是该 tokenizer 时统计口径失真，精确测量请用 `--tokenizer-path` 传被测模型自己的。

### Agent 类

整体配置文件式（脚本从模板派生 `config.py`），每实例起 docker sibling 容器，**发起前确认评测机资源**。

| suite | 数据 | 规模 | 说明 |
| --- | --- | --- | --- |
| `swe_bench_verified` | SWE-bench_Verified parquet | 500 实例 | 数小时~数天 |
| `swe_bench_verified_mini` | Verified 0.05 采样 | 50 实例 | 先跑通推荐 |
| `swe_bench_multilingual` | Multilingual parquet | 300 实例 | 历史任务实测 accuracy 55.0（165/300） |
| `terminal_bench_2` | /home/terminal-bench-2 | 96 任务 | Harbor 框架 + terminus-2 agent |
| `terminal_bench_2_mini` | offline-selected_0.20 | 14 任务 | 离线采样子集 |
| `tau2_bench` | tau2-bench 包内置 | airline 50 | 数据就绪性未实测，先小规模验证 |

SWE-bench 可调参数：`--step-limit`（默认 200 步/实例）、`--batch-size`（并发实例数，默认 60）。
`--num-prompts N` 对 SWE-bench 同样生效（实测抽前 N 个实例）。
结果字段：`accuracy`（resolved 率）、`resolved_instances/total_instances/error_instances/empty_patch_instances`。

**swe_bench_lite / full 未收录**：本机无数据，模板默认从 HuggingFace 下载（内网不可达）。确需使用：先从 modelers.cn 下载 parquet 放到评测机，再往 `AGENT_DATASET_PATHS` 登记。

### 多模态类

| suite | 数据集任务名 | 说明 |
| --- | --- | --- |
| `mmmu` | mmmu_gen | 配置已用绝对路径（/home/w30074604/datasets/mmmu） |

其余多模态数据集（textvqa/docvqa/videomme 等 20+ 个）本机无数据，见下文清单。

## 快速验证路径（推荐顺序）

1. `--suite gsm8k --num-prompts 20`：精度链路（分钟级），确认 API 连通与分数产出
2. `--suite perf_gsm8k --num-prompts 20`：性能链路，确认流式与 tokenizer
3. `--suite swe_bench_verified_mini --num-prompts 2 --step-limit 20`：Agent 链路（每实例分钟级）
4. 全量评测：去掉 `--num-prompts`，Agent 类按机器资源设 `--batch-size`

## 未收录数据集任务总览（本机不可离线跑）

包内共 268 个数据集任务配置（`ais_bench/benchmark/configs/datasets/` 下 `ls */*.py` 可查全量），除上表外的主要类别及不可跑原因：

- **数据缺失**（内网无法从 HF 下载）：agieval、ARC、bbh、cmmlu、drop、hellaswag、ifeval、lambada、lcsts、LEval 全系 20 个、mbpp、mgsm、needlebench_v2 全系、piqa、race、siqa、SuperGLUE、triviaqa、winogrande、Xsum、FewCLUE 全系、dapo_math、humanevalx
- **数据在 precision 目录但未收录 suite**（要加时照 override 三元组模式登记）：longbench 21 子集、math_prm800k、aime2024 str 变体
- **需要额外 pip 包**：BFCL 全系 23 个（bfcl_eval）、ocrbench_v2
- **多模态无数据**：textvqa、docvqa、infovqa、mathvision、mmstar、omnidocbench、realworldqa、refcoco 系、videobench、videomme、vocalsound、gedit
- **性能专用未收录**：mooncake_trace（需自备 trace 文件）、mtbench

## 新增 suite 的方法

1. 确认数据就绪：包内 `ais_bench/datasets/<名>` 存在且完整，或 precision 目录 / 其他本地路径有数据
2. 在 `SUITES` 登记条目；数据需要换路径的给 `override` 三元组（模块相对路径、列表变量名——`grep "_datasets = " <配置>.py` 查、数据绝对路径）
3. Agent 类在 `AGENT_DATASET_PATHS` 登记数据路径，必要时加模板
4. 同步更新本文表格

## demo 数据集的坑

`demo_gsm8k_*`（8 条）只能做**推理/性能冒烟**：其 test.jsonl 的 gold 是 'none'，精度评估会 `IndexError`（gsm8k 后处理 `text.split('#### ')[1]`）。精度快速验证用 `gsm8k --num-prompts 8` 代替。
