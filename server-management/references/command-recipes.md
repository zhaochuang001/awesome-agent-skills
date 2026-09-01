# 命令示例

所有脚本路径相对 skill 根目录（`server-management/`）。Windows 用 `py -3`，macOS/Linux 用 `python3`，下文以 `python` 代表。

脚本输出都是"stderr 进度 + stdout 单 JSON"，人类阅读时 JSON 里的 `status` / `error` / `note` 字段最有用。

## 添加服务器

用户给了 IP 和密码（密码已出现在对话中）：

```bash
python scripts/machine_add.py --host 125.173.1.2 --password '用户提供的密码'
```

密码未暴露在对话中时，优先环境变量（不回显）：

```bash
export SM_PASSWORD='...'        # PowerShell: $env:SM_PASSWORD = '...'
python scripts/machine_add.py --host 125.173.1.2 --password-env SM_PASSWORD
unset SM_PASSWORD               # PowerShell: Remove-Item Env:SM_PASSWORD
```

非默认端口/用户/别名：

```bash
python scripts/machine_add.py --host 10.0.0.5 --port 2222 --user ubuntu --alias npu-node-5
```

机器已配置过密钥（无需密码）：

```bash
python scripts/machine_add.py --host 10.0.0.7
```

成功输出（stdout JSON 关键字段）：

```json
{"ok": true, "action": "add", "status": "ready", "machine": {"alias": "10.0.0.7", "host": "10.0.0.7", ...}, "note": "密钥认证已就绪；密码不会再次使用，也未持久化"}
```

## 移除服务器

```bash
python scripts/machine_remove.py --machine 125.173.1.6
python scripts/machine_remove.py --machine npu-node-5
```

## 验证单台机器

```bash
python scripts/machine_verify.py --machine 10.0.0.5
```

## 集群查询（fleet CLI）

```bash
python scripts/fleet_cli.py servers                 # 列出全部机器与 NPU 状态（读缓存）
python scripts/fleet_cli.py servers --live          # 即时探测
python scripts/fleet_cli.py status 10.0.0.5         # 单机详情
python scripts/fleet_cli.py capacity --min-idle 4   # 至少 4 张空闲卡的机器
python scripts/fleet_cli.py capacity --min-idle 8 --max-age 600  # 空闲至少 10 分钟（需服务持续运行）
```

## fleet 常驻服务

```bash
python scripts/fleet_manage.py start     # 拉起服务（幂等），随后浏览器访问 http://127.0.0.1:8790
python scripts/fleet_manage.py status    # 健康检查 + 进程状态
python scripts/fleet_manage.py restart
python scripts/fleet_manage.py stop
```

日志：`~/.server-management/fleet-service.log`。服务未运行时 fleet CLI 自动降级为本地探测，查询依然可用。

## 依赖安装（首次使用）

```bash
python -m pip install --user -i https://repo.huaweicloud.com/repository/pypi/simple/ paramiko fastapi uvicorn
```

## 状态文件位置

| 文件 | 用途 |
| --- | --- |
| `~/.server-management/inventory.json` | 机器清单（唯一状态源） |
| `~/.server-management/fleet-cache.json` | 最近一次集群探测快照 |
| `~/.server-management/fleet-service.pid` | 常驻服务 PID |
| `~/.server-management/fleet-service.log` | 常驻服务日志 |
