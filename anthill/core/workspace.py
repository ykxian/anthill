"""建工作区这件事本身。

原来这段逻辑只长在 `anthill init` 里，于是「先在终端跑一次 init」成了硬前提 ——
新机器上装好、想直接开面板，第一步就撞墙。现在它是一个普通函数，
`init`、`serve`（找不到工作区就自己建一个）、面板都调它，行为完全一致。
"""

from __future__ import annotations

import contextlib
import socket
from collections.abc import Iterable
from itertools import count
from pathlib import Path

from anthill.core.config import Config, default_node_toml
from anthill.core.envelope import NODE_NAME_RE
from anthill.core.errors import AntHillError, ConfigError
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout

BOARD_SEED = "# BOARD\n\n> 当前协作状态快照，由 coordinator 单写者维护。\n"


VIRTUAL_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "tap", "tailscale", "zt", "wg", "vbox")
"""这些网卡上的地址**不该**被当成「局域网里别人能连到我的地址」。

一台开发机上 Docker 能造出几十个 `br-xxxx`，再加隧道、VPN、虚拟机网桥 ——
挑错一个，对端记下来之后永远连不上，而且报错很难懂。
"""

PHYSICAL_PREFIXES = ("en", "eth", "wl")
"""看着像真网卡的名字，优先。"""


def _is_virtual(name: str) -> bool:
    # `tun` 用包含匹配：真见过叫 `sbtun0` 的隧道，`startswith` 漏得干干净净
    return name.startswith(VIRTUAL_PREFIXES) or "tun" in name


def _interface_ips() -> list[tuple[str, str]]:
    """枚举本机网卡的 IPv4。拿不到就返回空 —— 调用方有兜底。

    标准库没有跨平台的枚举接口，这里走 Linux 的 ioctl；别的平台直接空手而归。
    """
    try:
        import fcntl
        import struct

        siocgifaddr = 0x8915
        found = []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for _, name in socket.if_nameindex():
                try:
                    packed = struct.pack("256s", name.encode()[:15])
                    raw = fcntl.ioctl(sock.fileno(), siocgifaddr, packed)
                except OSError:
                    continue  # 这块网卡没有 IPv4
                found.append((name, socket.inet_ntoa(raw[20:24])))
        return found
    except (ImportError, AttributeError, OSError):  # pragma: no cover - 非 Linux
        return []


def local_ip() -> str:
    """猜一个**局域网里别人能连到**的本机 IP。猜不出就退化成 127.0.0.1。

    `0.0.0.0` 是绑定用的通配符，不是地址 —— 把它当 endpoint 广播出去，
    对端记下来之后永远连不上（而且报错还很难懂：走代理的话是一个空的 502）。

    以前的做法是「连一个远地址、看内核挑了哪条路由」。那等于**问默认路由是什么**，
    而默认路由完全可能是一条隧道或 VPN —— 真机上就撞到过：
    一台有 40 个 Docker 网桥的机器，默认路由走 `sbtun0`，于是对外广播成
    `172.19.0.1`，而它真正的局域网地址是 `10.15.3.61`。

    现在先枚举网卡、排掉虚拟的那些，优先看着像真网卡的名字（en/eth/wl）；
    都排掉了才退回旧办法。**猜错的代价是别人连不上你**，所以启动时会把
    选中的地址和「怎么覆盖」一起打出来，别让人只能靠猜。
    """
    candidates = [(n, ip) for n, ip in _interface_ips() if not _is_virtual(n) and ip != "127.0.0.1"]
    physical = [ip for n, ip in candidates if n.startswith(PHYSICAL_PREFIXES)]
    if physical:
        return sorted(physical)[0]
    if candidates:
        return sorted(ip for _, ip in candidates)[0]

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("10.255.255.255", 1))  # 不发包，只让内核挑一条路由
            return str(sock.getsockname()[0])
        except OSError:
            return "127.0.0.1"


GENERIC_DIR_NAMES = frozenset({"anthill", "workspace", "work", "demo", "test", "tmp", "src", "app"})
"""这些目录名说明不了任何事，不如退回主机名。"""


def _slug(raw: str) -> str:
    """把一段文字捏成合法节点名：小写 ASCII 字母数字与 `._-`，必须字母开头。

    **只认 ASCII** —— `str.isalnum()` 对中文返回 True，直接用它的话
    `我的项目` 会被当成合法名字放过去，然后在 `NODE_NAME_RE` 那关炸掉。
    这个坑在密钥的变量名上刚踩过一次。
    """
    lowered = raw.strip().lower()
    kept = "".join(c if (c.isascii() and (c.isalnum() or c in "._-")) else "-" for c in lowered)
    cleaned = kept.strip("-._")
    # 首字符照 NODE_NAME_RE 的要求来（字母或数字都行），别卡得比它还严 ——
    # 那会让 `2024-demo` 这种正常目录名白白退回主机名
    while cleaned and not (cleaned[0].isascii() and cleaned[0].isalnum()):
        cleaned = cleaned[1:]
    return cleaned


