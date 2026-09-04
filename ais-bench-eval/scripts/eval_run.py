#!/usr/bin/env python3
"""发起或续跑一次 ais_bench 评测。

流程：解析 suite → 预检（SSH/镜像/源码/模型 API 可达）→ 在评测机生成
任务目录（模型配置 + 数据集覆盖 + 启动脚本）→ 自建评测容器 → 后台派活 → 注册任务并返回。

评测在评测机上后台运行，本脚本立即返回 task_id；进度用 eval_status.py 查询，
分数用 eval_result.py 提取。中断续跑：eval_run.py --reuse <task-id>。

用法（Windows 用 py -3，POSIX 用 python3，下同）：
  python eval_run.py --suite gsm8k --api-base http://10.0.0.1:8000/v1 --model qwen3-32b
  python eval_run.py --suite perf_synthetic --api-base ... --model ... --num-prompts 1000 --batch-size 32
  python eval_run.py --suite swe_bench_verified_mini --api-base ... --model ... --step-limit 100
  python eval_run.py --reuse 20260903_142500_ab12
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

# ---------------------------------------------------------------------------
# Suite 路由表：suite 名 → 评测形态。这是 eval_run 的权威数据源，
# references/benchmarks.md 里的表从这里派生，两边必须同步维护。
#
# 数据可用性分级（2026-09-03 在评测机实测）：
# - "override" 为 None：数据集配置开箱即用（包内数据完整或配置已用绝对路径）
# - "override" 非 None：包内配置的 path 指向损坏/缺失的数据（如 gsm8k 的
#   test.jsonl answer 全为 'none'），需生成覆盖配置指向 precision 数据目录；
#   三元组 = (相对包内 configs/datasets/ 的模块路径, 列表变量名, 本地数据绝对路径)
# - 未收录的数据集任务（agieval/bbh/cmmlu 等 200+ 个）：内网无数据不可离线跑，
#   全清单见 references/benchmarks.md；确需新增时先备数据再往本表登记
#
# category:
#   accuracy / multimodal —— 组合式：--models <cfg> --datasets <任务名...>
#   perf                  —— 组合式性能：流式后端 + -m perf（性能只支持流式）
#   agent                 —— 整体配置文件式：从模板派生 config.py
# model: chat=非流式 chat 接口；stream=流式 chat 接口（性能必用）
# ---------------------------------------------------------------------------

PRECISION_ROOT = "/home/aisbench_precision_dataset"

SUITES: dict[str, dict[str, Any]] = {
    # ---- 精度（accuracy）----
    "gsm8k": {"category": "accuracy", "model": "chat",
              "datasets": ["gsm8k_gen_4_shot_cot_chat_prompt"],
              "override": ("gsm8k.gsm8k_gen_4_shot_cot_chat_prompt", "gsm8k_datasets",
                           f"{PRECISION_ROOT}/gsm8k_precision")},
    "mmlu": {"category": "accuracy", "model": "chat",
             "datasets": ["mmlu_gen_0_shot_cot_chat_prompt"],
             "override": ("mmlu.mmlu_gen_0_shot_cot_chat_prompt", "mmlu_datasets",
                          f"{PRECISION_ROOT}/mmlu")},
    "ceval": {"category": "accuracy", "model": "chat",
              "datasets": ["ceval_gen_0_shot_cot_chat_prompt"],
              "override": ("ceval.ceval_gen_0_shot_cot_chat_prompt", "ceval_datasets",
                           f"{PRECISION_ROOT}/ceval")},
    "aime2024": {"category": "accuracy", "model": "chat",
                 "datasets": ["aime2024_gen_0_shot_chat_prompt"],
                 "override": ("aime2024.aime2024_gen_0_shot_chat_prompt", "aime2024_datasets",
                              f"{PRECISION_ROOT}/aime")},
    "aime2025": {"category": "accuracy", "model": "chat",
                 "datasets": ["aime2025_gen_0_shot_chat_prompt"], "override": None},
    "aime2026": {"category": "accuracy", "model": "chat",
                 "datasets": ["aime2026_gen_0_shot_chat_prompt"], "override": None},
    "gpqa": {"category": "accuracy", "model": "chat",
             "datasets": ["gpqa_gen_0_shot_cot_chat_prompt"], "override": None},
    "math500": {"category": "accuracy", "model": "chat",
                "datasets": ["math500_gen_0_shot_cot_chat_prompt"],
                "override": ("math.math500_gen_0_shot_cot_chat_prompt", "math_datasets",
                             f"{PRECISION_ROOT}/math")},
    "humaneval": {"category": "accuracy", "model": "chat",
                  "datasets": ["humaneval_gen_0_shot"],
                  "override": ("humaneval.humaneval_gen_0_shot", "humaneval_datasets",
                               f"{PRECISION_ROOT}/humaneval")},
    "mmlu_pro": {"category": "accuracy", "model": "chat",
                 "datasets": ["mmlu_pro_gen_0_shot_str"],
                 "override": ("mmlu_pro.mmlu_pro_gen_0_shot_str", "mmlu_pro_datasets",
                              f"{PRECISION_ROOT}/mmlu_pro")},
    "longbenchv2": {"category": "accuracy", "model": "chat",
                    "datasets": ["longbenchv2_gen"],
                    "override": ("longbenchv2.longbenchv2_gen", "LongBenchv2_datasets",
                                 f"{PRECISION_ROOT}/LongBench-v2")},
    "livecodebench": {"category": "accuracy", "model": "chat",
                      "datasets": ["livecodebench_code_generate_lite_gen_0_shot_chat"],
                      "override": None},
    # ---- 性能（perf）----
    "perf_synthetic": {"category": "perf", "model": "stream",
                       "datasets": ["synthetic_gen_string"], "override": None,
                       "note": "随机合成负载，需要 --num-prompts 指定条数"},
    "perf_gsm8k": {"category": "perf", "model": "stream",
                   "datasets": ["gsm8k_gen_0_shot_cot_str_perf"], "override": None,
                   "note": "性能只测吞吐/时延，不评 gold，包内数据可用"},
    "perf_sharegpt": {"category": "perf", "model": "stream",
                      "datasets": ["sharegpt_gen"],
                      "override": ("sharegpt.sharegpt_gen", "sharegpt_datasets",
                                   f"{PRECISION_ROOT}/sharegpt"),
                      "note": "多轮真实对话 trace"},
    # ---- Agent ----
    "swe_bench_verified": {"category": "agent", "template": "swe_bench",
                           "dataset_name": "verified",
                           "note": "500 实例，跑完数小时到数天"},
    "swe_bench_verified_mini": {"category": "agent", "template": "swe_bench",
                                "dataset_name": "verified_mini",
                                "note": "50 实例采样子集，适合先跑通"},
    "swe_bench_multilingual": {"category": "agent", "template": "swe_bench",
                               "dataset_name": "multilingual", "note": "300 实例"},
    "terminal_bench_2": {"category": "agent", "template": "harbor_terminal",
                         "dataset_name": "terminal_bench_2",
                         "note": "96 任务全集，经 Harbor 框架跑，需构建环境镜像"},
    "terminal_bench_2_mini": {"category": "agent", "template": "harbor_terminal",
                              "dataset_name": "terminal_bench_2_mini",
                              "note": "14 任务离线采样子集"},
    "tau2_bench": {"category": "agent", "template": "tau2", "dataset_name": "tau2",
                   "note": "数据随 tau2-bench 包内置，就绪性未实测验证"},
    # ---- 多模态（multimodal）----
    "mmmu": {"category": "multimodal", "model": "chat", "datasets": ["mmmu_gen"],
             "override": None},
}

# Agent 模板需要的本地数据落位（SWE-bench 系列为 parquet 数据目录；
# lite/full 未收录：本机无数据且内网无法从 HF 下载）。
AGENT_DATASET_PATHS: dict[str, str] = {
    "verified": "/home/jiguang/inference/SWE-bench/SWE-bench_Verified",
    "verified_mini": "/home/jiguang/inference/SWE-Bench_Verified_mini/SWE-Bench_Verified_selected_0.05",
    "multilingual": "/home/jiguang/inference/SWE-bench/SWE-bench_Multilingual",
    "terminal_bench_2": "/home/terminal-bench-2",
    "terminal_bench_2_mini": "/home/terminal-bench-2-offline-mini/terminal-bench-2-offline-selected_0.20",
}

# ---------------------------------------------------------------------------
# 配置模板
# ---------------------------------------------------------------------------

# 组合式模型配置：与包内 configs/models/vllm_api/*.py 同构。
CHAT_MODEL_TEMPLATE = """\
from ais_bench.benchmark.models import VLLMCustomAPIChat
from ais_bench.benchmark.utils.postprocess.model_postprocessors import extract_non_reasoning_content

