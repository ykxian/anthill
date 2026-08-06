# 快速开始

装依赖，起面板，完事。详细设计见 [README.md](./README.md)。

## 1. 装依赖

需要 [uv](https://docs.astral.sh/uv/) 和 Python 3.11+。

```bash
uv sync                      # 够跑起来了
uv sync --extra llm          # 想让 Agent 真接模型，再加这个（装 anthropic / openai SDK）
```

## 2. 单机：起一条命令

```bash
mkdir demo && cd demo
uv run anthill serve -w . --panel-write
```

没有工作区它会就地建一个，然后打开 **<http://127.0.0.1:45778/panel>**。

面板上就能：**配模型（provider + 密钥）**、加 Agent（记得选角色 —— 想跑多 Agent
编排就得有一个 `coordinator`）、把它启动起来、给它派活、跟它对话、改 `node.toml`。
**不用开第二个终端。**

> 密钥存在 `~/.anthill/secrets.env`（0600），**不进 `node.toml`** ——
> 那个文件会进 git、会被 `fetch` 拉走、会显示在配置页上。
> 不想落盘就照旧在终端 `export`，面板会显示「来自环境变量」。

> `-w .` 的意思是「就用当前目录，没有就建」。不给 `-w` 的话它不会擅自建，
> 而是让你在面板上挑一个目录 —— 免得 `.anthill` 落在你没想要的地方。

## 3. 两台机器互联

两台都起（跨机要对外监听）：

```bash
uv run anthill serve -w . --host 0.0.0.0 --panel-write
```

同网段的节点会自己出现在拓扑里，标成「见过，还没配对」。换密钥就是念一串数字：

```bash
# A 机（或在它的面板上点节点旁边的「配对」）
uv run anthill peers pair
#   → 显示六位 PIN，两分钟内有效，只能用一次

# B 机
uv run anthill peers pair --to <A的节点名> --pin 479486
```

两边屏幕上的指纹核对一致就成了。之后在任意一台的面板上都能看到全部节点。

## 4. 没有显示器的机器

机柜里的服务器没法开浏览器，给它一个面板令牌：

```bash
uv run anthill serve -w . --host 0.0.0.0 --panel-write --panel-token
```

启动时会打印一个带令牌的链接，在你自己的电脑上打开它就行。

> 令牌等价于「能在那台机器上执行命令」。局域网不可信的话，
> 改用 `ssh -L 45778:127.0.0.1:45778 你@那台机器` 然后浏览 `localhost:45778`。

## 5. 把你自己（或已有的 Claude Code）接进来

在面板的「加一个 Agent」里选 **bridge**，启动它，页面上就会多出一个 **桥接** 标签页。

**那一页顶上有「把一个终端会话接进来（三选一）」**：三条路的命令与配置都写在那儿，
路径由服务端填好（`anthill` 在不在 PATH 上、工作区在哪，页面猜不出来），点一下就复制。
下面那些是本节的展开说明。

桥接 Agent 背后是「一个人」，所以它不自己想 —— 别人发给它的消息会在那一页列出来等着回。
两种回法，**随时可以混着用**：

- **就在页面上回。** 消息下面就是输入框，写完点「回复」。也能主动发一条给别人。
  不用开终端，也不用有 Claude Code。
- **交给一个常开的 Claude Code 会话。** 那一页顶上有一句现成的话（带「复制」按钮），
  粘给你的会话就行 —— 路径已经填好了，不用自己拼。

两条路写的是同一批文件（`bridge/inbox/` 收，`bridge/outbox/` 回），
所以会话在盯着的同时，你照样可以在网页上先替它回一句。

## 6. 把 Claude Code 接进来

```bash
uv sync --extra mcp
```

**只配这一次**（全局，命令里**不写 Agent 名**）：

```bash
claude mcp add --scope user anthill -- /路径/.venv/bin/anthill mcp serve -w /你的工作区
```

之后每开一个 Claude Code，它都会**自动认领一个还没人占的桥接 Agent**。
在面板上建三个 bridge Agent，开三个会话，就是一一对应 —— 包括**同一个目录下
开三个**，因为认领跟着进程走，不跟着目录走。

想钉死某一个（**同一个目录下开两个会话**时必须靠它——目录一样，自动认领分不出谁是谁）：

```bash
ANTHILL_AGENT=cc2 claude
```

钉到一个**还活着**的会话头上会被拦下，不会悄悄顶掉它（两个会话同时是同一个 Agent
会互相抢消息）。真要接管：`ANTHILL_TAKEOVER=1 ANTHILL_AGENT=cc2 claude`。

> 为什么命令里不写 Agent 名：Claude Code 的配置粒度是**目录**。写死的话，
> 那个目录下开几个会话就有几个抢同一个 Agent —— 配置文件表达不出「谁对应谁」。
> 能穿透到单个会话的只有环境变量（子进程继承），所以默认走自动认领，
> 要指定就用 `ANTHILL_AGENT`。

**认领会认回上一次那个。** 认领跟着进程走（会话关了自动空出来），但**不会随机重排**：
判据是**工作目录**——`~/projA` 里重开的会话还是拿到上次那个 Agent。

这一条不是锦上添花：上下文是挂在 Agent 上的（它的邮箱、thread、别人对
「cc1 说过什么」的记忆）。纯粹「挑一个空的」的话，A 是 cc1、B 是 cc2 的两个会话
重启一轮顺序反了就变成 A→cc2、B→cc1 —— **历史就串了**。

挑选顺序：**上次就是我 > 谁都没用过的 > 别人用过的**（最后一档才会串）。

**每次绑定都会报自己是谁**：MCP 那条写进 server 的 instructions（会话一开口就知道），
同时往 stderr 打一行给你看；`anthill bridge` 自动挑的时候也会打印「这个会话 = cc1」。
面板桥接页上有一张表：谁占着哪个、空闲的上次是哪个目录用的。

### 让它「一直盯着」

MCP 工具和 hook 都是**拉取式**的 —— 会话闲着的时候没有任何东西会叫醒它。
所以有一个会阻塞的调用：

- 会话里让它调 **`anthill_wait`**（阻塞到有人找为止，超时再调一次）；
- 不想装 MCP 的话，粘桥接页上那句话就行 —— 那是条循环跑
  `anthill bridge --wait 300` 的指令，一个道理。

`anthill_inbox` / `anthill_reply` / `anthill_send` 是收发。

## 7. 存一件常做的事 / 定时跑 / 跑完通知

```toml
[templates.review]
goal = "审一遍 {arg} 的改动，重点看边界和错误处理"

[schedules.nightly]
every = 86400          # 秒。没做 cron 表达式 —— 「每隔多久」够用，写错的余地也小
template = "review"

[notify]
webhook = "https://example.com/hook"   # 跑完 POST 一个 JSON 过去
on_failure_only = false
```

```bash
uv run anthill run --template review "src/scheduler.py"
```

> 通知默认**全关** —— 一个会自己往外发 HTTP 的框架，得是你明确要的，
> 而且那条请求里带着任务目标与摘要（是内容，不是元数据）。

## 一台机器上多个工作区，怎么切

一个 serve 进程照看**这台机器上的全部工作区**（路由键 `to.node` 本来就写在信封上，
不必一个工作区起一个进程）。它启动时会接管：`-w` 指的那个 + 机器级清单里记着的。

节点名默认**跟着目录走**：`collab/` 里就叫 `collab`，`collab-tst/` 里就叫 `collab-tst`。
本机撞名会自动加序号；目录名说明不了事（`workspace`、`tmp` 这类）时退回主机名。

> ⚠️ **跨机不保证唯一** —— 两台机器上都有个 `collab` 目录很正常。
> 真撞上时配对会直接拒绝并让你改名，不会留下一个「收件人指谁说不清」的状态。

**侧栏就是切换器**：本机每个工作区都是一组，名字全都摆着。点一下就切过去
并展开它的 Agent；正在操作的那个左边有一道竖线。顶栏和「工作区」页也会跟着变。

「工作区」页每行右边还有：

- **切到这个** —— 和点侧栏一样。
- **接管** —— serve 启动**之后**才加进清单的工作区，点它就开始照看，不用重启。

## 清理工作区清单

面板「工作区」页底下三个按钮：

| 按钮 | 干什么 | 动文件吗 |
|---|---|---|
| 清掉失效的 | 只清路径已经不存在的那些 | 不 |
| 清空清单 | 全清 | 不 |
| 连目录一起删 | 全清，并删掉它们的 `.anthill/` | **删** |

第三个会带走那些工作区的邮箱、黑板、密钥，**不可撤销**；
但它**只删 `.anthill/`**，你放在那个目录里的别的东西（源码、笔记）一律不动。

三个都**永远保留本进程正在照看的那些** —— 把自己删了，面板下一秒就找不着自己。

点之前会把要删的路径原样列出来让你看。名单在「你看」和「你点」之间变了
（比如另一个窗口刚加了一个工作区），这一次就整个作废，不会误伤。

## 卡住了先跑这个

```bash
uv run anthill doctor      # 配置、密钥、邮箱、在跑的进程，一次查完
uv run anthill guide       # 按场景分组的命令地图
```

`doctor` 会直接点出最常见的两种卡壳：coordinator 没有大脑、provider 没设密钥。

## 常用命令

发消息之前，收件的那个 Agent 得先跑起来 —— 在面板上点「启动」，
或者开个终端 `uv run anthill agent start echo`。不然 `--wait` 会一直等到超时。

```bash
uv run anthill status                 # 本工作区总览：谁在跑、积压、死信、对端
uv run anthill agent ps               # 这台机器上**所有**工作区里在跑的 agentd
uv run anthill agent list
uv run anthill agent stop echo        # 停一个（面板上点「停止」是同一件事）
uv run anthill runs                   # 历史任务；Ctrl-C 退出观察后从这儿接着看
uv run anthill cost                   # token 用量与花费
uv run anthill agent start echo       # 面板上点「启动」是同一件事
uv run anthill send echo "在吗" --wait 8
uv run anthill run "给 utils/date.py 补单测，并让 reviewer 过一遍"
uv run anthill log echo --follow      # 结构化日志（JSON Lines）
uv run anthill chat coder             # 命令行里多轮对话
```

## 跑测试

```bash
uv sync --all-groups --all-extras
uv run pytest
uv run playwright install chromium    # 想跑浏览器那几条测试才需要（不装会自动跳过）
```

## 出问题时

| 现象 | 先看这个 |
|---|---|
| 端口被占起不来 | 换一个：`--port 45779` |
| 对方收不到消息 | 两边 `anthill status`，看 peers 那行是不是「已信任」 |
| 面板打不开 | 绑的是不是回环？跨机要 `--host 0.0.0.0` |
| 别的机器访问面板 403 | 写操作只对本机开放，远程要 `--panel-token` |
| `--wait` 一直超时 | 收件的那个 Agent 没跑 —— 面板上点「启动」（退出码是 2，脚本里能判） |
| `anthill run` 秒回一句复读 | coordinator 没配 provider —— 现在会直接拦下来，`anthill doctor` 也会点名 |
| 想接进 CI | `--json`（`runs` / `cost`）+ 退出码；`run --plain` 是真流式 |
| 不知道有哪些命令 | `anthill guide` |
| 想知道到底发生了什么 | `anthill log serve --follow`，消息就是文件，也可以直接 `cat` |
