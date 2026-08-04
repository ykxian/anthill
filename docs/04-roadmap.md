# 04 · 里程碑与任务清单

> 节奏假设：学生课余时间，每周 10–15 小时（Claude Code 辅助开发）。总计约 7 周。
> 铁律：**每个里程碑结束时都有一个能独立演示的东西**，秋招提前到来也不至于空手。

## 总览

```mermaid
flowchart LR
    M0[M0 协议骨架<br/>0.5周] --> M1[M1 邮箱与回执<br/>1周] --> M2[M2 Agent大脑<br/>1.5周]
    M2 --> M3[M3 多Agent编排<br/>1周] --> M4[M4 LAN发现<br/>1周] --> M5[M5 SSH跨机<br/>1周] --> M6[M6 面板与打磨<br/>1周]
```

依赖关系：M4 与 M5 相互独立，可按兴趣调换顺序；M6 的面板部分可与 M4/M5 并行。

---

## M0 · 协议骨架（0.5 周）

**目标**：协议层代码 + 测试全绿，还没有任何 LLM。

- [x] 仓库初始化：uv + pyproject + ruff/mypy/pre-commit + GitHub Actions
- [x] `core/envelope.py`：Envelope 与各 payload 的 pydantic 模型（TDD：02-protocol §8 用例 1）
- [x] `core/mailbox.py`：目录创建、原子写、归档、seen.db（用例 2/3/4）
- [x] `core/ids.py`、`core/config.py`（node.toml 解析 + fail-fast 校验）

**验收**：`pytest` 全绿；并发 100 写者压力测试无丢失无重复；覆盖率 ≥ 90%（此层）。

**实际结果（已达成）**：125 个测试全绿；envelope 96% / payloads 100% / seen 100% /
router 98% / states 96% / outbox 95% / mailbox 92%；mypy strict 与 ruff 全绿。
`core/mailbox.py` 拆成了 `atomic.py`（原子写）+ `mailbox.py`（目录语义）+ `seen.py`（去重），
另外多出 `paths.py`（目录布局）、`states.py`（回执状态机）、`outbox.py`（重试）、`router.py`（寻址）。

## M1 · 邮箱与回执 —— 土办法的工程化（1 周）

**目标**：两个 agentd（还没大脑，收到消息只会回显）在同一台机器上互发消息、互回回执。

- [x] `transport/local.py` + `transport/base.py`（另加 `transport/registry.py` 按目标选传输）
- [x] `agent/runtime.py`：watcher（含 NFS 降级自检）→ 队列 → 分发骨架
- [x] 回执状态机 + outbox 重试（用例 5）、hops 熔断（用例 6）
- [x] CLI：`anthill init`、`anthill agent start/list`、`anthill send`、`anthill status`、`anthill log --follow`

**验收**：终端 A 启动 echo-agent，终端 B `anthill send` 一条消息，
能看到 accepted 回执与回显 result；kill -9 任一方后重启不丢消息。
**这一步完成，你们团队现在的手工方案就已经被正式替代了。**

**实际结果（已达成）**：手工联调跑通 `init → agent start → send --wait`，
accepted 回执与 task.result 都能原路回到 CLI 邮箱；agent 未启动时投递的消息在启动后
被补处理；`cur/` 里的崩溃残留会在启动时 requeue（日志 `agentd.recover requeued=1`）。

联调时抓到两个测试没覆盖的真 bug，已修并补了回归测试：
1. 重试循环会抢发「首次投递还在路上」的消息（日志现象：`delivery.retry attempts=0`
   加 `非法状态迁移：delivered → delivered`）→ Sender 增加 in-flight 集合。
2. watcher 按文件名去重，导致同一 ULID 被重复投递时第二次被静默吞掉
   → 去重键改为 `(文件名, inode)`，把判重交还给 seen.db 这一层。

## M2 · Agent 大脑（1.5 周）

**目标**：单个 Agent 能用 LLM + 工具真正完成任务。

- [x] `providers/`：openai_compat + anthropic，`--record/--replay` 录制回放
- [x] `agent/tools/`：read_file / write_file / list_dir / run_shell / finish（路径逃逸防护）
- [x] `security/policy.py`：风险分级 + CLI 确认流
- [x] `agent/loop.py` + `context.py`：ReAct 循环、token 预算、步数/费用熔断
- [x] `agent/memory.py`：thread 历史落盘 + 超长摘要