models = [
    dict(
        attr="service",
        type=VLLMCustomAPIChat,
        abbr="{abbr}",
        path="{path}",
        model="{model}",
        stream={stream},
        request_rate=0,
        use_timestamp=False,
        retry=2,
        api_key="{api_key}",
        url="{api_base}",
        max_out_len={max_out_len},
        batch_size={batch_size},
        trust_remote_code=False,
        generation_kwargs=dict(
            temperature=0.01,
            ignore_eos=False,
        ),
        pred_postprocessor=dict(type=extract_non_reasoning_content),
    )
]
"""

# 数据集覆盖配置：read_base 继承包内配置后把 path 改成本地 precision 数据。
# for 循环写法对单元素列表（gsm8k）和循环构建的多科目列表（mmlu 57 科目）都适用。
DATASET_OVERRIDE_TEMPLATE = """\
from mmengine import read_base

with read_base():
    from ais_bench.benchmark.configs.datasets.{module} import {var}

for _ds in {var}:
    _ds['path'] = {data_path!r}
"""

# SWE-bench 整体配置（内嵌 mini-swe-agent + LiteLLMChat），import 用包聚合入口
# （与官方模板 mini_swe_agent_swe_bench_lite.py 一致）。
SWEBENCH_TEMPLATE = """\
from ais_bench.benchmark.datasets import SWEBenchDataset
from ais_bench.benchmark.partitioners import NaivePartitioner
from ais_bench.benchmark.runners import LocalRunner
from ais_bench.benchmark.tasks import SWEBenchInferTask, SWEBenchEvalTask
from ais_bench.benchmark.summarizers import SWEBenchSummarizer

