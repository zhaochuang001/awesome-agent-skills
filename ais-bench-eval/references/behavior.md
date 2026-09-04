# 行为契约

本文是 ais-bench-eval 状态与语义的唯一定义来源；SKILL.md 与脚本只引用、不重复定义。

## 任务状态机

```text
eval_run.py 发起成功 → running
running ├─ exit_code=0   → finished（可 eval_result.py 提分）
        ├─ exit_code≠0   → failed（看 log_tail 定位，可 --reuse 续跑）
        └─ 进程消失且无 exit_code → failed（容器被杀等异常终止）
finished/failed --reuse→ running（ais_bench --reuse 从最新时间戳断点续测）
needs_input / blocked   发起前的拒绝态，不产生任务记录（reuse 场景除外）
```

判定优先级（eval_status.py）：exit_code 文件 > 容器内 ais_bench 进程存活 > 启动 30 秒内宽限 > failed。

`exit_code` 由容器内 `run.sh` 的 `trap 'echo $? > exit_code' EXIT` 写入；`set -o pipefail` 保证 `ais_bench | tee` 管道中 ais_bench 的失败不被 tee 掩盖。

eval_status.py 的探测输出（除状态外）：

- **progress_stats**：解析日志尾的 `POST/RECV/FAIL/FIN` 计数行，给出 posted/finished/failed/total/percent/eta_minutes——只在推理阶段有效，任务结束后日志尾被汇总输出覆盖、返回 null（此时看 eval_result 即可）
- **服务崩溃感知**：任务 failed 时自动从评测机探测被测模型 API（curl `<api-base>/v1/models`）。服务不可达 → hints 明确提示"服务崩溃，修复后 `--reuse` 续跑"；服务可达但失败请求 ≥5 → 提示"偶发失败，可 --reuse 续跑失败部分"
- **output_ts 固化**：每次探测发现 work_dir 下的实际输出时间戳目录（容器时钟与宿主机有 ~15 分钟偏差，不能本地推算），写入任务记录 `output_ts`。eval_result 取结果、`--reuse` 续跑都优先用它精确锚定，避免"最新目录"猜测在时钟回跳时取错

## 发起语义（eval_run.py）

- **幂等性**：同一 task-id 重跑 `--reuse` 复用容器与任务目录；不带 `--reuse` 的每次调用都是新任务（新 task-id、新容器）。
- **预检只读**：SSH echo、`docker image inspect`、`test -d`、curl 模型 API，全部无副作用。模型 API 健康检查在评测机上执行（`curl <api-base>/models`），HTTP 200/401 视为服务存活。
- **参数继承**：`--reuse` 时未显式给出的 `--suite/--api-base/--model` 从原任务记录继承；`--api-key` 不继承（记录里不存），需要时重新提供。
- **容器生命周期**：任务容器 `ais-bench-eval-<task-id>` 在发起时创建（已存在且 running 则复用），任务结束后保留供排查；同一 task-id 续跑时容器已在则不重建。

## 评测形态与 ais_bench 命令映射

| suite category | 生成物 | 容器内命令形态 |
| --- | --- | --- |
| accuracy / multimodal | `cfg/models/api_model.py` | `ais_bench --config-dir <task_dir>/cfg --models api_model --datasets <任务名...> --merge-ds --dump-eval-details --work-dir <task_dir>/outputs --debug` |
| perf | 同上（stream=True） | 同上 + `-m perf` |
| agent | `config.py`（整体配置） | `ais_bench <task_dir>/config.py --work-dir <task_dir>/outputs --debug` |

续跑统一追加 `--reuse`（不带时间戳 = 取 work_dir 下最新一次运行断点续测）。

## 任务目录布局（评测机 `<task_root>/<task-id>/`）

```
cfg/models/api_model.py   # 组合式模型配置（Agent 场景无此项）
config.py                 # Agent 整体配置
run.sh                    # 实际执行体：pipefail + trap exit_code + cd 源码 + tee 日志
bootstrap.sh              # setsid 脱离 docker exec 会话，后台拉起 run.sh
aisbench.log              # 全量日志（tee 双写）
exit_code                 # 结束后写入（文件不存在 = 尚未结束）
outputs/<时间戳>/         # ais_bench 产物：results/ performances/ predictions/ summary/ status_tmp/
```

## 任务注册表（本地 `~/.ais-bench-eval/tasks.json`）

每条记录：`task_id`（`YYYYMMDD_HHMMSS_<4hex>`，支持唯一前缀引用）、`host`、`container`、`task_dir`、`work_dir`、`suite`、`category`、`api_base`、`model`、`num_prompts`、`batch_size`、`step_limit`、`max_out_len`、`tokenizer_path`、`reuse_of`、`started_at`、`status`、`last_checked`、`output_ts`（eval_status 固化的实际输出时间戳，续跑与取结果的锚点）、`output_ts_all`、`last_progress`。

**永不写入**：api_key（只在评测机 config 文件里）、日志内容、分数（分数每次实时收集，不缓存）。

## 评测机配置（`~/.ais-bench-eval/hosts.json`，可选）

内置默认 90.90.122.21（image=aisbench-swe:20260630，benchmark_src=/home/jiguang/inference/benchmark_20260630/benchmark，task_root=/tmp/ais-bench-eval）。hosts.json 按同名 host:port 覆盖任意字段：

```json
{"hosts": {"90.90.122.21": {"task_root": "/data/ais-bench-eval"}}}
```

新评测机至少提供 host/port/user/image/benchmark_src/task_root 六个字段——image 与 benchmark_src 是该机器的环境事实，预检阶段验证存在性。

## 结果收集语义（eval_result.py）

- 默认取 `work_dir` 下**最新**时间戳目录；`--all` 返回全部
- `results/*/*.json` 与 `performances/*/*.json` 只带回**标量字段**（int/float/str/bool）；`details`、`completed_ids` 等列表/字典字段在远端丢弃——完整明细保留在评测机，需要时人工查看
- 无时间戳目录 = 尚未产出结果，返回 `needs_input` 提示先查状态

## 清理边界

- skill 不自动删除任何容器或任务目录（包括已 finished 的）；用户明确要求清理时，删自建的 `ais-bench-eval-<task-id>` 容器与 `<task_root>/<task-id>/` 目录，并把任务表记录置为 `removed`
- 绝不删除评测机上他人的 aisbench-session 容器、共享 outputs、数据集与源码
