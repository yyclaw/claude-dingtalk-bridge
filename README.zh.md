# claude-dingtalk-bridge

[English](README.md) | [简体中文](README.zh.md)

通过钉钉在手机上远程驱动电脑上的 Claude Code —— 发起任务、接收进度、批准有风险的操作。它运行在钉钉 Stream 模式下，电脑只发起出站连接，无需内网穿透或公网 IP。

## 工作原理

入站流量走一条持久的 Stream 模式 WebSocket，出站流量走钉钉开放平台的 REST API。两条通路彼此独立。

```mermaid
sequenceDiagram
    participant P as 手机（钉钉）
    participant D as 守护进程
    participant C as Claude Code

    P->>D: 1. 发送指令文本（Stream WebSocket，入站）
    opt 配置了 geo 段
        Note over D: 2. 地区校验 —— 出口国家不匹配则跳过本轮
    end
    D->>C: 3. 启动一轮对话
    C->>D: 4. 请求一次工具调用（如 Bash）
    Note over D: 5. PermissionPolicy 评估
    alt 需要批准
        D->>P: 6. 推送授权请求（REST，出站）
        P->>D: 7. 回复 ok / no
    end
    D->>C: 8. 允许或拒绝该工具调用
    C->>D: 9. 流式返回回复分块，最后给出结果
    D->>P: 10. 每个分块到达即推送（REST，出站）
```

任务运行期间到达的新指令会被排队，待本轮结束后按顺序处理；控制命令（`/stop`、`ok`、`no` 等）立即生效，从不排队。

## 快速开始

首次配置，按顺序进行。第 1、3 步为手动操作，其余为命令。

```bash
# 1. 创建一个钉钉 Stream 模式机器人 —— 见下文“钉钉配置”。

# 2. 安装
git clone <repo> ~/Projects/claude-dingtalk-bridge
cd ~/Projects/claude-dingtalk-bridge
make setup            # 创建虚拟环境并安装依赖

# 3. 配置
make config           # 创建 ~/.config/claude-dingtalk-bridge/config.yaml
#    然后编辑该文件：client_id、client_secret、authorized_user_id、projects

# 4. 作为开机自启的后台守护进程运行
#    （需要 Xcode 命令行工具 —— 若未安装，先执行一次
#     `xcode-select --install`）
make daemon-install
make daemon-start
```

启动前请确认本机已登录 Claude Code（`claude` 命令可用）—— 守护进程会复用它的凭证。运行后，在钉钉里与机器人单聊即可；见“在手机上使用”。

## 钉钉配置

在钉钉开放平台上一次性完成：

1. 拥有或创建一个钉钉组织（钉钉 App → 通讯录 → 创建团队）。
2. 打开 https://open-dev.dingtalk.com ，用组织管理员账号登录，创建应用 → **企业内部应用**。
3. 添加**机器人**能力；将**消息接收模式**设为 **Stream 模式**（无需 webhook 地址），然后发布 / 启用应用。
4. 在凭证页面记下 **Client ID（AppKey）**和 **Client Secret（AppSecret）**。
5. 获取你自己的 userid（staffId）：钉钉管理后台 → 通讯录 → 你的资料 → 查看 userid。或者先启动守护进程并给机器人发消息 —— 日志会打印任何未授权发送者的 id。

把 `client_id`、`client_secret` 和 `authorized_user_id`（上面那个 userid）填入 `~/.config/claude-dingtalk-bridge/config.yaml`，并列出允许守护进程操作的项目目录。

## 在手机上使用

在钉钉里与机器人单聊。纯文本是发给 Claude 的指令；以 `/` 开头的文本是控制命令（不区分大小写）。授权回复（`ok`/`no`）是口语化的，不带 `/`。

语音消息由钉钉转写后作为普通提示词运行；图片 —— 单独发送或与文本一同发送 —— 会被下载并传给 Claude 阅读。两者都跳过命令解析。当 Claude 提问时，选项会带编号发到手机上；回复编号或自行输入答案均可。

下表中**命令本身只有英文**，钉钉上请照此输入。