STEP_LIMIT = {step_limit}

models = [
    dict(
        attr="local",
        abbr="swebench",
        type="LiteLLMChat",
        model="{model}",
        api_key="{api_key}",
        url="{api_base}",
        batch_size={batch_size},
        generation_kwargs=dict(temperature=0.6, top_p=0.95),
    )
]

datasets = [
    dict(
        type=SWEBenchDataset,
        abbr="swe_bench_{dataset_name}",
        path="{dataset_path}",
        name="{dataset_name}",
        split="test",
        filter_spec="",
        shuffle=False,
        step_limit=STEP_LIMIT,
    )
]

summarizer = dict(attr="accuracy", type=SWEBenchSummarizer)

infer = dict(
    partitioner=dict(type=NaivePartitioner),
    runner=dict(type=LocalRunner, task=dict(type=SWEBenchInferTask)),
)

eval = dict(
    partitioner=dict(type=NaivePartitioner),
    runner=dict(type=LocalRunner, task=dict(type=SWEBenchEvalTask)),
)
"""

# Terminal-Bench 2（经 HarborTask，terminus-2 agent），字段对应官方
# configs/agent_example/harbor_terminal_bench_2_task.py，只保留可调参数。
HARBOR_TEMPLATE = """\
from ais_bench.benchmark.tasks.custom_tasks.harbor_task import HarborTask
from ais_bench.benchmark.tasks.base import EmptyTask
from ais_bench.benchmark.summarizers.harbor import HarborSummarizer

