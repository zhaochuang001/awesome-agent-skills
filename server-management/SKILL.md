---
name: server-management
description: 管理用户的服务器清单：添加服务器（用密码一次性安装公钥后永久转为密钥认证）、移除服务器、验证单机健康、以及查询整个 NPU 集群的卡数/空闲/健康状态。适用于"配置服务器/加一台机器/推公钥/删机器/查服务器/NPU 状态/找有空闲卡的机器"等请求；不用于在服务器上部署应用、同步代码或跑业务任务。
---

# 服务器管理

维护用户的一组远程服务器（重点支持昇腾 NPU 机器，兼容普通 Linux 主机），提供四个动作：**add（添加）、remove（移除）、verify（单机验证）、fleet（集群查询）**。

状态存放在 `~/.server-management/`（独立于任何工作区，升级 skill 不丢数据）。

## 动作路由

收到请求先归入以下四类，再选对应脚本。跨平台启动规则：Windows 用 `py -3`，macOS/Linux 用 `python3`（下文统一写 `python`，按平台替换）。

| 意图 | 脚本 | 语义 |
| --- | --- | --- |
| 添加/配置服务器（用户给了 IP，可能附带密码） | `scripts/machine_add.py --host <ip> [--user root] [--port 22] [--alias <名>]` + 密码参数 | 幂等：已登记的机器转验证路径 |
| 移除服务器 | `scripts/machine_remove.py --machine <别名或ip>` | 只删本地登记，见删除边界 |
| 检查某台机器 | `scripts/machine_verify.py --machine <别名或ip>` | 只读，不修复 |
| 集群查询（有哪些机器/空闲卡/健康） | `scripts/fleet_cli.py servers\|status\|capacity` | 读缓存或 `--live` 即时探测 |

状态详细定义、认证边界、幂等规则见 [references/behavior.md](references/behavior.md)；具体命令示例见 [references/command-recipes.md](references/command-recipes.md)。

## 认证边界（核心安全规则）

- **密码只允许在 add 的 bootstrap 阶段使用一次**：脚本用 paramiko 密码登录一次，把本地公钥装到宿主机 `authorized_keys`，之后所有操作走密钥 SSH。
- 密钥登录已可用时，**即使提供了密码也不使用**。
- 密码不写入任何文件、不复述、不进入最终 JSON。优先 `--password-stdin` / `--password-env`，仅当密码已在对话中暴露时才用 `--password`。
- 永远不使用 `sshpass`、`expect`，SSH 永远带 `BatchMode=yes`（绝不弹交互提示）。
- 本地无密钥对时自动生成 ed25519。

## fleet 服务

集群查询有两种形态，都由本 skill 提供：

1. **常驻服务**（推荐）：`scripts/fleet_manage.py start` 拉起 FastAPI 服务（仅监听 `127.0.0.1:8790`），后台线程并行探测全部机器，浏览器打开 http://127.0.0.1:8790 看面板。`fleet_manage.py` 另支持 `stop` / `status` / `restart`。
2. **按需探测**：`fleet_cli.py` 在服务未运行时自动退化为本地并行探测一次；`--live` 强制即时探测。

面板与采集能力（对齐 vaws-top）：

- **分层采集**：NPU 高频采样；CPU/内存/磁盘挂载/Docker 容器低频（约 5 分钟），跨轮继承不丢数据；
- **SQLite 历史**：采样落 `~/.server-management/fleet-history.db`，保留 7 天；面板提供趋势图（canvas 手绘渐变面积图）与 2 小时粒度热力图；
- **自适应频率**：浏览器活跃时 30 秒探测一轮，无人查看 2 分钟后自动降到 120 秒；
- **前端工程**：`webapp/` 是 Vite + React + TypeScript 工程（视觉对齐 vaws-top 浅色风格）；构建产物在 `web/`，skill 使用者无需 Node。

面板迭代（仅开发时需要 Node ≥ 20）：

```bash
cd server-management/webapp
npm install --registry=https://mirrors.huaweicloud.com/repository/npm/
npm run build     # 产物输出到 ../web/，然后重启 fleet 服务
```

注意：构建前先停掉 fleet 服务（`fleet_manage.py stop`），Windows 上运行中的服务会锁住 `web/` 目录导致清空失败。

`capacity` 的语义是**观测到的空闲**（AICore 利用率 ≤5% 且显存占用 ≤5% 且健康 OK），不是预留：查询结果不锁定任何资源，向用户报告时保持这个措辞。

## 依赖 bootstrap

首次使用时检查依赖，缺失则安装（华为内网用华为云 PyPI 镜像）：

```bash
python -m pip install --user -i https://repo.huaweicloud.com/repository/pypi/simple/ paramiko fastapi uvicorn
```

- `paramiko`：仅 add 的密码 bootstrap 路径需要；
- `fastapi` + `uvicorn`：仅 fleet 常驻服务需要；CLI 按需探测路径零第三方依赖。

## 删除边界

remove 只删除：inventory 记录、本地 `known_hosts` 条目。**不碰**宿主机 `authorized_keys`、防火墙、任何远端文件。宿主机不可达时移除仍成功，并在结果中注明宿主机侧公钥保留。

## 输出协议

所有脚本遵守同一契约（agent 解析时依赖它）：

- stderr：`__SM_PROGRESS__=<json>` 阶段进度事件（phase 字段区分 add/probe/inventory 等阶段）；
- stdout：单个最终 JSON，含 `ok`、`action`、`status`、以及机器/观测数据；
- `status` 只取这些值：`ready` / `needs_input` / `needs_repair` / `blocked` / `removed` / `unmanaged`（定义见 behavior.md）；
- 退出码：`ok=true` 为 0，否则为 1。

## 不做什么

- 不在服务器上部署应用、装业务依赖、同步代码、跑评测或 benchmark（那些是其他 skill 的事）；
- 不管理宿主机防火墙、不动 docker（除非用户明确要求且理解后果）；
- 不把 fleet 查询结果当成资源预留；
- 不输出、存储或转发密码。
