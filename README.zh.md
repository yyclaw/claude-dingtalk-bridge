# claude-dingtalk-bridge

[English](README.md) | [简体中文](README.zh.md)

通过钉钉在手机上远程驱动电脑上的 Claude Code — 发起任务、接收进度、批准有风险的操作，让手机成为电脑的“安全远程显示屏”。

## 功能特性

1. Stream 模式出站长连接，电脑端不监听任何端口、不需要公网 IP — **不走内网穿透方案，保障内网数据安全**
2. 电脑端 daemon 跟你的钉钉账号绑定，其他账号无法驱动 daemon 程序，安全有保障
3. 每次调度 Claude Code 跑任务之前先校验出口 IP 的国家码 — 跟预期不符就直接终止本次任务，**降低 Claude 账号被风控 / 封禁的风险**
4. 默认一小时的 Cache TTL，提升手机这种低频交互场景的缓存命中率，节省 Token 消耗；完成一轮任务后，手机上通过 `/status` 命令可以随时查看 Session Cost 和上一轮任务的缓存命中情况，Token 用量了然于心
5. 集成官方 Claude Agent SDK，电脑端无需启动终端，只需保持电脑持续网络在线；可使用 `/resume` 命令实现手机、电脑无缝交接

## 快速开始

### 电脑端

```bash
# 1. 创建一个钉钉 Stream 模式机器人 — https://open-dev.dingtalk.com

# 2. 安装依赖，初始化程序运行虚拟环境
make setup

# 3. 创建配置文件（~/.config/claude-dingtalk-bridge/config.yaml）
#   （同时自动设置文件权限 `chmod 600`）
make config
#    然后手动编辑该文件：client_id、client_secret、authorized_user_id、projects

# 4. 作为开机自启的后台守护进程运行
#   （需要 Xcode 命令行工具 —— 若未安装，先执行一次 `xcode-select --install`）
make daemon-install
make daemon-start

# 5. 查看运行日志（Optional）
make logs-web
```

### 手机端

与你创建的机器人单聊，发送 `/help` 查看所有支持的指令。

## 安全设计

详见 `config.example.yaml` 内注释。