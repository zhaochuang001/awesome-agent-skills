---
name: npu-migrate
description: 把源服务器上的容器和代码文件夹迁移到有空闲 NPU 卡的服务器并自动拉起服务；支持按 fleet 空闲状态和所需卡数自动选择目标服务器。适用于"容器要搬到别的机器/卡被占了要迁移/帮我迁移服务到空闲机器"等请求；不用于普通的代码同步或单个文件拷贝。
---

# NPU 服务迁移

把"源服务器的容器 + 代码文件夹 + 服务"整体搬到有空闲卡的目标服务器。机器清单与空闲状态来自 **server-management skill**（前置依赖，需先安装）。

## 前置条件

- 已安装 server-management skill（机器在清单中、密钥可登录）
- 源服务器与目标服务器之间可直连 SSH（镜像与代码走服务器间直传，不经过本地）

## 使用方式

用户给出四要素后执行（Windows 用 `py -3`，POSIX 用 `python3`）：

```bash
python scripts/migrate.py \
  --source <源服务器> \
  --container <源容器名> \
  --code-path <源机上的代码文件夹绝对路径> \
  --script <容器内的服务启动脚本路径> \
  [--target <目标服务器>]      # 不指定则自动选空闲卡最多的机器
  [--npus N]                   # 卡数提取不到时手动指定
  [--weights-path <路径>]      # 服务依赖的模型权重路径（源机绝对路径，可传多次）
  [--plan]                     # 干跑：只输出迁移计划与命令，不执行
  [--stop-source]              # 成功后停源容器（默认保留以保回滚）
```

**首次对某个容器迁移建议先跑 `--plan`**：它会真实探测空闲状态、输出目标机选择、卡映射表（容器内编号 → 目标机物理卡）和将执行的全部命令，确认无误后去掉 `--plan` 正式执行。

**Windows Git Bash 注意**：传给脚本的 Linux 绝对路径（如 `/mnt/...`）会被 Git Bash 自动转换成本地 Windows 路径导致失败。调用时加前缀：`MSYS_NO_PATHCONV=1 python scripts/migrate.py ...`（CMD/PowerShell 无此问题）。

## 流程

1. **解析源机与容器**：inventory 查机器 → `docker inspect` 提取镜像、卡设备、挂载、端口、环境变量
2. **提取卡数**：优先 `ASCEND_RT_VISIBLE_DEVICES` 环境变量，其次数 `/dev/davinci*` 设备；`--npus` 可覆盖
3. **选择目标机**：空闲数据来自 fleet 服务（未运行则本地并行探测）；过滤条件 = 可达 + 非源机 + 空闲卡 ≥ 需求；取空闲最多的机器。用户指定目标时校验空闲充足性
4. **迁移执行**：
   - `docker commit` 源容器为镜像 `migrate/<容器名>:<时间戳>`
   - 镜像直传：源机 `docker save | gzip | ssh 目标 'docker load'`
   - 代码 rsync 到目标机**相同绝对路径**（路径不变 → 容器挂载参数原样复刻）
   - 目标机 `docker run -d` 复刻源容器参数，卡设备映射到新分配的物理卡（容器内可见编号重排为 0..N-1）
   - `docker exec` 容器内后台执行启动脚本（日志 → `/tmp/migrate-startup.log`）
5. **验证**：容器 running + 启动日志尾部随报告返回

## 模型权重检查

给了 `--weights-path` 时，在起容器**之前**检查目标机上的权重：存在且与源机大小一致（容差 1%）才继续，否则**停止迁移并报告明细**（每个路径的源/目标大小、缺失量），由用户决定怎么处理——权重动辄几百 GB，脚本不自动同步。`--plan` 干跑也会真实检查权重（只读）并列出状态。

## 安全边界

- **源容器默认绝不停止/删除**：迁移失败或需回滚时，回源机重启即可。只有用户明确给了 `--stop-source` 才在验证通过后停止源容器
- 只写目标机的 docker 镜像/容器和用户指定的代码路径；不碰目标机其他文件
- 挂载中代码路径之外的大目录（如权重目录）**不自动同步**，报告中列出提醒
- 端口冲突时 docker run 直接失败并清理半成品容器，不擅自改端口
- 迁移是幂等的：失败修复后可重跑（commit/load/rsync 都可重复执行）

## 输出协议

与 server-management 一致：stderr `__SM_PROGRESS__=<json>` 进度，stdout 单个最终 JSON。状态：`migrated`（成功）/ `needs_input`（缺参数、目标卡不足）/ `blocked`（源不可达、容器不存在）/ `failed`（传输或启动失败）。JSON 里含完整迁移计划（卡映射、将执行的命令、备选机器）与验证结果。

详细行为契约见 [references/behavior.md](references/behavior.md)。