models = [
    dict(
        abbr="terminus-2",
        agent_name="terminus-2",
        model_names=["hosted_vllm/{model}"],
        agent_kwargs=dict(
            api_base="{api_base}",
            model_info=dict(
                max_input_tokens=128000,
                max_output_tokens={max_out_len},
                input_cost_per_token=0.0,
                output_cost_per_token=0.0,
            ),
            llm_call_kwargs=dict(
                max_tokens={max_out_len},
            ),
        ),
        agent_env=None,
    )
]

datasets = [
    dict(
        abbr="harbor_terminal-bench-2",
        args=dict(
            n_attempts=1,
            n_concurrent_trials={batch_size},
            environment_type="docker",
            environment_delete=False,
            path="{dataset_path}",
            yes=True,
        ),
    )
]

infer = dict(runner=dict(task=dict(type=EmptyTask)))

eval = dict(runner=dict(task=dict(type=HarborTask)))

summarizer = dict(attr="accuracy", type=HarborSummarizer)
"""

# tau2-bench（TAU2BenchTask）：agent 与 user 都用被测模型。
TAU2_TEMPLATE = """\
from ais_bench.benchmark.tasks.custom_tasks.tau2_bench_task import TAU2BenchTask
from ais_bench.benchmark.tasks.base import EmptyTask

models = [
    dict(
        abbr="tau2-openai-v1-chat",
        api_key=None,
        agent=None,
        llm_agent="openai/{model}",
        llm_args_agent=dict(
            api_base="{api_base}",
            temperature=0.5,
        ),
    )
]

datasets = [
    dict(
        abbr="tau2_bench_airline",
        args=dict(
            domain="airline",
            num_trials=1,
            llm_user="openai/{model}",
            llm_args_user=dict(
                api_base="{api_base}",
                temperature=0.0,
            ),
            max_concurrency={batch_size},
        ),
    )
]

infer = dict(runner=dict(task=dict(type=EmptyTask)))

eval = dict(runner=dict(task=dict(type=TAU2BenchTask)))