**验收**：`anthill send coder "给 demo 项目的 date.py 写单测并跑通"`，
Agent 自主完成写文件 → 跑 pytest → 交付结构化 result；危险命令会弹确认。

**实际结果（已达成）**：234 个测试全绿，总覆盖率 89%；mypy strict 与 ruff 全绿。
用录制带在真实 CLI 上跑通 `init → agent start --replay → send --wait`：
Agent 写出 `greet.py`，试图跑 `python3 -c ...` 时被策略引擎拦下（无人值守 → 拒绝），
随后用 `finish` 交付 `{summary, artifacts:["greet.py"], status:"ok"}` 回到 CLI 邮箱。

几处实现上偏离原设计、值得在面试里讲的决定：

1. **未知来源在进上下文前就被拒**，而不是靠工具层逐次拦。理由：prompt 注入的成本
   应该停在「模型根本没看到它」这一步，而不是指望模型每次都守规矩。
2. **不可信定界符做转义**。来件里若自带一个「结束定界符」想逃出数据区，会被打断成
   `..._ESCAPED>>>` —— 只声明规则而不做转义，等于把安全性全押在模型的自觉上。
3. **工具失败不抛异常，而是作为 `role=tool` 结果回喂**。让模型看见错误自己改，
   是 ReAct 最有价值的部分；只有熔断与模型调用失败才中止任务。
4. **`--unattended` 不是「全部同意」而是「一律拒绝」**。第一版把它命名成 `--yes`，
   联调时立刻意识到这个名字会让人误以为是自动放行 —— 安全开关的命名不能有歧义。
5. **回放模式跳过 API key 体检**。`--replay` 根本不连上游，却因为「没 export key」
   被 fail-fast 拒绝启动，是把体检做成了教条。

## M3 · 多 Agent 编排（1 周）—— 核心演示场景 A 达成

- [x] `orchestrator/plan.py`：计划 JSON schema + 强制结构化输出（校验失败重试）
- [x] `orchestrator/coordinator.py`：拓扑调度、子 thread、催办、`done_when` 判定
- [x] @mention 路由 + `send_message` 工具
- [x] blackboard：BOARD.md 单写者维护 + 任务产物目录
- [x] `anthill run "<任务>"` 端到端命令 + rich 实时消息流渲染

**验收**：场景 A 全流程（coder 写 → @reviewer 审 → coder 改 → 汇总）无人工干预跑通，
两个角色用两家模型；构造 @ 循环被熔断。**录第一支演示视频。**

**实际结果（已达成）**：318 个测试全绿，总覆盖率 89%；mypy strict 与 ruff 全绿。
用录制带在真实 CLI 上跑通场景 A：`anthill run "给 greet.py 补单测并让 reviewer 过一遍"`
→ boss 拆出 2 步 → coder 写出 `test_greet.py` → reviewer 读文件后 approve
→ done_when 判定通过 → 汇总回到 CLI。**Agent 写出来的测试 `pytest` 真的能跑通（2 passed）。**
（演示视频待录）

这一步最关键的设计决定与踩的坑：

1. **coordinator 是事件驱动的状态机，不是一段从头跑到尾的流程。**
   agentd 的消费循环是串行的：如果 coordinator 在 handler 里 `await` 下游 worker 的结果，
   而那个结果又要经同一个循环投递进来，就是**死锁**。所以它处理完一条消息立刻返回，
   状态全部落在 `blackboard/tasks/<id>/state.json` 上。
   附带收获：崩溃重启能接着调度 —— 有一个集成测试专门换一个全新的 coordinator 实例来验证这点。
2. **调度器的「就绪」判定漏了失败态，导致无限重派。**
   `ready()` 原本只排除「在跑的」步骤，于是超时判失败的那一步又变回「可派发」，
   每个 tick（5s）重派一次 —— 真接上模型就是持续烧钱。修成排除所有非 pending 的步骤。
   这个 bug 是被「worker 一直不回话」的集成测试逼出来的，单测覆盖不到。
3. **上游失败的分支会让整次运行永远收敛不了。**
   s2 依赖 s1，s1 失败后 s2 永远等不到依赖，`all_settled` 永远是 False，用户就一直等不到结果。
   加了 `skipped` 状态（与 `failed` 分开记，排查时一眼看出谁真的坏了）。
