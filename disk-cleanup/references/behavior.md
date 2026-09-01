# disk-cleanup 行为契约

本文件是清理分级与安全边界的判定依据。机器清单语义以 server-management skill 的契约为准。

## 状态词汇

| 状态 | 含义 |
| --- | --- |
| `ready` | 分析完成（只读），输出候选清单 |
| `removed` | 清理执行完成，输出删除清单与 df 前后对比 |
| `needs_input` | 参数问题（如 --days < 1） |
| `blocked` | 机器不在清单中 |

## 安全模型（严格执行）

### 只读分析（默认）

- df 全景（排除虚拟文件系统）+ docker images/ps -a 全量 + 引用关系分析
- 候选分级：
  - `safe`：悬空镜像（无 tag 且无容器引用）
  - `needs_confirm`：未被任何容器引用的镜像、停止的容器
- `--path` 额外扫描某目录一级占用（du -x 不跨挂载点，大目录耗时在进度里说明）
- 预估回收量标注"逻辑值上界"说明

### execute-safe

- `docker image prune -f`（悬空层）+ `docker builder prune -f`（构建缓存）
- 这两项无损：悬空层定义上无 tag 无引用；构建缓存可重建
- 不需要用户确认（但 agent 汇报时仍应说明做了什么）

### execute-confirm

- 删除**退出超过 --days 天**的停止容器（`docker rm`，可写层数据一并丢失）
- `--include-images`：额外删除未被**任何**容器（含停止的）引用的镜像，删后再 prune 一轮悬空
- 前置：agent 必须先跑分析并把 needs_confirm 清单呈现给用户、得到明确确认
- 运行中的容器在任何模式下都不删；被引用的镜像在任何模式下都不删

### 永不触碰

- 运行中容器及其镜像
- `/home`、`/mnt`、`/data`、`/root` 下的用户数据（只出现在 disk_hogs 报告里）
- 不执行 `docker system prune -a`（共享节点上等于删掉所有备用资源）

## 引用判定的正确姿势

被引用镜像 ID 集合必须来自：

```
docker ps -a -q | xargs -r docker inspect --format '{{.Image}}' | sort -u
```

不能用 `docker ps --format '{{.Image}}'`：它对无 tag 镜像输出 ID 简写、对有 tag 的输出名字，
两种形态都无法与 `docker images` 的完整 ID 直接比对，会漏检（实战中曾因此把在用镜像
列进可删清单）。

## 容器年龄判定

- 解析状态字符串中的退出时间（days/weeks/months；hours/minutes/seconds 归 0）
- **向下取整**：8 天 23 小时算 8 天（删除场景宁可晚删）
- 运行中状态（Up/Restarting/Paused）直接排除
- `--days` 必须 ≥ 1

## agent 使用规则

- 用户说"空间满了/清理一下"时：先跑**只读分析**，把分级候选摘要给用户（safe 直接可做，confirm 列清单问）
- 共享节点上的 needs_confirm 项（别人的镜像/容器）：逐项列出名字、大小、状态、最后使用时间，由用户拍板，agent 不替用户决定
- 汇报清理结果时用 df 前后对比（真实回收量），不用清单求和（逻辑值会虚高）
- 清理后如仍不足，报告 disk_hogs（大目录）让数据主人处理，不越界删数据

## 触发示例

应触发：

- "80.5.9.126 磁盘满了，帮我清理下"
- "no space left on device 怎么办"
- "清理一下这台机器的 docker 镜像"
- "服务器空间不足，看看什么占的"

不应触发：

- "删掉 /mnt/share 下的 xxx 文件"（用户数据文件操作，非本 skill 职责）
- "把日志文件清一下"（日志治理可以分析（--path /var/log），但删除动作不在本 skill 边界内）
