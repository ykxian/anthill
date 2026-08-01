# AntHill（蚁丘）— 分布式多 Agent 协同系统 · 规划设计文档

> 工作代号 **AntHill**：蚂蚁不开会，它们把信息素留在环境里，别的蚂蚁路过就能读懂并接力。
> 这个机制在学术上叫 **Stigmergy（共识主动性）** —— 恰好就是本项目"文件邮箱"通信思想的理论原型。
> （名字可以随时改，但这个梗在面试里很好讲。）

## 一句话定位

一个基于"**一切皆邮箱**"思想的多 Agent 协同框架：用户下发一条命令，多个 LLM Agent
通过文件邮箱互相通信、分工协作完成任务；支持同机协作、局域网发现（默认关闭）、
以及通过 SSH 打通本地与服务器上的 Agent。

## 文档目录

| 文档 | 内容 | 读者 |
|------|------|------|
| [00-prd.md](./00-prd.md) | 产品需求：背景、目标、功能清单（P0/P1/P2）、非目标、参考项目分析 | 先读这个 |
| [01-architecture.md](./01-architecture.md) | 总体架构：核心概念、分层设计、三种传输的统一模型 | 理解全局 |
| [02-protocol.md](./02-protocol.md) | 通信协议：消息信封、邮箱目录规范、回执机制、可靠性设计 | 开发前必读 |
| [03-tech-design.md](./03-tech-design.md) | 技术设计：技术选型、模块划分、Agent 内核、编排器、安全设计 | 开发时对照 |
| [04-roadmap.md](./04-roadmap.md) | 里程碑与任务清单：M0–M6，每步的验收标准 | 排期用 |
| [05-resume-interview.md](./05-resume-interview.md) | 简历写法与面试问答要点 | 秋招前复习 |

## 思想来源（借鉴映射）

| 来源 | 借鉴了什么 | 本项目的做法 |
|------|-----------|-------------|
| 你们自己的土办法（Claude Code 监控文件夹 + 回执） | 文件邮箱、回执确认这一整套直觉 | 工程化为 LocalTransport + 三级回执协议，是整个系统的地基 |
| [collab-cli](https://github.com/yinsang0910-star/collab-cli) | "It's just files" 哲学、LAN UDP 发现、SHARD.md 分层共享记忆、P0 命令需人工确认 | 邮箱协议 + Discovery 模块（默认关闭）+ blackboard 共享黑板 + 策略引擎 |
| [clowder-ai](https://github.com/zts212653/clowder-ai) | Agent 角色/身份、A2A @mention 路由、跨模型互审（Claude 写码 GPT 审查）、Adapter 接入异构 Agent CLI | 角色化 Agent + @mention 路由 + reviewer 角色 + CLI Adapter 接口 |
| [cat-cafe-tutorials](https://github.com/zts212653/cat-cafe-tutorials) | 踩坑经验：stderr/进程管理、session 串扰、上下文工程 | 写进各文档的"风险与坑"小节 |

## 核心设计决策（速览）

1. **Python 3.11+**，asyncio 单进程守护（`anthill agent` 即一个 agentd）。
2. **自研 Agent Loop 为主**（LLM + 工具调用循环），同时定义 Adapter 接口，
   让 Claude Code 这类现成 CLI 也能作为一种 Agent 挂进邮箱网络。
3. **一切皆邮箱**：三种传输（同机文件 / 局域网 / SSH）只负责"把信封文件送达对方邮箱目录"，
   Agent 消费消息的方式完全一致 —— 这是整个架构最漂亮的一层抽象。
4. **默认不广播**：LAN 发现是显式 opt-in，且有 allowlist + 消息签名，
   避免一台机器上的多个 Agent 互相干扰。
5. **多模型接入**：Anthropic SDK + OpenAI 兼容接口（覆盖 OpenAI/DeepSeek/Qwen/GLM），
   不同角色可配置不同模型（如 DeepSeek 写码、Claude 审查）。