4. **@ 循环的熔断不写在工具里。** `send_message` 走 `Envelope.reply()` 构造消息，
   hops 自动 +1，超过 `ttl_hops` 时构造就失败 —— 熔断在协议层，工具层一行相关逻辑都没有。
5. **`anthill run` 不含任何编排逻辑**，只是个只读观察者（轮询黑板 + 自己的收件箱）。
   所以 Ctrl-C 掉 CLI 不影响后台协作继续跑完。

## M4 · LAN 发现与投递（1 周）—— 场景 C

- [x] `discovery/beacon.py`：UDP 组播 announce/probe，**默认 enabled=false**
- [x] `discovery/registry.py`：peers 列表、`anthill peers trust`（TOFU 指纹）
- [x] `security/signing.py`：HMAC + 时间窗防重放（用例 7）
- [x] `transport/lan.py` + FastAPI `/deliver` 端点（未信任来源直接拒收）

**验收**：两台机器（或同机两个 workspace + 不同端口模拟）：默认互相不可见；
双方开启 discovery 并 trust 后可互派任务；抓包确认关闭时零发包；
篡改消息被拒收。

**实际结果（已达成）**：392 个测试全绿，总覆盖率 89%；mypy strict 与 ruff 全绿。
02-protocol §8 的一致性清单**至此全部打钩**（用例 7 的三种攻击各有一条测试）。
同机两个 workspace + 两个端口做了真实联调：`peers invite/trust` 配对 → 两端
`anthill serve` → `anthill send lab-server:echo ... --wait` → accepted 回执与
task.result 都跨 HTTP 原路回到 CLI 邮箱。「关闭时零发包」用 `ss -uan` 取证：
默认配置下 45777 上零监听，改成 `enabled = true` 后才出现。

设计决定：

1. **一个节点一个接收端进程（`anthill serve`），不是每个 Agent 一个端口。**
   收下来的信封直接原子写进对应 Agent 的 inbox/new，剩下的交给那个 agentd 的 watcher。
   HTTP 只是「又一种把文件送进目录的方式」，agentd 完全不知道消息来自网线。
2. **准入四道闸，任何一道不过都不落盘**：能否解析（400）→ 发件节点是否被信任（403）
   → 签名与时间窗（401）→ 收件人是否本机已存在的 Agent（421/404）。
   421 那一条是「不当跳板」：绝不代为转投第三方。
3. **默认既不广播也不对外**：`discovery.enabled=false` 时连 socket 都不创建；
   `serve` 默认只绑 127.0.0.1，要对外必须显式 `--host 0.0.0.0`。
4. **防重放是两道**：时间窗 5 分钟（本层）+ id 去重（seen.db，M0 就有）。
   只有前者能在窗口内重放；只有后者则 seen.db 得永久保留。合起来 seen.db 只需存一个窗口。

联调抓到三个测试没覆盖的真 bug（均已修 + 补回归）：

1. **死信无限循环（最严重）**：不可重试的失败只上报了 coordinator，却没把条目移出
   `outbox/pending` —— 重试循环每秒捡起来一次、每秒报一次死信。日志和 coordinator
   邮箱会被慢慢刷爆。修：新增 `Outbox.abandon()`，不可重试即刻进死信。
2. **回信路由是断的**：`invite/trust` 配好后，被邀请方在邀请方的 peers 里没有 endpoint，
   单向能通、回信发不出去。修：投递请求带 `X-AntHill-Endpoint`，收方**验签通过后**
   记下来（能伪造这个头的人已经有共享密钥，所以不构成新攻击面）。
3. **peers 列表被进程内缓存**：一个节点上跑着 serve + 若干 agentd，
   serve 学到的地址 agentd 看不见。修：按 mtime 自动重载 —— 文件是唯一真相，内存只是缓存。

还有一个纯可用性 bug：`peers invite` 打印的令牌被 rich 按终端宽度折行，
用户复制过去就带着换行导致解析失败。两头都修了（打印用 soft_wrap，解析先清空白）。

## M5 · SSH 跨机（1 周）—— 场景 B

- [x] `transport/ssh.py`：asyncssh SFTP 投递（tmp→rename）、连接复用与重连
- [x] 远端 artifacts 按需拉取（result 引用的产物 SFTP get）
- [x] 远端 high 风险操作强制本地确认的策略打通

