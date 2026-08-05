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

桥接 Agent 背后是「一个人」，所以它不自己想 —— 别人发给它的消息会在那一页列出来等着回。
两种回法，**随时可以混着用**：

- **就在页面上回。** 消息下面就是输入框，写完点「回复」。也能主动发一条给别人。
  不用开终端，也不用有 Claude Code。
- **交给一个常开的 Claude Code 会话。** 那一页顶上有一句现成的话（带「复制」按钮），
  粘给你的会话就行 —— 路径已经填好了，不用自己拼。

两条路写的是同一批文件（`bridge/inbox/` 收，`bridge/outbox/` 回），
所以会话在盯着的同时，你照样可以在网页上先替它回一句。

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
