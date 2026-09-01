# awesome-agent-skills

面向真实工程问题的 Agent Skills 集合。

## Skills

- [`vllm-ascend-accuracy`](vllm-ascend-accuracy/)：进入本地或远程容器，复现、诊断、迭代修复并验证 vLLM Ascend 的乱码、复读及推理精度问题。
- [`server-management`](server-management/)：管理服务器清单——密码一次性引导公钥后永久密钥认证地添加/移除/验证服务器，并提供 NPU 集群状态常驻监控与查询。
- [`npu-migrate`](npu-migrate/)：把源服务器的容器和代码文件夹迁移到有空闲 NPU 卡的服务器并自动拉起服务，依赖 server-management 的机器清单与空闲状态。
- [`disk-cleanup`](disk-cleanup/)：服务器磁盘空间分析与清理——docker 镜像/容器分级清理（安全级/确认级），共享节点安全边界内执行，依赖 server-management。

每个一级目录都是一个可独立安装的 skill，入口文件为该目录中的 `SKILL.md`。

## 各 Skill 调用示例

四种触发方式从左到右递进：**自然语言**（Agent 按 `description` 自动选择）→ **显式调用**（`/skill-name`）→ **命令行**（直接跑 skill 自带脚本，Windows 用 `py -3`，macOS/Linux 用 `python3`，下文以 `py -3` 为例；脚本路径相对 skill 根目录）。

### vllm-ascend-accuracy

纯指令型 skill（无独立脚本），描述任务即可触发：

```text
80.5.9.126 容器里的 vllm 输出乱码/复读，帮我排查修复。
验收标准：xxx 数据集分数恢复到 0.85 以上。
```

### server-management

```bash
# 添加服务器（密码仅本次使用，装完公钥后永久密钥认证）
py -3 scripts/machine_add.py --host 10.0.0.1 --password 'xxx'

# 验证 / 移除
py -3 scripts/machine_verify.py --machine 10.0.0.1
py -3 scripts/machine_remove.py --machine 10.0.0.1

# 查询集群：哪些机器有 ≥4 张空闲卡
py -3 scripts/fleet_cli.py capacity --min-idle 4

# 启动监控面板（浏览器打开 http://127.0.0.1:8790）
py -3 scripts/fleet_manage.py start
```

自然语言："帮我加台服务器 10.0.0.1，密码 xxx"、"现在哪些机器有空闲卡？"

### npu-migrate

```bash
# 先干跑看方案（首次迁移推荐：输出目标机选择、卡映射、将执行的命令）
py -3 scripts/migrate.py --source 10.0.0.1 --container my-vllm \
  --code-path /mnt/code --script /workspace/start.sh --plan

# 正式迁移：自动选空闲卡最多的机器，起容器前检查目标机模型权重
py -3 scripts/migrate.py --source 10.0.0.1 --container my-vllm \
  --code-path /mnt/code --script /workspace/start.sh \
  --weights-path /data/weights/MyModel
```

自然语言："10.0.0.1 的卡被占了，把 my-vllm 容器和 /mnt/code 迁到有空卡的机器，权重在 /data/weights/MyModel"

### disk-cleanup

```bash
# 只读分析：磁盘全景 + docker 镜像/容器分级清理候选（不动任何东西）
py -3 scripts/cleanup.py --host 10.0.0.1

# 无损清理：悬空镜像 + 构建缓存
py -3 scripts/cleanup.py --host 10.0.0.1 --execute-safe

# 确认级清理：删退出超 9 天的容器（先看分析报告再执行）
py -3 scripts/cleanup.py --host 10.0.0.1 --execute-confirm --days 9
```

自然语言："10.0.0.1 磁盘满了，帮我看看能清什么"

## Codex 安装与使用

### 使用 Skill Installer（推荐）

在 Codex 中调用内置安装器，并提供需要安装的 skill 的 GitHub 目录地址：

```text
$skill-installer

请从下面的 GitHub 地址安装 skill：
https://github.com/zhaochuang001/awesome-agent-skills/tree/main/vllm-ascend-accuracy
```

安装完成后，在下一条消息中显式调用：

```text
$vllm-ascend-accuracy 帮我修复一个 vLLM Ascend 精度问题
```

也可以直接描述匹配的任务，让 Codex 根据 `description` 自动选择 skill。输入 `$` 或使用 `/skills` 可以查看当前可用的 skills；新安装的 skill 未出现时，请新建任务或重启 Codex。

### 手动安装

不使用安装器时，将整个 skill 目录复制到 Codex 的 skills 目录：用户级 `~/.agents/skills/<skill-name>/`，项目级 `<project>/.agents/skills/<skill-name>/`。命令见下方[「手动安装」](#手动安装)。

## Claude Code 安装与使用

将整个 skill 目录复制到 Claude Code 的 skills 目录：用户级 `~/.claude/skills/<skill-name>/`，项目级 `<project>/.claude/skills/<skill-name>/`。命令见下方[「手动安装」](#手动安装)。

在 Claude Code 中显式调用：

```text
/vllm-ascend-accuracy 帮我修复一个 vLLM Ascend 精度问题
```

Claude Code 也可以根据 skill 的 `description` 自动调用。使用 `/skills` 检查是否加载成功；如果 skills 顶层目录是在当前会话启动后首次创建的，请重启 Claude Code。

## 手动安装

Codex 与 Claude Code 的手动安装步骤相同，仅 skills 目录不同。安装时必须复制整个目录，不能只复制 `SKILL.md`，否则 `references/`、`scripts/` 和其他资源将不可用；安装本仓库其他 skill 时，将命令中的 `vllm-ascend-accuracy` 替换为对应目录名。

macOS/Linux（以 Codex 为例，Claude Code 将 `SKILLS_ROOT` 改为 `~/.claude/skills`）：

```bash
git clone https://github.com/zhaochuang001/awesome-agent-skills.git
SKILLS_ROOT=~/.agents/skills        # Codex；Claude Code 改为 ~/.claude/skills
mkdir -p "$SKILLS_ROOT"
cp -R awesome-agent-skills/vllm-ascend-accuracy "$SKILLS_ROOT/"
```

Windows PowerShell（以 Codex 为例，Claude Code 将 `$SkillsRoot` 改为 `"$env:USERPROFILE\.claude\skills"`）：

```powershell
git clone https://github.com/zhaochuang001/awesome-agent-skills.git
$SkillsRoot = "$env:USERPROFILE\.agents\skills"   # Codex；Claude Code 改为 .claude\skills
New-Item -ItemType Directory -Force $SkillsRoot | Out-Null
Copy-Item -Recurse ".\awesome-agent-skills\vllm-ascend-accuracy" $SkillsRoot
```

## 更新

拉取本仓库最新版本后，重新复制对应 skill 目录即可：

```bash
cd awesome-agent-skills
git pull
```

开发者也可以将 skill 目录软链接到个人 skills 目录，这样仓库更新后会立即生效。Codex 与 Claude Code 都支持链接形式的 skill 目录。

## 安全提示

安装前请审查 `SKILL.md`、脚本和引用文件。涉及远程服务器时，不要把密码、私钥、token 或其他凭据提交到本仓库；仅在受控会话中临时用于认证。

## 参考文档

- [Codex：Build skills](https://learn.chatgpt.com/docs/build-skills)
- [Claude Code：Extend Claude with skills](https://code.claude.com/docs/en/skills)