**验收**：本地 coordinator 派任务给学校服务器上的 runner agent，
远端跑 pytest 并回传失败归因；全程服务器不开任何新端口。**录第二支演示视频。**

**实际结果（已达成）**：454 个测试全绿，总覆盖率 88%；mypy strict 与 ruff 全绿。
测试用 asyncssh 在**进程内起真的 SSH + SFTP 服务端**，所以测的是真链路（真握手、
真 SFTP 写、真 rename），不是打桩。手工联调跑通完整场景 B：
`send lab-server:echo` → SFTP 落进远端邮箱 → 远端 agent 处理 → 回信暂存 →
`anthill pull lab-server` 取回 accepted 回执与 task.result。
`ss -tlnp` 确认服务器上除 sshd 外零新增端口。

这一步最重要的发现与决定：

1. **SSH 是单向的，回信只能靠拉。**
   联调时才意识到：笔记本能 SSH 到服务器，服务器**连不回笔记本**（NAT 后面、
   也没跑 sshd）。那远端干完活结果怎么送回来？答案是 `core/spool.py`：
   投不出去的信封落进 `.anthill/spool/<对方节点>/`，由对方 `anthill pull` 取走 ——
   **和 `git pull` 一个道理**。信封原样保留（id/签名/thread 都不变），
   拉回去之后走的是和同机投递完全一样的处理路径。
   开关默认关闭（`[runtime] spool_unroutable`），关闭时路由不到就是死信，行为不变。
2. **远端危险操作的确认走文件，不走消息 —— 因为消息会死锁。**
   agentd 的消费循环是串行的：handler 里 await 一条回信，那条回信却要经同一个
   循环投递进来。所以 `security/approvals.py` 用两个文件（请求 + 答复）+ 轮询，
   轮询发生在**同一个协程**里，不经过消费循环。
   本机 `anthill approve --peer lab` 经 SFTP 读列表、写回答复；服务器上不需要有人。
   超时按拒绝处理：没人管的危险操作，默认不做。
3. **收件方验签才让签名有意义。**
   邮箱就是一个目录：共用服务器上，同机器的其他账号也能往里面写文件。
   SSH/LAN 通道的加密拦不住这种「本地伪造投递」。所以 agentd 读到跨节点信封时
   会验签（只要本机持有对方密钥），不通过就进隔离区。
   **at-rest 验签刻意不查时间窗** —— 邮箱是存储转发队列，agentd 停机几小时
   再启动是正常的，按 5 分钟窗判会把一堆合法消息误杀；重放由 seen.db 兜。

联调抓到的 bug：

- **`anthill pull` 把「连不上服务器」和「没有待取的回信」当成了同一件事**，
  静默返回成功。用户会以为回信收完了，其实还堆在服务器上。
  修：只把 SFTP 的「目录不存在」当作正常，连接失败照常报错。
- **没法给 peer 单独指定 known_hosts**：真实场景用 `~/.ssh/known_hosts` 没问题，
  但专用密钥 / 专用 known_hosts 的部署配不了。补了 `known_hosts` 配置项 ——
  注意**没有**「跳过主机指纹校验」的开关，那是这条路的安全前提。

## M6 · 面板与打磨（1 周）

- [x] `web/`：拓扑 + 实时消息流 + 任务看板（只读，127.0.0.1）
- [x] README（中英）：架构图、快速开始 —— **三支演示 GIF/视频仍待录**（需要本人录屏）
- [x] 补测试到全项目 ≥ 80%（实际 88%）；CHANGELOG；MIT LICENSE
- [ ] （加分，二期候选）Claude Code Adapter：把你们的文件夹监控用法接成正式 Agent

**验收**：陌生同学按 README 10 分钟跑通场景 A；covr 报告 ≥ 80%；仓库开源发布。

**实际结果（除演示视频外已达成）**：493 个测试全绿，总覆盖率 88%；
mypy strict 与 ruff 全绿。**README 的快速开始是照着文档一字不差跑过一遍的**
（`uv run anthill init` → `agent start echo` → `send --wait 8` → 收到回执与 result），
`anthill serve` 与面板同样按文档验证过。

面板的两个设计决定：

