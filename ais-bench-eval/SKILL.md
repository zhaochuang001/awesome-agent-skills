---
name: ais-bench-eval
description: 在评测机上用 ais_bench 自动评测大模型的四类基准：精度（mmlu/ceval/gsm8k 等数据集打分）、性能（吞吐/TTFT/TPOT 压测）、Agent（SWE-bench/Terminal-Bench/tau2-bench）、多模态（mmmu 等）。覆盖评测发起、后台执行、进度跟踪、断点续跑、分数提取全流程。适用于"帮我评测模型/跑个 benchmark/测下精度/性能压测/SWE-bench 跑个分/评测任务怎么样了/续跑上次评测"等请求；不用于部署或起模型推理服务、不做精度问题根因排查（那是 vllm-ascend-accuracy 的职责）、也不管理评测机本身的机器清单（server-management 的职责）。
---

# AISBench 模型评测

用 [AISBench](https://github.com/AISBench/benchmark)（基于 OpenCompass）对**已部署的 OpenAI 兼容推理服务**做端到端评测：本地脚本编排，评测在评测机的专用容器里后台执行，动辄数小时的任务通过任务表跟踪、随时查进度、中断可续跑。

## 前置条件

- 评测机可达且已配置（默认 90.90.122.21；其他机器在 `~/.ais-bench-eval/hosts.json` 登记，字段：host/port/user/image/benchmark_src/task_root）
- 被测模型服务已运行，且有 **评测机可达** 的 OpenAI 兼容地址（`http://ip:port/v1`）——注意是评测机到模型服务的网络，不是本地
- 本地脚本零第三方依赖（只用系统 ssh）

## 动作路由

| 用户意图 | 命令（Windows 用 `py -3`，POSIX 用 `python3`） |
| --- | --- |
| 发起评测 / 续跑 | `python scripts/eval_run.py --suite <名> --api-base <url> --model <名> [选项]`，续跑加 `--reuse <task-id>` |
| 查进度 / 列任务 | `python scripts/eval_status.py <task-id>`，列全部任务 `--list` |
| 提取分数 | `python scripts/eval_result.py <task-id>`（历史时间戳加 `--all`） |

三个脚本都是后台执行模式：`eval_run.py` 完成预检和派活后立即返回 `task_id`，**不要在发起后同步等待评测完成**——SWE-bench 全量要数小时到数天。发起后主动用 `eval_status.py` 轮询（建议间隔 ≥ 5 分钟），状态 `finished` 后再 `eval_result.py` 取分。

## 发起前的必问参数

用户没有显式给出 `--max-out-len` 和 `--batch-size`（并发）时，**发起评测前先问用户设为多少**，不要自作主张用脚本默认值。询问时给出有依据的建议值：

- `--max-out-len`（单请求最大输出 token）：参考被测服务启动脚本的 `--max-model-len`，**上限 = max-model-len − 8K**（输入+输出 ≤ max-model-len，超了服务直接 400 拒绝）。thinking 模型（qwen3.5/qwq 等）对截断极敏感——实测 gsm8k 同一服务：512→25%，2048→65%，40960（max-model-len−8K）→90%，低截断值给出的分数毫无意义
- `--batch-size`（并发）：参考服务启动脚本的 `--max-num-seqs`，精度评测给 16~128（对齐 max-num-seqs 最快）；Agent 场景是并发实例数

用户让"直接跑/快速验证"不给值时，才用脚本默认（max_out_len 2048、精度并发 16、性能并发 32、Agent 60）。

## suite 一览（权威表在 scripts/eval_run.py 的 SUITES，明细见 references/benchmarks.md）

| 类别 | suite | 要点 |
| --- | --- | --- |
| 精度 | `gsm8k` `mmlu` `ceval` `math500` `humaneval` `mmlu_pro` `longbenchv2` `aime2024/25/26` `gpqa` `livecodebench` | 组合式命令，结果取 `accuracy`（百分数） |
| 性能 | `perf_synthetic` `perf_gsm8k` `perf_sharegpt` | 流式 + `-m perf`；synthetic 必须 `--num-prompts`；需 tokenizer（默认取机器配置，精确测量用 `--tokenizer-path`） |
| Agent | `swe_bench_verified` `_mini` `swe_bench_multilingual` `terminal_bench_2` `_mini` `tau2_bench` | 整体配置式，每实例起 sibling 容器；`--step-limit` 控步数 |
| 多模态 | `mmmu` | 组合式，chat 接口 |

**快速验证路径**（先跑通再上全量）：`gsm8k --num-prompts 20`（精度）→ `perf_gsm8k --num-prompts 20`（性能）→ `swe_bench_verified_mini --num-prompts 2 --step-limit 20`（Agent）。不要用 `demo_gsm8k` 评精度——它的 gold 是 'none' 会报 IndexError（详见 references/benchmarks.md 的坑说明）。

## 关键流程（eval_run.py 内部行为）

1. **预检**（只读）：SSH 可达 → 镜像存在 → 源码目录存在 → 从**评测机**上 curl 模型 API 健康检查；任一失败返回 `blocked` 并说明缺什么
2. **生成任务目录** `<task_root>/<task-id>/`：模型配置（组合式 `cfg/models/api_model.py`，Agent 场景为整体 `config.py`）+ 数据集覆盖配置（`cfg/datasets/<任务名>.py`，把包内损坏/缺失的数据路径改指 precision 数据目录，read_base 继承模式）+ `run.sh`（cd 到源码目录、`tee` 日志、`trap` 记录 exit_code）+ `bootstrap.sh`
3. **自建评测容器** `ais-bench-eval-<task-id>`：与评测机现有 aisbench-session 容器同款挂载（/home /data /tmp /mnt + docker.sock），host 网络，`sleep infinity` 保活。**绝不动别人的 session 容器**
4. **派活**：`docker exec` + `setsid` 后台执行，立即返回
5. 评测产物全部落在 `<task-dir>/outputs/`（--work-dir），与共享的 `outputs/default` 隔离

## 安全边界

- **API key 只进评测机任务目录的 config 文件**，不写入本地任务表、不回显、不进最终 JSON
- 不起停别人的容器；自建容器任务结束后保留（排查用），用户要求清理时才删
- 评测写入只发生在 `<task_root>/<task-id>/` 下，不碰源码目录、数据集、共享 outputs
- Agent 类评测会经 docker.sock 起大量 sibling 容器（SWE-bench 每实例一个，跑完自动清理）：发起前向用户确认机器资源可承受
- 评测机 /tmp 重启可能清空（默认 task_root 在 /tmp，与机器现有惯例一致）：重要结果用 `eval_result.py` 及时提取，长跑任务建议把 task_root 配到持久盘

## 输出协议

与 server-management / npu-migrate 一致：stderr `__SM_PROGRESS__=<json>` 进度，stdout 单个最终 JSON，`ok=true` 退出码 0。任务状态词汇：`running`（发起成功/进行中）/ `finished` / `failed` / `needs_input`（缺参数）/ `blocked`（评测机或 API 不可达）/ `removed`。

## 分数解读速查

- 精度：`results/<模型abbr>/<数据集>.json` 顶层 `accuracy`（百分数）
- 性能：`performances/<模型abbr>/<数据集>.json`（Request Throughput、TTFT/TPOT 分位）+ `.csv`（单请求延迟表）
- SWE-bench：`accuracy` = resolved 率，另有 `resolved_instances/total_instances/error_instances` 等明细
- 官方汇总文本：`summary/summary_<时间戳>.txt`（eval_result.py 已带回）

## 与其他 skill 协同（按需调用，本 skill 单独可用）

- **找空闲机器跑模型服务**：被测服务还没起时，用 server-management 的 `fleet_cli.py capacity` 查集群空闲卡，而不是逐台问
- **迁移评测环境**：评测相关容器/代码要搬机器时用 npu-migrate
- **评测机磁盘满**：任务产物（尤其 SWE-bench 的实例镜像和日志）很占空间，用 disk-cleanup 分析清理
- **评测分数异常要排查根因**：本 skill 只负责跑分和报数；精度问题定位用 vllm-ascend-accuracy

## 行为契约

任务状态机、续跑语义、评测机配置字段的完整定义见 [references/behavior.md](references/behavior.md)；各 suite 的数据集任务名、参数、结果落位对照表见 [references/benchmarks.md](references/benchmarks.md)。
