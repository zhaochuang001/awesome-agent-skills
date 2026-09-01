# server-management 行为契约

本文件是状态词汇与行为边界的详细定义。SKILL.md 是路由入口，本文件是判定依据。

## 状态词汇

所有脚本的最终 JSON 里 `status` 只取以下值：

| 状态 | 含义 | 典型场景 |
| --- | --- | --- |
| `ready` | 请求的动作成功完成 | add 完成（密钥认证就绪、已入册）；verify 通过；fleet 查询正常返回 |
| `needs_input` | 缺少用户输入才能继续 | add 时密钥不可用且未提供密码 |
| `needs_repair` | 机器受管但检查失败 | verify 时 SSH 断开；NPU 出现 Alarm；bootstrap 后密钥仍不可用 |
| `blocked` | 前置条件不满足 | paramiko 未安装；fleet 服务起不来（缺依赖/端口占用） |
| `removed` | 移除动作完成（幂等） | remove 成功；服务已停止 |
| `unmanaged` | 目标不在管理范围内 | verify/remove 的机器不在 inventory |

幂等原则：同一请求重复执行返回相同终态，不产生重复副作用（重复 add 不重复建记录，重复 remove 不报错）。

## 认证契约

- 密码的唯一合法用途：add 阶段对新机器做一次性 bootstrap（paramiko 密码登录 -> 幂等追加公钥到 `authorized_keys` -> 立即丢弃密码）。
- bootstrap 之前先检查密钥登录：已可用则完全跳过密码（"If key auth already works, do not use the password"）。
- 密码传递优先级：`--password-stdin` > `--password-env` > `--password`（最后一个仅当密码已出现在对话中）。
- 所有常规 SSH 带 `BatchMode=yes`：连接失败就失败，永远不进入交互式提示。
- `known_hosts` 用 `accept-new` 策略：首次连接自动接受，之后变化会报错（防中间人）。

## inventory 契约

- 位置 `~/.server-management/inventory.json`，字段：`alias`、`host`、`port`、`user`、`added_at`、`auth`、`npu_count`、`npu_name`、`machine_type`、`last_verified_at`、远端主机元数据。
- 机器唯一键是 `host:port`；alias 只是展示名，`find_machine` 接受 alias / host / `host:port` 三种标识。
- 写入原子化：临时文件 + `os.replace`。
- `machine_type` 从 NPU 名称推断（910B* -> 910B、910C* -> 910C、310P* -> 310P），仅元数据用途；无法推断时为 `unknown`，不要编造。

## fleet 观测契约

- 探测只读：SSH echo + `npu-smi info` + `/proc/loadavg` + `/proc/meminfo` + `/proc/stat` + `df` + `docker ps`，绝不在远端执行写操作。
- 探测并行（线程池 8 并发），单机失败/超时收敛为该机器的 `error` 字段，不影响其他机器。
- 分层采集：NPU 每轮采（30s/120s 自适应）；CPU/内存/磁盘/Docker 约 5 分钟采一次，未采集的轮次从上一轮继承（`extras_probed_at` 标记采集时间），机器转为不可达时不继承。
- 自适应频率：API 轮询即心跳，2 分钟无客户端时探测间隔从 30s 降到 120s。
- 历史采样写 SQLite（`~/.server-management/fleet-history.db`），保留 7 天；趋势查询走 `/api/history`，聚合走 `/api/history/aggregate`。
- 空闲卡判定：`health == OK` 且 `aicore_util <= 5` 且 `mem_used/mem_total <= 0.05`（双 chip 卡取各 chip 的 AICore/HBM 最大值）。
- `capacity` 是观测不是预留。向用户转述时必须保持这个语义，不要说"已占用/已锁定"。
- 服务仅监听 `127.0.0.1`；任何情况下不改为 `0.0.0.0`。
- 服务不可达时 CLI 的降级顺序：HTTP API -> 文件缓存 -> 本地并行探测。`--live` 时跳过缓存。
- 静态前端从 skill 的 `web/` 目录提供（vite 构建产物在 `web/assets/`）；静态路由必须做路径穿越防护（resolve 后必须仍在 `web/` 内）。

## 面板管理 API 契约（服务器管理页）

- `POST /api/servers/batch`：批量添加。先试已有密钥登录，失败后按顺序尝试请求内的候选密码做一次性 bootstrap（与 machine_add.py 同一套实现）；密码只在本次请求内使用，不落盘、不写入任何响应字段。
- `PUT /api/servers/{host}`：更新 tags / enabled。enabled=false 的机器不再被探测循环采集；tags 上限 20 个、单个 32 字符。
- `DELETE /api/servers/{host}`：与 machine_remove.py 完全同边界——只删 inventory 记录与本地 known_hosts 条目，不碰宿主机 authorized_keys。
- `POST /api/collect?host=`：单机即时采集（面板"立即采集"按钮），复用探测与历史入库逻辑。

## 删除边界

remove 删除且仅删除：

1. inventory 记录；
2. 本地 `known_hosts` 中该 `host:port` 条目（`ssh-keygen -R`）。

明确不删：宿主机 `authorized_keys`、防火墙规则、远端任何文件、其他机器的记录。宿主机侧公钥保留的事实必须写进移除结果的说明。

## 触发示例

应触发本 skill：

- "帮我配置一下 125.173.1.2 这台服务器，密码是 xxxx"
- "加一台机器 10.0.0.5，用户名 ubuntu"
- "把 125.173.1.6 移除"
- "检查 10.0.0.5 是不是 ready"
- "现在哪些机器有空闲的 NPU？"
- "集群整体状态怎么样？"
- "查一下 fleet 服务还活着吗"

不应触发（除非服务器可用性本身是障碍）：

- "在这台服务器上部署 xxx 服务"
- "把代码同步到远程机器"
- "跑一下 benchmark"
- "SSH 到机器上看个日志"（一次性操作，不需要管理状态时）

## agent 使用规则

- 用户给密码时：把密码通过 `--password-stdin`（或环境变量）传给脚本，不在回复中复述密码原文。
- 收到 `needs_input` 时：向用户说明缺什么（通常是密码或确认），拿到后重试，不要绕过脚本手动操作。
- 收到 `needs_repair` 时：先跑 `machine_verify.py` 拿到具体错误，向用户报告；修复动作（网络/密钥问题）可能需要用户参与。
- fleet 查询正常路径不需要 `--live`；用户要"现在的实时状态"或缓存明显过期时才加。
- 不要在脚本成功后再自己 SSH 上去补查信息，脚本返回的观测已经足够。