1. **数据源全部是磁盘上的文件，不是内存 event bus。**
   一个节点上跑着好几个进程（每个 agentd 一个，加一个 serve），内存 bus 跨不过
   进程边界。好在这个项目里「文件就是唯一真相」，面板照着读就行。
2. **面板默认只在绑回环时开启。**
   一旦 `--host 0.0.0.0`（为了让同网段投递进来），面板会跟着暴露给整个网段。
   要那样必须显式 `--panel` —— 不给默认踩坑的机会。面板本身也只读，
   一个只会 `GET` 的页面没法成为攻击面。

打磨阶段抓到的两个 bug：

1. **面板事件流里每条日志成双出现**：`serve` 的日志只有一个文件，
   但它在事件里的 agent 名是 `serve:<节点>`，照名字拼路径把同一个文件读了两遍。
2. **终端日志里的方括号被 rich 当成样式标记吃掉**：照 README 跑 `anthill serve` 时
   看到 `hint=node.toml 里  enabled = false` —— `[discovery]` 整段消失了。
   日志字段装的是任意内容（任务标题、错误信息、文件路径、模型输出），
   被悄悄删掉一段比没有日志更误导人。修：渲染前一律 escape。

一处**刻意不做**的取舍：`follow_log` 是个阻塞的 tail -f 循环，没有为它写线程测试 ——
要测就得起线程等它，而一个可能挂住整个套件的测试，代价远大于它带来的覆盖。
它的验证靠 `anthill log --follow` 的手工使用。这一点在测试文件里写明了，不是漏掉。

### 收尾复查（M6 之后单独做的一轮）

全部里程碑做完后又整体查了一遍，抓到一个**发布阻断级**问题和两处一致性缺口：

1. **`httpx` 从来没被声明成依赖**。`transport/lan.py` 在模块顶层 import 它，
   而它一直只是被 `openai`/`anthropic` 顺带装进来的 —— 也就是说，
   不装可选的 `llm` extra 时 **`import anthill.cli.main` 直接失败，整个 CLI 起不来**。
   本地一直没发现，是因为开发环境早就装了 llm extra。
2. **CI 的触发分支写的是 `main`，而仓库分支是 `master`** —— CI 从来没跑过，
   正好把上面那个依赖问题也一起掩盖了。两个 bug 互相打掩护，这是最难自查的一类。
3. **三条入口只有 `/deliver` 做了边界校验**：`anthill pull` 把远端给的文件名直接
   拼进「读」和「删」的路径、也没检查收件节点是不是本机（`/deliver` 有这个 421 检查）；
   `anthill approve --peer` 把远端文件内容里的 `id` 直接拼进写路径。
   远端本来就是被信任的，所以严重度不高，但「在边界上校验」是本项目反复强调的原则，
   同类入口的做法不该不一致。已统一：文件名与 id 一律要求是 ULID，并补上收件节点检查。

另外把版本号从 `0.1.0` 对齐到 `0.7.0`（此前与 tag、CHANGELOG 都对不上）。
修完用 `uv run --isolated` 在 Python 3.11/3.12 × 带/不带 extras 四种组合下复验通过。

---

## 全部里程碑完成（2026-08-01）

M0–M6 六个里程碑均已交付并打 tag（v0.1 ~ v0.7），每个 tag 都验证过能独立跑通自己那一版的测试。
剩余待办：**三支演示视频**（场景 A/B/C，需要本人录屏）、开源发布、以及二期候选的 Claude Code Adapter。

## M7 · 接入已有终端、Agent 对话、面板可写（用户提出的二期需求）

M0–M6 之后用户提了三点：**支持 Claude Code 这类已有终端**、
**很多时候的需求是 agent 之间的对话**、**面板只读功能太少，希望能配置与发起任务**。

- [x] `adapters/cli_agent.py`：把「给一段 prompt、吐一段结果」的命令行 Agent 接进来
- [x] `agent/conversation.py`：@ 谁回给谁 + 按 thread 的轮次预算
- [x] `anthill chat` / `anthill talk`
- [x] 面板写入口：发起任务、发消息、在线改 node.toml

三个设计上的关键取舍：

1. **外来终端就是又一个 handler**，不是特例。它和自研 Agent 守同样的规矩：
   来件包不可信定界块、按 thread 记上下文（外来 CLI 每次都是新进程，自己不记事，
   上下文得我们塞回去）、超时杀整个进程组。
   **唯一不同是工具与策略引擎管不到它** —— Claude Code 有自己的权限体系，
   我们不代管，但在文档里讲清楚：你给它什么权限，它就有什么权限。
   结构化交付做成「能给最好，不给也不影响用」：外来 Agent 不受我们控制，不能强求格式。
