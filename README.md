# awesome-agent-skills

面向真实工程问题的 Agent Skills 集合。

## Skills

- [`vllm-ascend-accuracy`](vllm-ascend-accuracy/)：进入本地或远程容器，复现、诊断、迭代修复并验证 vLLM Ascend 的乱码、复读及推理精度问题。
- [`server-management`](server-management/)：管理服务器清单——密码一次性引导公钥后永久密钥认证地添加/移除/验证服务器，并提供 NPU 集群状态常驻监控与查询。
- [`npu-migrate`](npu-migrate/)：把源服务器的容器和代码文件夹迁移到有空闲 NPU 卡的服务器并自动拉起服务，依赖 server-management 的机器清单与空闲状态。
- [`disk-cleanup`](disk-cleanup/)：服务器磁盘空间分析与清理——docker 镜像/容器分级清理（安全级/确认级），共享节点安全边界内执行，依赖 server-management。

每个一级目录都是一个可独立安装的 skill，入口文件为该目录中的 `SKILL.md`。

## 各 Skill 调用示例

向 Agent 直接描述任务即可自动触发对应 skill（也可以用 `/skill-name` 显式调用）：

### vllm-ascend-accuracy

> 80.5.9.126 容器里的 vllm 输出乱码/复读，帮我排查修复。验收标准：xxx 数据集分数恢复到 0.85 以上。

### server-management

> 帮我加台服务器 10.0.0.1，密码是 xxx
>
> 现在集群里哪些机器有空闲卡？
>
> 把 10.0.0.5 这台机器移除

### npu-migrate

> 10.0.0.1 的卡被占了，把 my-vllm 容器和 /mnt/code 迁到有空卡的机器上，权重在 /data/weights/MyModel

### disk-cleanup

> 10.0.0.1 磁盘满了，帮我看看能清什么

## 安装

最简单的方式：**把仓库地址发给 Agent（Codex 或 Claude Code），让它自己装**。

安装全部 skill：

```text
把 https://github.com/zhaochuang001/awesome-agent-skills 里的所有 skill 安装到你的 skills 目录
```

安装指定 skill：

```text
把 https://github.com/zhaochuang001/awesome-agent-skills/tree/main/vllm-ascend-accuracy 安装到你的 skills 目录
```

Codex 也可以用内置安装器：输入 `$skill-installer` 并提供上面的目录地址。

安装完成后**新开会话**即可使用（skill 列表在会话启动时加载）；之后直接描述任务触发，或用 `/skill-name` 显式调用。不经过 Agent 的环境参考下方「手动安装」。

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
