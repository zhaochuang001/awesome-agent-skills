---
name: disk-cleanup
description: 分析并清理服务器磁盘空间：磁盘全景、docker 镜像/容器占用、分级清理候选（悬空镜像/未引用镜像/陈旧容器）。适用于"磁盘满了/空间不足/no space left/清理 docker 镜像/清理停止的容器"等请求；不用于删除用户数据文件（/home、/mnt 等只报告不动）。
---

# 服务器磁盘清理

磁盘空间不足时的分析与清理（以 docker 资源为主，共享 NPU 节点安全优先）。机器清单与 SSH 来自 **server-management skill**（前置依赖）。

## 使用方式

```bash
# 1. 分析（默认，只读不动任何东西）：磁盘全景 + docker 占用 + 分级清理候选
python scripts/cleanup.py --host <服务器> [--path /var]

# 2. 无损清理：悬空镜像 + 构建缓存（不需要确认）
python scripts/cleanup.py --host <服务器> --execute-safe

# 3. 确认级清理：删除退出超过 N 天的容器（先跑过分析、用户确认清单后执行）
python scripts/cleanup.py --host <服务器> --execute-confirm --days 9

# 4. 连同未被任何容器引用的镜像一起删（共享节点慎用，逐个过目清单）
python scripts/cleanup.py --host <服务器> --execute-confirm --days 9 --include-images
```

## 三级安全模型

| 级别 | 内容 | 前提 |
| --- | --- | --- |
| **只读分析**（默认） | df 全景、du 大目录、docker 镜像/容器清单、分级候选与预估回收量 | 无 |
| **safe**（`--execute-safe`） | 悬空镜像（无 tag 无引用）、docker 构建缓存 | 无损，可直接执行 |
| **confirm**（`--execute-confirm`） | 退出 ≥ `--days` 天的容器；`--include-images` 时加未引用镜像 | 必须先跑分析、把清单给用户确认 |

**绝不触碰**：运行中的容器、被引用的镜像、`/home` `/mnt` `/data` `/root` 下的用户数据（这些只出现在报告里，由数据主人自己处理）。

## 实战校准（写进本 skill 的坑）

- **引用检查必须用 `docker inspect`**：`docker ps --format {{.Image}}` 对无 tag 镜像输出 ID 简写、对有 tag 的输出名字，直接比对会漏检，把在用镜像列进可删清单（实战踩过）
- **镜像 SIZE 是逻辑值**：层共享导致实际回收量远小于清单求和（commit 产物删掉 tag 可能只回收几百 MB）；报告始终注明，最终以 df 前后对比为准
- **容器年龄向下取整**：8 天 23 小时算 8 天——宁可晚删不可早删
- `--days` 强制 ≥ 1，防止误删刚退出的容器

详细行为契约见 [references/behavior.md](references/behavior.md)。

## 输出协议

与 server-management 一致：stderr `__SM_PROGRESS__=<json>` 进度，stdout 单个最终 JSON。状态：`ready`（分析完成）/ `removed`（清理完成）/ `needs_input`（参数问题）/ `blocked`（机器不在清单）。
