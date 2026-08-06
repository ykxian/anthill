# 让常驻的 Claude Code 会话自动收发

桥接 Agent 的设计是给人留的口子：收到的消息写成 `bridge/inbox/*.md`，
你（或你一直开着的 Claude Code 会话）把回复写进 `bridge/outbox/`。

但它的集成方式一直是**被动**的 —— 面板给你一句现成的提示词，你粘给会话，
让它「盯着这个目录」。Claude Code 不会被通知，只有你主动说「看一下收件箱」
它才去看。**人肉转述**这一步没去掉。

这个目录里是最便宜的解法：一个 hook。

## 装

**别写进 `~/.claude/settings.json`** —— 那是全局的，你开的每一个 Claude Code
都会去查这个收件箱，而通常只有一两个会话需要接进来。

按作用域从窄到宽：

| 放哪 | 谁受影响 | 进 git 吗 |
|---|---|---|
| `<项目>/.claude/settings.local.json` | 只有这个项目、只有你 | 不（默认被忽略） |
| `<项目>/.claude/settings.json` | 这个项目的所有人 | 进 |
| `~/.claude/settings.json` | **你开的每一个会话** | 不 |
| `claude --settings <文件>` | 只有这一次启动 | 不 |

想接进来的那个会话，用 `.claude/settings.local.json`；只想试一次，
用 `claude --settings`。

```jsonc
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "command": "anthill bridge cc --json -w /path/to/your/workspace",
        "description": "每轮开始前看看 AntHill 那边有没有消息在等这个会话"
      }
    ]
  }
}
```

hook 的输出会进入这一轮的上下文，所以会话在开始干你交代的事之前，
自己就知道「有两条消息在等我回」。

## 为什么是 hook，不是 MCP

值得说清楚，免得搞反因果：

**MCP 工具也是拉取式的** —— 模型自己决定什么时候调。装了 MCP server 之后，
Claude Code 依然不会主动知道有消息在等它。真正去掉人肉转述的是**这个 hook**。

MCP 让那次调用更规整（有 schema、不用解析 CLI 输出、不只限 Claude Code），
所以两个都值得有 —— 但如果你只想先试试「自动收发到底顺不顺手」，
这一个文件就够了，不用等 MCP。

装了 MCP server（`anthill mcp serve`）之后，把 hook 的命令换成一句提示也行：

```jsonc
{ "command": "echo '先调 anthill_inbox 看看有没有消息在等你'" }
```

## 输出长什么样

```json
{
  "agent": "cc",
  "count": 1,
  "waiting": [
    {
      "id": "01KZ81Y0QGREFVNSPQK02BKPZ5",
      "short": "2BKPZ5",
      "from": "laptop:coder",
      "type": "chat",
      "thread": "01KZ81Y0PM3FW1XZRBXMW7BT1K",
      "body": "这块接口我想改成异步的，你那边有依赖吗"
    }
  ],
  "inbox": "/path/to/.anthill/agents/cc/bridge/inbox",
  "outbox": "/path/to/.anthill/agents/cc/bridge/outbox",
  "reply_hint": "回复：anthill bridge cc --reply <id> --text '…'，或直接在 …/outbox 下写同名 .md"
}
```

`count` 为 0 时其余字段照常给 —— 让会话知道「查过了，没有」，
比什么都不输出更有用。
