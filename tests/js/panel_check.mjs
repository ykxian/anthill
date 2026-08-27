/* 在 Node 里跑一遍面板的脚本，验证两路数据合成一张图的逻辑。
 *
 * 面板有两个数据源：WebSocket 每 2 秒推**本机**快照，api/cluster 每 5 秒拉**全集群**。
 * 一旦 WS 那一路直接替换整个节点列表，别的机器就会在页面加载一秒后凭空消失 ——
 * 这正是总控面板最容易坏、又最不容易被人发现的地方，所以单独钉一颗钉子。
 *
 * 用法：node panel_check.mjs <panel.html 的路径>
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const html = readFileSync(process.argv[2], "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

assert.ok(html.includes('id="rcp-codex-current"'), "桥接页没有“发给当前 Codex”的提示词入口");
assert.ok(
  html.includes('data-copy="rcp-codex-current"'),
  "当前 Codex 接入提示词没有一键复制按钮",
);

// ---- 标签页、PANES、<section> 三者必须一一对应 ----
//
// 漏掉其中一处的后果特别隐蔽：那一页的数据照常拉、照常渲染，**只是永远不显示**
// —— showPane 把别的 pane 藏起来了，却没把这个放出来。页面上看就是「点了没反应」，
// 而控制台干干净净。加「Agent 往来」那次就这么栽过一次。
const tabs = [...html.matchAll(/<button class="tab[^"]*" type="button" data-pane="([a-z]+)"/g)]
  .map((m) => m[1]);
const panes = JSON.parse(script.match(/const PANES = (\[[^\]]*\]);/)[1].replace(/'/g, '"'));
assert.deepEqual([...tabs].sort(), [...panes].sort(), "标签页与 PANES 对不上");
for (const pane of tabs) {
  assert.ok(html.includes(`id="pane-${pane}"`), `有标签页 ${pane} 但没有 #pane-${pane}`);
}

// 原生对话框清零：确认走 ask()，输入走 ask 的 input 变体，信息走 tell() ——
// window.confirm 装不下三种意图（「取消」被迫变过「删得少一点」），
// prompt 把两种意图塞进空串重载，alert 在无头/自动化环境里被静默吞掉。别再回去。
assert.ok(!script.includes("window.confirm("), "又用回 window.confirm 了 —— 破坏性确认走 ask()");
assert.ok(!script.includes("window.prompt("), "又用回 window.prompt 了 —— 输入走 ask 的 input 变体");
assert.ok(!script.includes("window.alert("), "又用回 window.alert 了 —— 信息走 tell()");

// ---- 最小 DOM 桩：面板只用到这几样 ----

const elements = new Map();
const blank = () => ({
  textContent: "",
  innerHTML: "",
  className: "",
  value: "",
  hidden: false,
  disabled: false,
  scrollTop: 0,
  scrollHeight: 0,
  clientHeight: 0,
  dataset: {},
  addEventListener() {},
});
const $ = (id) => {
  if (!elements.has(id)) elements.set(id, blank());
  return elements.get(id);
};
let fallbackCopyBox;
let fallbackCopied = "";
const document = {
  getElementById: $,
  title: "",
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener() {},
  createElement: () => {
    fallbackCopyBox = {
      value: "",
      style: {},
      setAttribute() {},
      focus() {},
      select() {},
      setSelectionRange() {},
      remove() {},
    };
    return fallbackCopyBox;
  },
  body: { appendChild() {} },
  execCommand: (command) => {
    fallbackCopied = fallbackCopyBox?.value || "";
    return command === "copy";
  },
};
const window = { prompt: () => null, alert() {}, confirm: () => false, isSecureContext: true };
const navigator = {
  clipboard: { writeText: async () => { throw new Error("permission denied"); } },
};
const localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
const history = { replaceState() {} };
class URL {
  constructor() { this.searchParams = { get: () => null, delete() {} }; }
  toString() { return "http://panel.test/panel"; }
}
const location = { protocol: "http:", host: "panel.test", pathname: "/panel", href: "http://panel.test/panel" };
const fetch = async () => {
  throw new Error("测试里不连网");
};
class WebSocket {
  constructor() {
    throw new Error("测试里不开 WS");
  }
}
const noop = () => 0;

const panel = new Function(
  "document",
  "window",
  "localStorage",
  "history",
  "URL",
  "location",
  "fetch",
  "WebSocket",
  "navigator",
  "setInterval",
  "setTimeout",
  // setWrite：写权限开着时拓扑会多画启停/删除按钮和「加 Agent」表单，
  // 那几处也把外部数据拼进了 HTML，必须一起验
  `${script}\nreturn { state, local, topoOpen, draw, applyCluster, applyLocal, renderWorkspaces, renderConnect, copyText, chat, renderChat, md, plainPreview, stripScaffold, patchToml, diffLines, renderDiff, agentEndpoint, setWrite: (v) => { canWrite = v; } };`,
)(document, window, localStorage, history, URL, location, fetch, WebSocket, navigator, noop, noop);

// ---- 数据 ----

const agent = (name) => ({
  name,
  role: "worker",
  provider: "echo",
  running: true,
  watch_mode: "inotify",
  queue: 0,
  dead: 0,
});

const CLUSTER = {
  node: "laptop",
  nodes: [
    {
      node: "laptop",
      local: true,
      reachable: true,
      agents: [agent("cli")],
      runs: [],
      events: [{ ts: "01", agent: "cli", event: "serve.start", level: "info", detail: "" }],
      peers: [],
    },
    {
      node: "lab",
      local: false,
      reachable: true,
      agents: [agent("echo")],
      runs: [
        {
          goal: "给 utils 补单测",
          finished: false,
          steps: [{ id: "s1", state: "running", assignee: "echo", detail: "写测试" }],
        },
      ],
      events: [{ ts: "02", agent: "echo", event: "msg.received", level: "info", detail: "" }],
      peers: [],
    },
    {
      node: "server",
      local: false,
      reachable: false,
      reason: "ConnectError: All connection attempts failed",
      agents: [],
      runs: [],
      events: [],
      peers: [],
    },
  ],
};

const LOCAL_PUSH = {
  node: "laptop",
  agents: [agent("cli"), agent("coder")],
  runs: [],
  events: [{ ts: "03", agent: "cli", event: "delivery.ok", level: "info", detail: "" }],
  peers: [],
};

// ---- 断言 ----

panel.applyCluster(CLUSTER);
assert.deepEqual(
  panel.state.nodes.map((n) => n.node),
  ["laptop", "lab", "server"],
  "总控视图应当原样带上三个节点",
);

// 回归点：WS 推来的是本机快照，只能换掉本机那一格
panel.applyLocal(LOCAL_PUSH);
assert.deepEqual(
  panel.state.nodes.map((n) => n.node),
  ["laptop", "lab", "server"],
  "WS 推送把远端节点抹掉了 —— 总控面板会在加载一秒后只剩本机",
);
assert.deepEqual(
  panel.state.nodes[0].agents.map((a) => a.name),
  ["cli", "coder"],
  "本机那一格应当用 WS 的新快照",
);
assert.equal(panel.state.nodes[1].agents.length, 1, "远端那一格不该被动过");

// 单机节点没有 nodes 字段，页面要自己兜成一个节点
panel.applyCluster({ node: "solo", agents: [agent("echo")], runs: [], events: [], peers: [] });
assert.deepEqual(
  panel.state.nodes.map((n) => n.node),
  ["solo"],
);
assert.equal(panel.state.nodes[0].local, true);

// 画出来的东西也得对：连不上的节点要标出来，而不是悄悄消失
panel.applyCluster(CLUSTER);
const topo = $("topo-body").innerHTML;
for (const name of ["laptop", "lab", "server"]) {
  assert.ok(topo.includes(name), `拓扑里少了节点 ${name}`);
}
assert.ok(topo.includes("连不上"), "连不上的节点没有标出来");

assert.ok(topo.includes("All connection attempts failed"), "没有告诉人连不上的原因");
assert.ok($("runs-body").innerHTML.includes("lab"), "编排任务没标出它跑在哪台机器上");

const stream = $("stream-body").innerHTML;
assert.ok(stream.includes("serve.start") && stream.includes("msg.received"), "事件没有跨节点汇总");
assert.equal($("topo-count").textContent, "3 个节点 · 2/2 在跑");

// Agent 卡片可能来自主节点以外的本机工作区。删除、启停、角色卡都必须使用
// 按钮自己携带的 node，不能默默落到主节点；card-tst 的删除故障就出在这里。
assert.equal(
  panel.agentEndpoint("card-tst", "", "collab-tst"),
  "/panel/api/agents/card-tst?node=collab-tst",
);
assert.equal(
  panel.agentEndpoint("card-tst", "/persona", "collab-tst"),
  "/panel/api/agents/card-tst/persona?node=collab-tst",
);

// 只是「见过」的节点也该出现在拓扑里，好让人知道它可以配对
panel.applyCluster({
  node: "laptop",
  nodes: [
    { node: "laptop", local: true, reachable: true, agents: [] },
    {
      node: "陌生的",
      local: false,
      reachable: false,
      pairable: true,
      reason: "见过，还没配对",
      agents: [],
    },
  ],
});
assert.ok($("topo-body").innerHTML.includes("陌生的"), "见过但没配对的节点没显示出来");
panel.applyCluster(CLUSTER);

// 页面可能先于 serve 重启：旧后端没有 current_prompt，但已有 attach 配方足够
// 让浏览器组成可直接发给当前 Codex 的提示，不能给用户留一个空复制框。
panel.renderConnect({
  connect: { codex: { attach: "anthill codex cc --attach THREAD_ID -w /workspace" } },
  claims: [],
});
const recipes = $("bridge-recipes").innerHTML;
assert.ok(recipes.includes('data-provider="codex"'), "Codex 接入说明没有独立折叠区");
assert.ok(recipes.includes('data-provider="claude"'), "Claude Code 接入说明没有独立折叠区");
assert.ok(!recipes.includes('data-provider="codex" open'), "Codex 接入说明不应默认展开");
assert.ok(!recipes.includes('data-provider="claude" open'), "Claude Code 接入说明不应默认展开");
const currentPrompt = $("bridge-recipes").innerHTML.match(
  /id="rcp-codex-current">([\s\S]*?)<\/pre>/,
)[1];
assert.ok(currentPrompt.includes("--attach current"), "旧 serve 下没有生成当前会话接入提示词");
assert.ok(currentPrompt.includes("CODEX_THREAD_ID"), "接入提示没有先检查当前 thread 环境");
assert.ok(currentPrompt.includes("CODEX_HOME"), "接入提示没有解释 app-server 的状态目录权限");
assert.ok(currentPrompt.includes("一次性沙箱外执行"), "接入提示仍会让 Codex 在只读沙箱里直接试跑");
await panel.copyText("完整的接入提示词");
assert.equal(fallbackCopied, "完整的接入提示词", "Clipboard API 拒绝后没有走兼容复制");

// ---- 选中的工作区展开，其余一律折叠成一行 ----
//
// 本机的、远端的都一样：不折叠的话，照看两个工作区再连几台机器，
// 一屏全是别处的 Agent 卡片，正在操作的那个反而要翻着找。
const FOLD_CLUSTER = {
  node: "laptop",
  nodes: [
    { node: "laptop", local: true, reachable: true, agents: [agent("cli")], runs: [], events: [] },
    { node: "desk", local: true, reachable: true, agents: [agent("deskbot")], runs: [], events: [] },
    { node: "lab", local: false, reachable: true, agents: [agent("labbot")], runs: [], events: [] },
    {
      node: "server",
      local: false,
      reachable: false,
      reason: "ConnectError: nobody home",
      agents: [],
      runs: [],
      events: [],
    },
  ],
};
panel.topoOpen.clear();
panel.local.node = "";
panel.applyCluster(FOLD_CLUSTER);
let fold = $("topo-body").innerHTML;
// 谁都没点过的时候，**主节点也折着** —— 「选中谁展开谁」的另一半是
// 「没选中就都别展开」：默认焦点是服务端的主节点，不是人选的
assert.ok(!fold.includes(">cli<"), "没人选过时主节点不该自动展开");
assert.ok(fold.includes("正在操作"), "主节点折着也得标出它是正在操作的那个");
assert.ok(!fold.includes("deskbot"), "没在操作的本机工作区应当折叠");
assert.ok(!fold.includes("labbot"), "远端节点默认应当折叠");
assert.ok(fold.includes("nobody home"), "折叠的节点连不上时，原因至少要在悬停提示里");

// 点过（选中）之后才展开
panel.local.node = "laptop";
panel.draw();
fold = $("topo-body").innerHTML;
assert.ok(fold.includes("cli"), "选中的工作区没展开");
panel.local.node = "";
panel.draw();
fold = $("topo-body").innerHTML;

// 可点的头部得让键盘也够得着：能 Tab 到、读屏认得出是按钮、报告开合状态
assert.ok(
  /data-toggle-node="lab"[^>]*tabindex="0"/.test(fold) ||
    /tabindex="0"[^>]*data-toggle-node="lab"/.test(fold),
  "可点的头部 Tab 不到",
);
assert.ok(fold.includes('role="button"'), "可点的头部读屏认不出是按钮");
assert.ok(fold.includes('aria-expanded="false"'), "折叠状态没报告给辅助技术");
assert.ok(
  /class="fold"[^>]*aria-hidden="true"|aria-hidden="true"[^>]*class="fold"/.test(fold),
  "▸/▾ 箭头对读屏是纯噪音，得藏起来",
);

// 点远端节点的头部 = 扒开看一眼，跨重画要记住
panel.topoOpen.add("lab");
panel.draw();
fold = $("topo-body").innerHTML;
assert.ok(fold.includes("labbot"), "手动扒开的远端节点没展开");
assert.ok(fold.includes('aria-expanded="true"'), "展开状态没报告给辅助技术");

// 切到另一个工作区：它展开，原来那个折叠
panel.topoOpen.clear();
panel.local.node = "desk";
panel.draw();
fold = $("topo-body").innerHTML;
assert.ok(fold.includes("deskbot"), "切过去的工作区没展开");
assert.ok(!fold.includes(">cli<"), "切走之后原来的工作区应当折叠");

// 存档的选择指向已消失的工作区：一次**全量**快照后必须清掉 ——
// 不清的话 scoped() 会把它挂到每个 ?node= 请求上，服务端全线 404，
// 而顶栏显示的还是主节点，人毫无线索
panel.local.node = "ghost-of-removed";
panel.applyCluster(FOLD_CLUSTER);
assert.equal(panel.local.node, "", "陈旧的工作区选择没被清掉");
assert.ok(
  $("topo-body").innerHTML.includes("正在操作"),
  "清掉陈旧选择后主节点该拿回「正在操作」标签",
);
// WS 快照只带主节点 —— 在那儿校验会把合法的选择误清掉
panel.local.node = "desk";
panel.applyLocal({ node: "laptop", agents: [], runs: [], events: [] });
assert.equal(panel.local.node, "desk", "WS 快照不该把合法的工作区选择误清");

panel.topoOpen.clear();
panel.local.node = "";
panel.applyCluster(CLUSTER);

// 跨零点：事件按完整时间戳排，不能按显示用的 HH:MM:SS
panel.applyCluster({
  node: "x",
  nodes: [
    {
      node: "a",
      local: true,
      reachable: true,
      agents: [],
      runs: [],
      events: [
        { sort: "2026-08-04T23:59:59", ts: "23:59:59", event: "昨天最后一条" },
        { sort: "2026-08-05T00:00:07", ts: "00:00:07", event: "今天第一条" },
      ],
    },
  ],
});
const ordered = $("stream-body").innerHTML;
assert.ok(
  ordered.indexOf("昨天最后一条") < ordered.indexOf("今天第一条"),
  "跨零点后新事件被排到了前面 —— 再截断就会把它们丢掉",
);

// XSS：整份 payload 都来自对端的 status.json，每一个插值点都是外部数据
const EVIL = "<img src=x onerror=alert(1)>";
panel.setWrite(true);
panel.applyCluster({
  node: "role-box",
  nodes: [{
    node: "role-box", local: true, reachable: true,
    agents: [{ ...agent("reviewer"), has_persona: true }], runs: [], events: [],
  }],
});
const roleTopo = $("topo-body").innerHTML;
assert.ok(roleTopo.includes('data-op="persona"'), "现有 Agent 没有角色卡入口");
assert.ok(roleTopo.includes('id="agent-persona"'), "新建 Agent 不能填写角色卡");
assert.ok(roleTopo.includes("角色卡 ✓"), "已配置角色卡的 Agent 没有明确状态");
assert.ok(roleTopo.includes('class="card-persona configured"'), "角色卡状态没有落在稳定的轻量入口上");
assert.match(
  roleTopo,
  /<div class="detail">[\s\S]*data-op="persona"[\s\S]*<\/div>/,
  "角色卡入口没有放在元信息行，会重新挤乱 Agent 名称和运行状态",
);

panel.applyCluster({
  node: "x",
  nodes: [
    {
      // 连得上的那台：Agent 卡片只在 reachable 时才画。
      // local:true 还会多画启停按钮那一条分支，把 Agent 名拼进 data 属性
      node: EVIL,
      local: true,
      reachable: true,
      agents: [
        {
          name: EVIL,
          role: EVIL,
          provider: EVIL,
          watch_mode: EVIL,
          running: true,
          queue: EVIL,
          dead: EVIL,
        },
      ],
      runs: [
        {
          goal: EVIL,
          round: EVIL,
          finished: false,
          steps: [{ id: EVIL, assignee: EVIL, state: EVIL, detail: EVIL }],
        },
      ],
      events: [{ ts: EVIL, sort: EVIL, agent: EVIL, event: EVIL, level: EVIL, detail: EVIL }],
    },
    // 连不上的那台：走的是另一条渲染分支（只画 reason）
    { node: `${EVIL}2`, local: false, reachable: false, reason: EVIL, agents: [] },
  ],
});
for (const pane of ["topo-body", "runs-body", "stream-body"]) {
  assert.ok(!$(pane).innerHTML.includes("<img"), `${pane} 里有没转义的对端数据`);
}

// 工作区清单里的路径与节点名同样是外部数据（磁盘上的目录名、别人写的 node.toml）
panel.renderWorkspaces({
  ready: true,
  node: EVIL,
  home: "/tmp",
  workspaces: [{ name: EVIL, path: EVIL, node: EVIL, exists: true, current: false }],
});
assert.ok(!$("ws-list").innerHTML.includes("<img"), "工作区清单里有没转义的数据");
assert.ok(
  $("ws-list").innerHTML.includes("data-ws-row="),
  "工作区行少了 data-ws-row 身份 —— 行内报错要靠它找到出错的那一行",
);

// ---- 对话按工作区分组：一台机器好几个工作区，各家的 cli/echo 重名，
// 不标工作区的话一屏「cli ↔ echo」根本分不清是谁家的 ----
const msg = (frm, body) => ({ frm, body, ts: "" });
panel.chat.data = {
  threads: [
    {
      thread: "t1", short: "T1", ws: "alpha-ws", count: 1, last: "",
      peers: ["alpha-ws:cli", "alpha-ws:echo"],
      messages: [msg("alpha-ws:echo", "自家的")],
    },
    {
      thread: "t2", short: "T2", ws: "beta-ws", count: 1, last: "",
      peers: ["beta-ws:cli", "alpha-ws:tst9"],
      messages: [msg("alpha-ws:tst9", "跨工作区来的")],
    },
  ],
};
panel.renderChat();
let convo = $("chat-body").innerHTML;
assert.ok(convo.includes('class="chat-ws"'), "多工作区时对话没按工作区分组");
assert.ok(convo.includes("alpha-ws") && convo.includes("beta-ws"), "分组标题少了工作区名");
assert.ok(!convo.includes("alpha-ws:echo"), "同工作区的 agent 该显示短名");
assert.ok(convo.includes("alpha-ws:tst9"), "跨工作区的 agent 必须带完整身份，不然重名分不清");

// 只有一个工作区时不摆分组标题 —— 单机用户的页面不该多出一层没意义的壳
panel.chat.data.threads.forEach((t) => { t.ws = "solo"; });
panel.renderChat();
assert.ok(!$("chat-body").innerHTML.includes('class="chat-ws"'), "单工作区不该出分组标题");

// 工作区名也是外部数据（目录名、node.toml），进 HTML 前要转义
panel.chat.data = {
  threads: [{
    thread: "t3", short: "T3", ws: EVIL, count: 1, last: "",
    peers: [`${EVIL}:cli`, "x:y"], messages: [msg(`${EVIL}:cli`, EVIL)],
  }, {
    thread: "t4", short: "T4", ws: "别家", count: 1, last: "", peers: ["别家:cli"], messages: [],
  }],
};
panel.renderChat();
assert.ok(!$("chat-body").innerHTML.includes("<img"), "对话分组里有没转义的工作区名");
// 一页空对话 + 一家读不到：不能静默 —— 段数栏平时只在有对话时才说话，
// 空的时候更要把「有一家没读出来」说出口，不然人以为真的什么都没有
panel.chat.data = { threads: [], broken: ["坏了的家"] };
panel.renderChat();
assert.ok(
  $("chat-count").textContent.includes("坏了的家"),
  "空对话时读不到的工作区被静默了",
);

// ---- 迷你 Markdown：先转义再织标签 —— 恶意消息永远进不了 DOM ----
assert.ok(!panel.md(EVIL).includes("<img"), "md 没把恶意 HTML 转义掉");
assert.ok(panel.md("**加粗**").includes("<b>加粗</b>"), "粗体没渲染");
assert.ok(panel.md("`x < 1`").includes("<code>x &lt; 1</code>"), "行内代码没渲染或没转义");
const fenced = panel.md("```py\nif a<b: pass\n```");
assert.ok(fenced.includes('class="md-code"') && fenced.includes("a&lt;b"), "代码块没渲染或没转义");
assert.ok(panel.md("- 甲\n- 乙").includes("<ul><li>甲</li><li>乙</li></ul>"), "无序清单没渲染");
assert.ok(panel.md("1. 甲\n2. 乙").includes("<ol>"), "有序清单没渲染");
assert.ok(panel.md("[说明](https://a.b/c)").includes('href="https://a.b/c"'), "http 链接没渲染");
assert.ok(!panel.md("[x](javascript:alert(1))").includes("<a"), "非 http(s) 链接不该成为 <a>");
assert.ok(panel.md("> 引用行").includes("<blockquote>"), "引用没渲染");
assert.ok(panel.md("`**a**` 和 **b**").includes("<code>**a**</code>"),
  "行内代码里的星号被当成了粗体 —— 代码该先摘走再处理其余语法");
// 占位符不能被正文伪造：NUL 包着序号是 mdInline 的内部记号，正文里带字面 NUL
// （JSON 里合法）就能冒充它，渲染出 undefined 或错位复制同条消息里的 code
assert.ok(!panel.md("\u00000\u0000").includes("undefined"), "正文伪造的占位符没被剥掉");
assert.ok(panel.md("`真代码` 与 \u00000\u0000").includes("<code>真代码</code>"),
  "剥 NUL 不该把真的行内代码一起弄坏");

// ---- 详情窗格：消息经 md 渲染，恶意正文照样进不了 DOM ----
panel.chat.data = {
  threads: [
    {
      thread: "t1", short: "T1", ws: "alpha-ws", count: 1, last: "",
      peers: ["alpha-ws:cli", "alpha-ws:echo"],
      messages: [msg("alpha-ws:echo", EVIL)],
    },
    {
      thread: "t2", short: "T2", ws: "beta-ws", count: 1, last: "",
      peers: ["beta-ws:cli", "beta-ws:tst9"],
      messages: [msg("beta-ws:tst9", "乙家的话")],
    },
  ],
};
panel.chat.thread = "t1";
panel.renderChat();
assert.ok(!$("chat-msgs").innerHTML.includes("<img"), "详情消息里有没转义的正文");
assert.ok($("chat-msgs").innerHTML.includes('class="msg them"'), "详情没按气泡渲染");

// ---- 工作区 / Agent 筛选：筛掉的段不该再出现在清单里 ----
panel.chat.ws = "beta-ws";
panel.renderChat();
let listed = $("chat-body").innerHTML;
assert.ok(listed.includes("tst9") && !listed.includes("echo"), "工作区筛选没生效");
panel.chat.ws = "";
panel.chat.agent = "alpha-ws:echo";
panel.renderChat();
listed = $("chat-body").innerHTML;
assert.ok(listed.includes("echo") && !listed.includes("tst9"), "Agent 筛选没生效");
panel.chat.agent = "";
panel.chat.thread = "";

// ---- 任务看板：交付说明是 Markdown，别把标记当字面量摊给人看 ----
//
// 步骤的 detail 动辄两三千字，带标题、代码块、表格。以前直接 esc() 塞进
// 一行截断，于是看板上是一串字面量 `**` `##` `|---|` 挤在一起还被切一半。
const DELIVERY = [
  "**先纠正一个前提：**这个 bug 上一轮已经修好了。",
  "",
  "## 三档深度实测",
  "",
  "| 深度 | exit |",
  "|---|---|",
  "| 246 | `2` |",
  "",
  "```",
  "74 passed in 3.28s",
  "```",
  "",
  "## 本步所属目标",
  "带领 tst1、tst2 协作做出一个计算器",
  "",
  "## 你的产物放这里",
  "blackboard://tasks/01ABC/",
].join("\n");

const preview = panel.plainPreview(DELIVERY);
assert.ok(!preview.includes("**"), `摘要里还有粗体标记：${preview}`);
assert.ok(!preview.includes("##"), `摘要里还有标题标记：${preview}`);
assert.ok(!preview.includes("|---|"), `摘要里还有表格分隔行：${preview}`);
assert.ok(!preview.includes("74 passed"), "代码块该整段拿掉，一行摘要放不下");
assert.ok(preview.includes("先纠正一个前提"), `摘要丢了正文：${preview}`);
// 脚手架是 coordinator 给**执行者**看的，每一步都一样 —— 摘要里是纯噪音
assert.ok(!preview.includes("本步所属目标"), "摘要里混进了脚手架段落");
assert.ok(!preview.includes("blackboard://"), "摘要里混进了脚手架段落");

// `_` **不能**当强调标记剥掉：这个项目的交付说明里 snake_case 标识符遍地都是，
// 而 `_斜体_` 几乎没人写。实测真实语料曾经损坏三处（`test_calc` → `testcalc`、
// `MAX_DEPTH` → `MAXDEPTH`），而标识符恰恰是扫看板时最需要认出来的东西。
// 同一条理由让 md() 故意不支持单星斜体（`*.py` 会被误伤）——
// **渲染器不认的语法，摘要不该替它剥。**
const idents = panel.plainPreview("改了 test_calc.py，把 MAX_DEPTH 提到 100。");
assert.ok(idents.includes("test_calc.py"), `标识符被剥坏了：${idents}`);
assert.ok(idents.includes("MAX_DEPTH"), `标识符被剥坏了：${idents}`);

// 全代码块 / 全表格的交付**不能得到空摘要** —— 空摘要会让看板显示
// 「（没有交付说明）」，那是句假话：说明明明在，旁边还摆着展开箭头。
// 而这两种形态恰恰最常见：pytest 全绿输出是一整个代码块，验收报告是表格密集型。
const onlyCode = panel.plainPreview("```\npytest -q\n74 passed in 3.28s\n```");
assert.ok(onlyCode.trim(), "纯代码块的交付得到了空摘要");
const onlyTable = panel.plainPreview("| 深度 | exit |\n|---|---|\n| 246 | 2 |");
assert.ok(onlyTable.trim(), "纯表格的交付得到了空摘要");

// 表格分隔行 `|---|---|` 是纯格式产物，对人零信息 —— 渲染时丢掉
assert.ok(
  !panel.md("| a | b |\n|---|---|\n| 1 | 2 |").includes("---"),
  "展开区里还留着表格分隔行",
);

const stripped = panel.stripScaffold(DELIVERY);
assert.ok(stripped.includes("三档深度实测"), "正文被砍多了");
assert.ok(!stripped.includes("你的产物放这里"), "脚手架没砍干净");
// 完整正文走 md() 正经渲染 —— 这是「要么渲染它、要么别把标记拿给人看」的另一半
const rendered = panel.md(stripped);
assert.ok(rendered.includes('class="md-h"'), "展开区没把标题渲染出来");
assert.ok(rendered.includes('class="md-code"'), "展开区没把代码块渲染出来");

panel.chat.data = { threads: [] };
panel.renderChat();

// ---- 配置页：文本手术 + git 式 diff ----
// 图形字段改的是**原文**（注释和未知节区原样保留），保存前一律过 diff 关
const TOML = '[node]\nname = "x"\n\n[runtime]\npoll_interval = 0.5\n';
assert.ok(panel.patchToml(TOML, "runtime", "poll_interval", "2").includes("poll_interval = 2"));
assert.ok(!panel.patchToml(TOML, "runtime", "poll_interval", "2").includes("0.5"));
const inserted = panel.patchToml(TOML, "node", "endpoint", '"http://a:1"');
assert.ok(inserted.includes('endpoint = "http://a:1"') && inserted.indexOf("endpoint") < inserted.indexOf("[runtime]"),
  "缺的 key 该插进对应节区，不是文件末尾");
assert.ok(panel.patchToml('[node]\nname = "x"\n', "runtime", "watch_mode", '"poll"').includes("[runtime]"),
  "缺的节区要整个补出来");
assert.ok(!panel.patchToml('[node]\nname = "x"\nendpoint = "e"\n', "node", "endpoint", "").includes("endpoint"),
  "空值 = 删掉那一行");
assert.ok(panel.patchToml(TOML, "node", "name", '"y"').includes("# 注释也不能丢") === false, "冒烟");

const d = panel.diffLines("a\nb\nc", "a\nX\nc");
assert.deepEqual(d.map((x) => x.type), [" ", "-", "+", " "], "diff 分类不对");
assert.ok(!panel.renderDiff([{ type: "-", line: EVIL }]).includes("<img"), "diff 行没转义");
assert.ok(panel.renderDiff(panel.diffLines("a", "b")).includes("diff"), "diff 容器该在");

console.log("ok");
