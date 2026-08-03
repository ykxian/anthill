# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本号对应 [docs/04-roadmap.md](./docs/04-roadmap.md) 里的里程碑。

## [0.7] · M6 面板与打磨

### 新增

- **只读 Web 面板**（`anthill serve` → `/panel`）：拓扑（Agent 在跑没跑、积压、死信、
  对端信任状态）、任务看板（每步的状态与交付）、合并后的实时消息流。
  单页 HTML + 原生 JS + WebSocket，**无构建链、无外部资源**，没外网的服务器也能打开。
- 面板**默认只在绑回环时开启**：一旦 `--host 0.0.0.0`（为了让同网段投递进来），
  面板就会跟着暴露给整个网段，所以那种情况必须显式 `--panel`。
- 英文 README、CHANGELOG、MIT LICENSE。

### 修复

- **`httpx` 从来没被声明成依赖**（发布阻断）：`transport/lan.py` 顶层 import 它，
  而它只是被可选的 `llm` extra 顺带装进来的 —— 不装那个 extra 时整个 CLI 起不来。
- **CI 触发分支写成了 `main`，而仓库分支是 `master`**，CI 从来没跑过，
  正好掩盖了上面那条依赖问题。
- `anthill pull` / `anthill approve --peer` 把远端给的文件名与 id 直接拼进路径，
  且 `pull` 少了「收件节点必须是本机」的检查（`/deliver` 一直有，返回 421）。
  已统一要求 ULID 并补上检查。
- 版本号从 `0.1.0` 对齐到 `0.7.0`（此前与 tag 和 CHANGELOG 都对不上）。
- 终端日志里的方括号被 rich 当成样式标记吃掉（`[discovery]` 整段消失）。
- 编排恢复测试里的竞态导致偶发超时（等的条件不是单调的）。
- 面板事件流里每条日志成双出现：`serve` 的日志只有一个文件，
  但它在事件里的 agent 名是 `serve:<节点>`，照名字拼路径把同一个文件读了两遍。

### 设计说明

- 面板的数据源**全部是磁盘上的文件**，不是内存 event bus —— 一个节点上跑着好几个
  进程（每个 agentd 一个，加一个 serve），内存 bus 跨不过进程边界。
- 面板**只读**是刻意的：确认、审批、派活一律留在 CLI。一个只会 `GET` 的页面，
  最坏也就是被人看到状态，没法成为攻击面。

## [0.6] · M5 SSH 跨机

### 新增

- `transport/ssh.py`：asyncssh SFTP 直写远端邮箱，`tmp→rename` 跨机与同机语义一致；
  连接按节点复用、断了自动重连。**服务器不用开任何新端口**。
- `core/spool.py`：回信暂存区。SSH 天生单向（服务器连不回 NAT 后面的笔记本），
  所以投不出去的信封落进 `spool/<对方节点>/`，由对方 `anthill pull` 取走。
- `security/approvals.py`：远端危险操作由本机点头。走文件不走消息 ——
  消息会和串行的消费循环死锁。超时按拒绝处理。
- agentd 收件方验签：共用服务器上，同机器的其他账号也能往你的 inbox 里写文件；
  通道加密拦不住这种本地伪造投递。at-rest 验签刻意不查时间窗（邮箱是存储转发队列）。
- CLI：`anthill pull` / `approve` / `fetch`；peer 支持 `port` / `identity_file` / `known_hosts`。

### 修复

- `anthill pull` 把「连不上服务器」和「没有待取的回信」当成了同一件事，静默返回成功。

## [0.5] · M4 LAN 发现与跨机投递

### 新增

- `security/signing.py`：HMAC-SHA256 覆盖整个信封 + 5 分钟时间窗防重放。
  02-protocol §8 的协议一致性清单**至此全部打钩**。
- `discovery/`：UDP 组播信标（**默认关闭时连 socket 都不创建**）与 TOFU peers 列表。
- `web/app.py`：`/deliver` 端点，准入四道闸（400/403/401/421·404）。
- `transport/lan.py`、`anthill serve`、`anthill peers list/invite/trust/forget`。

### 修复

- **死信无限循环**：不可重试的失败没把条目移出 `outbox/pending`，
  重试循环每秒捡起来一次、每秒报一次死信。
- `invite/trust` 之后回信路由是断的：投递请求改为带 `X-AntHill-Endpoint`，
  收方验签通过后记下来。
- peers 列表被进程内缓存，改为按 mtime 自动重载。
- `peers invite` 打印的令牌被终端折行导致复制后解析失败。

## [0.4] · M3 多 Agent 编排

### 新增

- `orchestrator/`：计划 DAG（校验环与悬空依赖）、运行状态机、BOARD.md 共享黑板、
  事件驱动的 coordinator（拓扑调度、子 thread 隔离、催办与超时、`done_when` 判定与返工）。
- `send_message` 工具：worker 之间点对点 @mention，熔断由协议层的 hops 承担。
- `anthill run`：只读观察者 + rich 实时步骤表，编排逻辑全在 coordinator 里。

### 修复

- 调度器把失败的步骤当成「还没派」反复重派（每个 tick 一次，真接模型就是持续烧钱）。
- 上游失败后下游永远等不到依赖，运行永不收敛 —— 新增 `skipped` 状态。

## [0.3] · M2 Agent 大脑

### 新增

- `providers/`：`ChatProvider` 抽象 + Anthropic / OpenAI 兼容端点，`--record/--replay` 录制回放。
- `agent/tools/`：`read_file` / `write_file` / `list_dir` / `run_shell` / `finish`，
  路径逃逸防护、shell 超时杀进程组、结构化交付。
- `security/policy.py`：工具风险 × 来源信任 → 放行/确认/拒绝。
- `agent/loop.py`：ReAct 循环 + 步数与 token 双熔断；`context.py` 不可信包裹与 token 预算；
  `memory.py` thread 历史落盘与摘要压缩。

### 修复

- shell 白名单放了 `cat`/`ls`/`head`，但参数可以写绝对路径 —— `cat /etc/passwd` 照读不误。
- 熔断抛异常导致已做的工作不落盘，重试等于从头再来。
- `--yes` 的语义其实是「一律拒绝」，名字读起来却像「全部同意」→ 改名 `--unattended`。

## [0.2] · M1 传输层、agentd 与 CLI

### 新增

- `transport/`：Transport 抽象 + 同机 rename 投递。
- `agent/`：watcher（inotify，NFS 上自动降级轮询）、runtime、sender、handler 协议。
- CLI：`init` / `agent start|list` / `send` / `status` / `log --follow`。

### 修复

- 重试循环会抢发「首次投递还在路上」的消息。
- watcher 按文件名去重，导致同 ULID 重投被静默吞掉。

## [0.1] · M0 协议骨架与文件邮箱

### 新增

- `core/`：Envelope 协议、Maildir 变体邮箱与 `tmp→rename` 原子写、seen.db 幂等去重、
  投递状态机、指数退避重试与死信、路由与 @mention 解析、node.toml 与 fail-fast 体检。
- 工程化：uv + ruff + mypy strict + pre-commit + GitHub Actions。