2. **对话的两个缺口各有确定性的答案**。
   「聊不起来」是因为回信永远发给发件人 —— 用协议里本来就有的 `mentions`：
   带 @ 的对话回信发给被 @ 的那个人，并把自己 @ 回去。
   「停不下来」此前只能靠 hops，但 hops 是协议层的兜底、一响就说明出事了 ——
   改成按 thread 数轮次，**不依赖模型自觉说「我说完了」**。
3. **面板的写权限按「等同于本机执行命令」来设计**：能改 node.toml 就能加一个带
   `run_shell` 的 Agent。所以两道闸缺一不可 —— 显式开关（默认关）+ **逐请求**
   校验来源是回环，而不是「我们没绑 0.0.0.0 所以应该安全」（反向代理、端口转发
   都会让那个假设失效）。写入口默认根本不挂（404），不是「挂着但会拒绝」。
   改配置先用启动期同一套模型校验，不合法磁盘一个字都不改。确认与审批仍然只在 CLI。

顺带修掉两个真 bug：

1. **模型纯文本回复时，它自己说的话没进 thread 记忆** ——
   多轮对话里 Agent 记得别人说过什么，却不记得自己说过什么，会变成单方面复读。
   （对话轮次数不上去只是这个 bug 的一个症状。）
2. **coordinator 在「结果已发出、状态还没存」之间被 Ctrl-C，重启后会再发一遍结果。**
   改成先落盘再发送 —— 和 Sender 同一条原则。这个窗口只有几毫秒，
   但一个偶发失败的测试把它揪了出来。

### 常驻交互式会话的桥接（用户追加的需求）

用户看完 CLI 适配器后指出：真正想要的是**让一个常驻的交互式 Claude Code 会话参与
协作，人还能中途手动插话**。CLI 适配器是每条消息起一个新进程，满足不了这个。

于是有了 `adapters/bridge.py` —— 也就是这个项目**起点那个土办法的正式版本**：
收到的消息写成 `bridge/inbox/<信封id>.md`，人或常驻会话把回复写进 `bridge/outbox/`。

三个关键设计：

1. **不阻塞**。`handle()` 只写文件就返回，回复由 runtime 的定时 tick 扫 outbox 发出。
   人可以想十分钟，期间 Agent 照常收新消息，几条一起躺在 inbox 里等 ——
   这是它和 cli_agent 适配器最根本的区别（那个是同步等子进程）。
   回执仍然由 runtime 立即发出，发起方不会以为消息丢了。
2. **人能主动插话**。既然回复是「扫 outbox」，那 outbox 里放一个带 `to:` 的文件
   自然就是**主动发起**一条消息，不必是对谁的回复。这个能力是设计的副产品，
   但恰恰是用户要的那半 —— 人可以随时插进正在进行的协作里说一句。
3. **「写完了」用 mtime 判定**（至少 1 秒前）。编辑器与 Agent 都是边写边刷盘的，
   抢在中途读走会发出半句话。这比要求人写个结束标记更不容易出错。

前端格式刻意极简：`--- / key: value / ---` 加正文，**不引 YAML** ——
人要手写它，多一分语法就多一分写错的机会。模板里那段说明注释会被自动剥掉，
因为人多半懒得删。

联调时发现一个可用性问题：`anthill send` 默认取正文前 60 字当标题，
桥接渲染时把标题和正文拼在一起，同一句话被读了两遍。已修。

567 个测试全绿，覆盖率 87%。

## 里程碑外的持续事项

- 每个 M 结束：更新 docs、给 demo 打 git tag（v0.1 ~ v0.7）
- 每写完一个模块跑 code review（CLAUDE 规则里的 code-reviewer 流程）
- 花销记录：provider 层自动统计 token 费用，写进 logs，防止调试烧钱失控

## 砍单预案（时间不够时的优先级）

秋招时间紧张时按此顺序砍：M6 面板 → M4 LAN → （M5 SSH 保留，它是差异化亮点）。
M0–M3 + M5 就足以支撑一份有说服力的简历项目；M4 可以只留设计文档并在面试中口述。