summarizer = dict(attr="accuracy")
"""

# 容器挂载（与评测机现有 aisbench-session-* 容器一致：源码、数据、
# docker.sock 都来自宿主机；SWE-bench 评测要经 sock 起 sibling 容器）。
CONTAINER_MOUNTS = [
    "/home:/home",
    "/data:/data",
    "/tmp:/tmp",
    "/mnt:/mnt",
    "/usr/bin/docker:/usr/bin/docker",
    "/var/run/docker.sock:/var/run/docker.sock",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="发起或续跑 ais_bench 评测")
    parser.add_argument("--host", default="90.90.122.21", help="评测机（host/alias，默认 90.90.122.21）")
    parser.add_argument("--suite", choices=sorted(SUITES.keys()), help="评测套件名，全表见 references/benchmarks.md")
    parser.add_argument("--api-base", help="被测模型 OpenAI 兼容 base url，如 http://10.0.0.1:8000/v1")
    parser.add_argument("--model", help="服务端模型名（/v1/models 里显示的名字）")
    parser.add_argument("--api-key", default="EMPTY", help="API key（默认 EMPTY；不会写入本地任务表）")
    parser.add_argument("--num-prompts", type=int, help="只取数据集前 N 条（快速验证/性能压测条数）")
    parser.add_argument("--batch-size", type=int, default=None, help="请求并发数；Agent 场景=并发实例数")
    parser.add_argument("--max-out-len", type=int, default=None, help=f"单请求最大输出 token 数（默认 {DEFAULT_MAX_OUT_LEN}，thinking 模型勿低于此值）")
    parser.add_argument("--tokenizer-path", default=None,
                        help="性能场景的本地 tokenizer 路径（评测机上；默认取评测机配置的 default_tokenizer）")
    parser.add_argument("--step-limit", type=int, default=None, help="Agent 每实例步数上限（默认 200，SWE-bench 用）")
    parser.add_argument("--extra-args", default="", help="透传给 ais_bench 的额外参数（原样拼接）")
    parser.add_argument("--reuse", metavar="TASK_ID", help="续跑已有任务（复用其容器与任务目录）")
    parser.add_argument("--dry-run", action="store_true", help="只输出将要执行的命令与生成的配置，不实际执行")
    return parser.parse_args()


def remote_write(machine: dict[str, Any], path: str, content: str) -> tuple[bool, str]:
    """在评测机上写一个文本文件（base64 传输，避开多层引号转义）。"""
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    parent = str(Path(path).parent).replace("\\", "/")
    command = (
        f"mkdir -p {common.sh_quote(parent)} && "
        f"echo {payload} | base64 -d > {common.sh_quote(path)} && "
        f"echo written"
    )
    code, out, err = common.run_ssh(machine, command, timeout=30)
    if code == 0 and "written" in out:
        return True, ""
    return False, (err or out or f"exit {code}").strip()[:300]


def default_batch_size(suite: dict[str, Any]) -> int:
    """并发默认值：对齐常见 vllm 服务能力（max-num-seqs 128），
    评测请求打到 16/32 并发既快又不至于压垮被测服务；压测特定并发用 --batch-size 显式给。"""
    if suite["category"] == "agent":
        return 60
    return 32 if suite["category"] == "perf" else 16


# max_out_len 默认值。512 会在 thinking 模型（qwen3.5 等）思考完成前截断、
# 严重扭曲精度分（实测 gsm8k 20 条：512→25%，2048→65%），2048 起步。
# 注意不要超过被测服务的 max_model_len（/v1/models 可查）。
DEFAULT_MAX_OUT_LEN = 2048


def strip_v1(api_base: str) -> str:
    """剥掉 base url 尾部的 /v1。

    VLLMCustomAPI 系列的 url 字段语义是"到端口为止"（内部 urljoin 拼上
    v1/chat/completions）；用户口头给的地址习惯带 /v1，这里统一剥掉。
    Agent 模板（LiteLLMChat/Harbor/tau2）的 api_base 语义相反，要带 /v1，
    走 normalize_api_base 之外的路径，不受此函数影响。
    """
    base = (api_base or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def models_url(api_base: str) -> str:
    """健康检查 URL：无论用户给不给 /v1，都探测 <base>/v1/models。"""
    return strip_v1(api_base).rstrip("/") + "/v1/models"


def build_model_config(args: argparse.Namespace, suite: dict[str, Any], abbr: str, machine: dict[str, Any]) -> str:
    # 性能场景的 default_perf 汇总要 tokenizer 算 token 数，path 必须真实存在；
    # 精度场景不加载 tokenizer，path 留空。
    if suite["category"] == "perf":
        tokenizer_path = args.tokenizer_path or machine.get("default_tokenizer", "")
        if not tokenizer_path:
            raise ValueError("性能场景需要 --tokenizer-path（或评测机配置 default_tokenizer）")
    else:
        tokenizer_path = ""
    return CHAT_MODEL_TEMPLATE.format(
        abbr=abbr,
        model=args.model or "",
        stream="True" if suite.get("model") == "stream" else "False",
        api_key=args.api_key,
        api_base=strip_v1(args.api_base),
        max_out_len=args.max_out_len or DEFAULT_MAX_OUT_LEN,
        batch_size=args.batch_size if args.batch_size is not None else default_batch_size(suite),
        path=tokenizer_path,
    )


def build_dataset_override(suite: dict[str, Any]) -> str | None:
    """生成数据集覆盖配置（--config-dir 优先级高于包内默认，同名任务名生效）。"""
    override = suite.get("override")
    if not override:
        return None
    module, var, data_path = override
    return DATASET_OVERRIDE_TEMPLATE.format(module=module, var=var, data_path=data_path)


def build_agent_config(args: argparse.Namespace, suite: dict[str, Any]) -> str:
    template_name = suite["template"]
    dataset_name = suite["dataset_name"]
    dataset_path = AGENT_DATASET_PATHS.get(dataset_name, "")
    batch_size = args.batch_size if args.batch_size is not None else default_batch_size(suite)
    # Agent 模板（LiteLLMChat/Harbor/tau2）的 api_base 语义要带 /v1，与 VLLMCustomAPI 相反
    api_base = args.api_base.rstrip("/")
    if not api_base.endswith("/v1"):
        api_base += "/v1"
    if template_name == "swe_bench":
        if not dataset_path:
            raise ValueError(f"swe_bench 数据集 {dataset_name} 的本地路径未登记")
        return SWEBENCH_TEMPLATE.format(
            step_limit=args.step_limit or 200,
            model=args.model or "",
            api_key=args.api_key,
            api_base=api_base,
            batch_size=batch_size,
            dataset_name=dataset_name,
            dataset_path=dataset_path,
        )
    if template_name == "harbor_terminal":
        if not dataset_path:
            raise ValueError(f"terminal_bench 数据集 {dataset_name} 的本地路径未登记")
        return HARBOR_TEMPLATE.format(
            model=args.model or "",
            api_base=api_base,
            max_out_len=args.max_out_len or DEFAULT_MAX_OUT_LEN,
            batch_size=batch_size,
            dataset_path=dataset_path,
        )
    if template_name == "tau2":
        return TAU2_TEMPLATE.format(
            model=args.model or "",
            api_base=api_base,
            batch_size=batch_size,
        )
    raise ValueError(f"未知 agent 模板：{template_name}")


def build_ais_bench_command(task_dir: str, work_dir: str, args: argparse.Namespace, suite: dict[str, Any],
                            reuse_record: dict[str, Any] | None = None) -> str:
    """构造容器内执行的 ais_bench 命令行。"""
    parts = ["ais_bench"]
    if suite["category"] == "agent":
        parts.append(f"{task_dir}/config.py")
    else:
        parts.append(f"--config-dir {task_dir}/cfg")
        parts.append("--models api_model")
        parts.append("--datasets " + " ".join(suite["datasets"]))
        if suite["category"] == "perf":
            parts.append("-m perf")
        else:
            # 精度场景合并同类子数据集（mmlu/ceval 多科目）并落评估明细
            parts.append("--merge-ds")
            parts.append("--dump-eval-details")
    if args.num_prompts:
        parts.append(f"--num-prompts {args.num_prompts}")
    # 注意：并发数（batch_size）是模型配置字段而不是 CLI 参数，命令行传 --batch-size 会报错
    if args.extra_args:
        parts.append(args.extra_args)
    parts.append(f"--work-dir {work_dir}")
    parts.append("--debug")
    if args.reuse:
        # 带时间戳的 --reuse 精确锚定输出目录；评测容器时钟有偏差，
        # 裸 --reuse 取"最新目录"在时钟回跳时会续错目录。记录里没有时才裸续。
        output_ts = (reuse_record or {}).get("output_ts")
        parts.append(f"--reuse {output_ts}" if output_ts else "--reuse")
    return " ".join(parts)


def preflight(machine: dict[str, Any], args: argparse.Namespace) -> tuple[bool, str, list[str]]:
    """预检：SSH / 镜像 / 源码 / 模型 API。返回 (ok, 错误说明, 检查详情列表)。"""
    details: list[str] = []
    ok, err = common.ssh_ok(machine)
    if not ok:
        return False, f"评测机 SSH 不可达：{err}", details
    details.append(f"ssh ok: {machine['host']}")

    code, out, _ = common.run_ssh(machine, f"docker image inspect {common.sh_quote(machine['image'])} --format '{{{{.Id}}}}'")
    if code != 0:
        return False, f"评测机缺少镜像 {machine['image']}", details
    details.append(f"image ok: {machine['image']}")

    code, out, _ = common.run_ssh(machine, f"test -d {common.sh_quote(machine['benchmark_src'])} && echo yes")
    if code != 0 or "yes" not in out:
        return False, f"评测机缺少 AISBench 源码目录 {machine['benchmark_src']}", details
    details.append(f"source ok: {machine['benchmark_src']}")

    # 模型 API 健康检查在评测机上做（本地通不代表评测机网络可达）。
    if args.api_base:
        header = ""
        if args.api_key and args.api_key != "EMPTY":
            header = f" -H {common.sh_quote('Authorization: Bearer ' + args.api_key)}"
        code, out, _ = common.run_ssh(
            machine,
            f"curl -sS -m 10 -o /dev/null -w '%{{http_code}}' {header} {common.sh_quote(models_url(args.api_base))}",
            timeout=30,
        )
        status = out.strip()
        if code != 0 or status not in ("200", "401"):
            return False, f"评测机访问模型 API 失败：{args.api_base} -> http {status or 'error'}（评测机到模型服务的网络/服务本身需排查）", details
        details.append(f"model api ok: {args.api_base} -> http {status}")
    return True, "", details


def main() -> int:
    args = parse_args()
    machine = common.find_host(args.host)
    if machine is None:
        return common.emit({"ok": False, "action": "run", "status": "needs_input",
                            "error": f"评测机 {args.host} 不在配置中；内置支持 {sorted(common.DEFAULT_HOSTS.keys())}，"
                                     f"其他机器请在 ~/.ais-bench-eval/hosts.json 登记"})

    # ---- 续跑：从任务记录恢复参数 ----
    reuse_record: dict[str, Any] | None = None
    if args.reuse:
        reuse_record = common.find_task(args.reuse)
        if reuse_record is None:
            return common.emit({"ok": False, "action": "run", "status": "needs_input",
                                "error": f"任务 {args.reuse} 不存在或前缀不唯一"})
        # 未显式给定的评测参数全部从原任务继承，续跑语义 = 原任务参数下断点继续。
        # api_key 不在记录里（安全边界），需要时重新提供。
        for field in ("suite", "api_base", "model"):
            if getattr(args, field) is None:
                setattr(args, field, reuse_record.get(field))
        for field in ("num_prompts", "batch_size", "step_limit", "max_out_len", "tokenizer_path"):
            if getattr(args, field) is None:
                setattr(args, field, reuse_record.get(field))
        common.progress("reuse", f"复用任务 {reuse_record['task_id']} 的参数")

    if not args.suite:
        return common.emit({"ok": False, "action": "run", "status": "needs_input",
                            "error": "缺少 --suite；可用值见 references/benchmarks.md 或 --help"})
    suite = SUITES.get(args.suite)
    if suite is None:
        return common.emit({"ok": False, "action": "run", "status": "needs_input",
                            "error": f"未知 suite：{args.suite}"})

    # 所有形态都需要模型 API 信息。
    if not args.api_base or not args.model:
        return common.emit({"ok": False, "action": "run", "status": "needs_input",
                            "error": "需要 --api-base 与 --model（被测模型服务的 OpenAI 兼容地址与模型名）"})

    task_id = reuse_record["task_id"] if reuse_record else common.new_task_id()
    task_dir = f"{machine['task_root']}/{task_id}"
    work_dir = f"{task_dir}/outputs"
    container = f"ais-bench-eval-{task_id}"
    abbr = (args.model or "model").replace("/", "_").replace(":", "_")

    common.progress("preflight", f"预检 {machine['host']}")
    ok, error, details = preflight(machine, args)
    if not ok:
        return common.emit({"ok": False, "action": "run", "status": "blocked",
                            "task_id": task_id, "error": error, "checks": details})

    # ---- 生成配置与脚本 ----
    generated: dict[str, str] = {}
    if suite["category"] == "agent":
        try:
            generated[f"{task_dir}/config.py"] = build_agent_config(args, suite)
        except ValueError as exc:
            return common.emit({"ok": False, "action": "run", "status": "needs_input", "error": str(exc)})
    else:
        try:
            generated[f"{task_dir}/cfg/models/api_model.py"] = build_model_config(args, suite, abbr, machine)
        except ValueError as exc:
            return common.emit({"ok": False, "action": "run", "status": "needs_input", "error": str(exc)})
        override = build_dataset_override(suite)
        if override:
            # 覆盖配置文件名必须与 --datasets 任务名一致才会优先生效
            generated[f"{task_dir}/cfg/datasets/{suite['datasets'][0]}.py"] = override

    ais_command = build_ais_bench_command(task_dir, work_dir, args, suite, reuse_record)
    run_sh = (
        "#!/bin/bash\n"
        f"# ais-bench-eval task {task_id}\n"
        "set -o pipefail\n"
        f"trap 'echo $? > {task_dir}/exit_code' EXIT\n"
        f"export PATH={machine.get('python_bin', '/usr/local/python3.11.10/bin')}:$PATH\n"
        # 镜像里的 torch 是 NPU 版，会自动加载 torch_npu 后端扩展；
        # 评测机无 NPU 硬件时加载即崩（API 模型评测不需要 NPU，禁用自动加载）。
        "export TORCH_DEVICE_BACKEND_AUTOLOAD=0\n"
        f"cd {machine['benchmark_src']}\n"
        f"{ais_command} 2>&1 | tee {task_dir}/aisbench.log\n"
    )
    bootstrap_sh = (
        "#!/bin/bash\n"
        f"setsid bash {task_dir}/run.sh < /dev/null > /dev/null 2>&1 &\n"
        f"echo $! > {task_dir}/bootstrap.pid\n"
    )
    generated[f"{task_dir}/run.sh"] = run_sh
    generated[f"{task_dir}/bootstrap.sh"] = bootstrap_sh

    if args.dry_run:
        return common.emit({"ok": True, "action": "run", "status": "needs_input", "dry_run": True,
                            "task_id": task_id, "container": container,
                            "files": generated, "ais_bench_command": ais_command,
                            "note": "dry-run：以上为将写入评测机的文件与将执行的命令"})

    # ---- 写文件 ----
    common.progress("config", f"写入任务目录 {task_dir}")
    for path, content in generated.items():
        ok, error = remote_write(machine, path, content)
        if not ok:
            return common.emit({"ok": False, "action": "run", "status": "blocked",
                                "task_id": task_id, "error": f"写入 {path} 失败：{error}"})

    # ---- 容器：已存在且 running 则复用（续跑场景），否则自建 ----
    common.progress("container", container)
    code, out, _ = common.run_ssh(machine, f"docker inspect {container} --format '{{{{.State.Running}}}}' 2>/dev/null")
    if "true" not in out:
        mounts = " ".join(f"-v {m}" for m in CONTAINER_MOUNTS)
        code, out, err = common.run_ssh(
            machine,
            f"docker rm -f {container} >/dev/null 2>&1; "
            f"docker run -d --name {container} --network host {mounts} "
            f"{common.sh_quote(machine['image'])} sleep infinity",
            timeout=120,
        )
        if code != 0:
            return common.emit({"ok": False, "action": "run", "status": "blocked",
                                "task_id": task_id, "error": f"容器启动失败：{(err or out).strip()[:300]}"})

    # ---- 派活：setsid 后台执行，exit_code 由 trap 落盘 ----
    common.progress("launch", "docker exec 后台启动 ais_bench")
    code, out, err = common.run_ssh(
        machine, f"docker exec {container} bash {task_dir}/bootstrap.sh", timeout=60)
    if code != 0:
        return common.emit({"ok": False, "action": "run", "status": "blocked",
                            "task_id": task_id, "error": f"派活失败：{(err or out).strip()[:300]}"})

    record = {
        "task_id": task_id,
        "host": machine["host"],
        "container": container,
        "task_dir": task_dir,
        "work_dir": work_dir,
        "suite": args.suite,
        "category": suite["category"],
        "api_base": args.api_base,
        "model": args.model,
        "num_prompts": args.num_prompts,
        "batch_size": args.batch_size,
        "step_limit": args.step_limit,
        "max_out_len": args.max_out_len,
        "tokenizer_path": args.tokenizer_path,
        "reuse_of": reuse_record["task_id"] if reuse_record else None,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": "running",
    }
    common.upsert_task(record)
    return common.emit({
        "ok": True, "action": "run", "status": "running", "task": record,
        "note": suite.get("note"),
        "next": [f"python eval_status.py {task_id}   # 查进度",
                 f"python eval_result.py {task_id}   # 跑完后提取分数"],
    })


if __name__ == "__main__":
    sys.exit(main())
