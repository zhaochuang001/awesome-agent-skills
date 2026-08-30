# awesome-agent-skills

面向真实工程问题的 Agent Skills 集合。

## Skills

- [`vllm-ascend-accuracy`](vllm-ascend-accuracy/)：进入本地或远程容器，复现、诊断、迭代修复并验证 vLLM Ascend 的乱码、复读及推理精度问题。

每个一级目录都是一个可独立安装的 skill，入口文件为该目录中的 `SKILL.md`。

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

将整个 skill 目录复制到以下任一位置：

- 用户级：`~/.agents/skills/<skill-name>/`
- 项目级：`<project>/.agents/skills/<skill-name>/`

例如在 macOS/Linux 上安装到用户目录：

```bash
git clone https://github.com/zhaochuang001/awesome-agent-skills.git
mkdir -p ~/.agents/skills
cp -R awesome-agent-skills/vllm-ascend-accuracy ~/.agents/skills/
```

在 Windows PowerShell 中：

```powershell
git clone https://github.com/zhaochuang001/awesome-agent-skills.git
New-Item -ItemType Directory -Force "$env:USERPROFILE\.agents\skills" | Out-Null
Copy-Item -Recurse ".\awesome-agent-skills\vllm-ascend-accuracy" "$env:USERPROFILE\.agents\skills\"
```

## Claude Code 安装与使用

Claude Code 可以从用户目录或项目目录加载 skills：

- 用户级：`~/.claude/skills/<skill-name>/`
- 项目级：`<project>/.claude/skills/<skill-name>/`

### macOS/Linux

```bash
git clone https://github.com/zhaochuang001/awesome-agent-skills.git
mkdir -p ~/.claude/skills
cp -R awesome-agent-skills/vllm-ascend-accuracy ~/.claude/skills/
```

### Windows PowerShell

```powershell
git clone https://github.com/zhaochuang001/awesome-agent-skills.git
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills" | Out-Null
Copy-Item -Recurse ".\awesome-agent-skills\vllm-ascend-accuracy" "$env:USERPROFILE\.claude\skills\"
```

在 Claude Code 中显式调用：

```text
/vllm-ascend-accuracy 帮我修复一个 vLLM Ascend 精度问题
```

Claude Code 也可以根据 skill 的 `description` 自动调用。使用 `/skills` 检查是否加载成功；如果 skills 顶层目录是在当前会话启动后首次创建的，请重启 Claude Code。

## 安装其他 Skills

将示例中的 `vllm-ascend-accuracy` 替换为本仓库其他包含 `SKILL.md` 的目录名即可。安装时必须复制整个目录，不能只复制 `SKILL.md`，否则 `references/`、`scripts/` 和其他资源将不可用。

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