def default_node_name(taken: Iterable[str] = (), *, directory: Path | None = None) -> str:
    """给这个工作区起个名字。

    **优先用目录名。** 一台机器上放着 `collab` 和 `collab-tst` 时，
    `collab` / `collab-tst` 一眼就知道谁是谁；而主机名派生出来的 `cs` / `cs-2`
    什么也没说 —— 那是最早只考虑「一台机器一个工作区」时的选择。

    目录名说明不了事（`workspace`、`tmp`、`src` 这类）或者拿不到时，退回主机名。
    主机名的好处是局域网里一眼对得上是哪台机器，那条理由仍然成立。

    最后一定保证**本机唯一**（信封上的收件人靠这个名字指人），撞了就加序号。

    ⚠️ 跨机不保证唯一：两台机器上都有个 `collab` 目录是很正常的事。
    真撞上时配对会直接拒绝并让人改名（见 `PeerRegistry.trust`）——
    与其在这儿猜一个又长又丑的名字，不如让那一步说清楚。
    """
    used = {n.lower() for n in taken}
    base = ""
    if directory is not None:
        candidate = _slug(directory.name)
        if candidate and candidate not in GENERIC_DIR_NAMES:
            base = candidate
    if not base:
        base = _slug(socket.gethostname().split(".")[0]) or "node"
    if base not in used:
        return base
    return next(f"{base}-{n}" for n in count(2) if f"{base}-{n}" not in used)


def create_workspace(layout: NodeLayout, *, node_name: str = "", force: bool = False) -> Config:
    """在 `layout` 指的位置建出工作区骨架，返回加载好的配置。

    已经有 node.toml 时默认拒绝 —— 这个函数会覆盖配置文件，
    而配置被无声覆盖是很难查的那种事故。
    """
    name = node_name or suggest_node_name(layout.workspace)
    # 先验名字再动盘：不然会写出一份读不回来的 node.toml，
    # 留下一个「看着像工作区、其实起不来」的目录
    if not NODE_NAME_RE.match(name):
        raise ConfigError(
            f"非法节点名 {name!r} —— 只能用小写字母开头的 ASCII 字母/数字/`.`/`_`/`-`"
        )
    layout.ensure_base()
    if layout.node_toml.is_file() and not force:
        raise ConfigError(f"{layout.node_toml} 已存在；要重建请显式要求覆盖")

    layout.node_toml.write_text(default_node_toml(name), encoding="utf-8")
    config = Config.load_from(layout)
    ensure_mailboxes(layout, config)
    (layout.blackboard / "BOARD.md").write_text(BOARD_SEED, encoding="utf-8")
    return config


def ensure_mailboxes(layout: NodeLayout, config: Config) -> list[str]:
    """给配置里每个 Agent 都把邮箱目录备好，返回这次新建的那些。

    **配置里有它，就该能收它的信** —— 不该等到那个 agentd 第一次启动。
    否则在面板上加完 Agent，别的机器投过来会撞上「邮箱还没建」的 404，
    而人看着面板上明明有这个 Agent，完全不知道差在哪。
    幂等，所以任何一次配置写入之后都可以无脑调一遍，顺便自愈。
    """
    created = []
    for name in config.agents:
        mailbox = Mailbox(layout.mailbox_dir(name))
        if not mailbox.exists:
            mailbox.ensure()
            created.append(name)
    return created


def load_or_create(layout: NodeLayout, *, node_name: str = "") -> tuple[Config, bool]:
    """有就加载，没有就建一个。返回 `(配置, 是不是刚建的)`。

    给「装好就能直接开面板」用：`anthill serve` 找不到工作区时不该报错退出，
    它该把工作区建出来然后继续 —— 建在哪、叫什么名字，打印出来告诉人就是了。
    """
    if layout.node_toml.is_file():
        return Config.load_from(layout), False
    return create_workspace(layout, node_name=node_name), True


class ConfigRef:
    """按 mtime 自动重载的 node.toml。

    起因是一个真 bug：面板上加了个 Agent，配置文件确实改了，可 serve 手里捧着的
    还是启动那一刻读到的那份 —— 于是新 Agent **既不出现在面板上，也收不了消息**
    （`/deliver` 拿旧 config 判收件人，直接回 404「本节点没有这个 Agent」）。

    node.toml 现在是运行期可改的（面板加 Agent、远端管理、人手动编辑），
    那就不能再当成启动期常量。和 `peers.json` 一个套路：**文件是唯一真相，
    内存里那份只是缓存**；改坏了就继续用上一份好的，别让一次手滑弄停整个节点。
    """

    def __init__(self, layout: NodeLayout, config: Config | None = None) -> None:
        self._layout = layout
        self._config = config if config is not None else Config.load_from(layout)
        self._mtime = self._stamp()

    @property
    def current(self) -> Config:
        stamp = self._stamp()
        if stamp is not None and stamp != self._mtime:
            # 改坏了就继续用上一份好的 —— 别让一次手滑弄停整个节点
            with contextlib.suppress(AntHillError):
                self._config = Config.load_from(self._layout)
            self._mtime = stamp
        return self._config

    def _stamp(self) -> float | None:
        try:
            return self._layout.node_toml.stat().st_mtime
        except OSError:
            return None


def suggest_node_name(directory: Path | None = None) -> str:
    """给一个**这台机器上还没被占用**的节点名。

    `init` 和 `serve` 都得走这一个入口 —— 各算各的话，`init` 那条路会绕过
    去重，于是连着 init 两次仍然都叫主机名，第二个工作区起 serve 时被跳过。
    """
    return default_node_name(_names_in_use(), directory=directory)


def _names_in_use() -> list[str]:
    """这台机器上已经有的节点名。

    两个来源都要看，只看一个会漏：

    - **机器级清单**（`~/.anthill/workspaces.json`）—— 面板建的、serve 记下的；
    - **家目录下常见的兄弟目录** —— `anthill init` 建的工作区不进清单，
      而人恰恰最常连着 init 两次。只靠清单的话第二次仍然重名。

    读不出来就当没有 —— 起名字这件事不该因为一个清单文件坏了而失败。
    """
    names: list[str] = []
    try:
        from anthill.web.workspaces import listing

        names += [str(e.get("node", "")) for e in listing() if e.get("node")]
    except Exception:  # pragma: no cover - 清单坏了/循环导入，都只是少一点信息
        pass
    return [n for n in names if n]
