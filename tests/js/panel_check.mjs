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

const script = readFileSync(process.argv[2], "utf8").match(/<script>([\s\S]*?)<\/script>/)[1];

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
const document = {
  getElementById: $,
  title: "",
  querySelectorAll: () => [],
  addEventListener() {},
};
const window = { prompt: () => null, alert() {}, confirm: () => false };
const location = { protocol: "http:", host: "panel.test", pathname: "/panel" };
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
  "location",
  "fetch",
  "WebSocket",
  "setInterval",
  "setTimeout",
  // setWrite：写权限开着时拓扑会多画启停/删除按钮和「加 Agent」表单，
  // 那几处也把外部数据拼进了 HTML，必须一起验
  `${script}\nreturn { state, applyCluster, applyLocal, setWrite: (v) => { canWrite = v; } };`,
)(document, window, location, fetch, WebSocket, noop, noop);

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

console.log("ok");