| 命令 | 作用 |
|---|---|
| `/help` | 列出所有命令 |
| `/stop` | 中断当前任务 |
| `/clear` | 中断任务并重置会话 |
| `/status` | 显示运行状态（项目、模型、token、缓存） |
| `/pwd` | 显示当前项目 |
| `/ls` | 列出项目 |
| `/cd <name>` | 切换当前项目 |
| `/session` | 显示当前会话 id |
| `/resume` | 列出近期会话 |
| `/resume <n>` / `/resume <id>` | 切换到某个历史会话 |
| `/model` | 列出模型 |
| `/model <n\|name>` | 切换模型 |
| `/verbose on\|off` | 详细模式开关（流式输出每一步） |
| `/debug on\|off` | 调试模式开关：跳过 Claude，仅调试守护进程 |
| `/compact` | 压缩对话历史（转发给 Claude） |
| `/context` | 显示上下文窗口用量（转发给 Claude） |
| `/usage` | 显示用量与花费（转发给 Claude） |
| `ok` / `yes` / `approve` | 批准一次授权请求 |
| `no` / `deny` / `reject` | 拒绝一次授权请求 |

## 安全设计

### 接入鉴权
- Stream 模式出站长连接，本机不监听任何端口、不需要公网 IP — **不走内网穿透方案，公司红线坚决不踩**
- 单用户白名单：只有 `authorized_user_id` 指定的账号能驱动 Claude — **其他钉钉账号无法调度你电脑里的 Daemon**
- 图片消息在下载前就先校验发件人，未授权用户无法用一条图片触发任何网络读写 — 防止被当成下载 DoS 入口

### Claude 工具调用权限
- **任何对项目目录外的写操作都需要你在手机上点头** — Edit / Write 系列工具的目标路径经 `..` 与符号链接解析后必须落在当前项目内，否则升级到手机。**就算 Claude Code 端开了 auto mode 也绕不过这一层** — 权限回调挂在 SDK 之上，独立于 Claude 自己的内部决策
- **Bash 等命令走白名单 + 精确前缀匹配**（`git` 不会顺手放过 `gitleaks`），含 shell 元字符（`&&` `|` `;` `>` `<` `` ` `` `$(` 通配符等）的命令一律识别并拦截 — 杜绝靠命令拼接绕过 allowlist

### 本地文件权限
- `config.yaml` 启动期校验权限位，宽于 `0600` 直接拒绝加载并提示 `chmod` 命令；`make config` 创建配置时已自动锁好 — 防止同机其他本地用户读到你的 DingTalk `client_secret`
- 图片缓存目录创建即 `0700`，主动拒绝符号链接父目录 — 防 pre-creation symlink 攻击（攻击者抢在我们 `mkdir` 之前把目标路径替成符号链接，把我们的写操作引到他指定的地方）

### Claude 账号守护
每次调起 Claude 跑任务之前先校验出口 IP 的国家码 — 跟 config 中设定的预期不符就直接终止本次任务，**降低 Claude Code 账号被风控 / 封禁的风险**。

### 随时能踩刹车
手机发 `/stop` 立刻中断当前 turn、`/clear` 重置整个会话 — 任务跑飞了你随时能拉手刹。

## 效率优化

Prompt cache 针对手机驱动的节奏做了两处调优。`claude_code` preset 默认会把 git status 等动态段塞进 system prompt，每个 turn 前缀都在变，缓存前缀根本复用不上——Daemon 把这些动态段剔除，让前缀字节级稳定。再加上手机两次 turn 之间往往隔好几分钟，默认 5 分钟缓存窗口大概率已经冷掉，于是切到 1 小时 TTL。`/status` 会展示累计 token 与上一次 turn 的 cache 读 / 写明细，命中率随时可查。

## 更多命令

所有操作都封装在 `Makefile` 里；不带参数运行 `make` 即可列出全部。

| 命令 | 作用 |
|---|---|
| `make setup` | 创建虚拟环境并安装依赖 |
| `make config` | 从模板创建配置文件（若不存在） |
| `make start` | 在前台运行守护进程（日志输出到终端） |
| `make test` | 运行单元测试 |
| `make daemon-install` | 安装为开机自启的后台守护进程 |
| `make daemon-start` | 启动守护进程 |
| `make daemon-stop` | 停止守护进程（KeepAlive 不会重新拉起） |
| `make daemon-restart` | 重启守护进程 |
| `make daemon-status` | 显示守护进程状态 |
| `make daemon-uninstall` | 卸载守护进程 |
| `make logs-tail` | 在终端中跟踪守护进程日志 |
| `make logs-web` | 在浏览器中打开守护进程日志实时查看器（`ARGS=...`） |

守护进程日志：`~/Library/Logs/claude-dingtalk-bridge/daemon.{out,err}.log`

图片缓存：`~/Library/Caches/claude-dingtalk-bridge/`。入站图片下载到此处供 Claude 阅读；下次下载时会清理超过 72 小时的旧文件（最多每小时一次）。除此之外没有其他清理机制，所以长时间没有新图片时缓存会一直留着，可以手动 `rm`。
