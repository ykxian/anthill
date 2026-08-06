"""对外广播哪个地址 —— 多网卡机器上这一步真的会挑错。

实测现场：一台开发机有约 40 个网卡（Docker 造的 `br-*`、`docker0`，
加隧道 `sbtun0`、VPN `tailscale0`、ZeroTier `ztukuqgywh`），
而它真正的局域网地址在 `eno1` 上。

旧做法是「连一个远地址，看内核挑了哪条路由」—— 那等于**问默认路由是什么**，
而那台机器的默认路由走隧道，于是对外广播成 `172.19.0.1`，
局域网里谁都连不上它（真正的地址是 `10.15.3.61`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.workspace import PHYSICAL_PREFIXES, _is_virtual, local_ip


@pytest.mark.parametrize(
    "name",
    [
        "lo",
        "docker0",
        "br-ec82e7eaa590",
        "veth1a2b3c",
        "virbr0",
        "tailscale0",
        "ztukuqgywh",
        "wg0",
        "tun0",
        "sbtun0",  # `startswith("tun")` 漏掉的那个 —— 真机上就叫这名字
    ],
)
def test_virtual_interfaces_are_not_advertised(name: str) -> None:
    assert _is_virtual(name)


@pytest.mark.parametrize("name", ["eno1", "eth0", "enp3s0", "wlan0", "wlp2s0"])
def test_real_interfaces_are_kept(name: str) -> None:
    assert not _is_virtual(name)
    assert name.startswith(PHYSICAL_PREFIXES)


def test_a_physical_interface_wins_over_a_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    """这就是那台机器的现场：默认路由在隧道上，但该广播的是 eno1。"""
    monkeypatch.setattr(
        "anthill.core.workspace._interface_ips",
        lambda: [
            ("lo", "127.0.0.1"),
            ("sbtun0", "172.19.0.1"),
            ("docker0", "172.17.0.1"),
            ("br-ec82e7eaa590", "172.19.0.1"),
            ("tailscale0", "100.69.224.78"),
            ("eno1", "10.15.3.61"),
        ],
    )

    assert local_ip() == "10.15.3.61"


def test_it_falls_back_when_nothing_looks_physical(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有像样网卡时也得给个答案 —— 剩下的里挑一个，总比 0.0.0.0 强。"""
    monkeypatch.setattr(
        "anthill.core.workspace._interface_ips",
        lambda: [("lo", "127.0.0.1"), ("bond0", "192.168.5.7")],
    )

    assert local_ip() == "192.168.5.7"


def test_no_interfaces_at_all_still_returns_something(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("anthill.core.workspace._interface_ips", list)

    assert local_ip()  # 走回旧的路由探测；连不上就是 127.0.0.1


def test_it_never_returns_the_bind_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    """`0.0.0.0` 是绑定用的通配符，不是地址 —— 广播出去对端永远连不上。"""
    monkeypatch.setattr("anthill.core.workspace._interface_ips", lambda: [("eno1", "10.15.3.61")])

    assert local_ip() != "0.0.0.0"


# ---------- 一台机器上多个工作区 ----------


def test_a_second_workspace_gets_a_distinct_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """一台机器上可以有好几个工作区，而节点名必须唯一（信封上的收件人靠它指人）。

    全都叫主机名的话，第二个工作区起 serve 时就被跳过，还只给一句
    「已经有一个叫 cs 的节点了」—— 用户既没做错什么，也不知道该怎么办。
    """
    from anthill.core.workspace import default_node_name

    monkeypatch.setattr("socket.gethostname", lambda: "cs")

    assert default_node_name() == "cs"
    assert default_node_name(["cs"]) == "cs-2"
    assert default_node_name(["cs", "cs-2"]) == "cs-3"
    assert default_node_name(["CS"]) == "cs-2", "比较得忽略大小写"


def test_a_weird_hostname_still_yields_a_legal_node_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from anthill.core.workspace import NODE_NAME_RE, default_node_name

    monkeypatch.setattr("socket.gethostname", lambda: "My Box！.lan")

    assert NODE_NAME_RE.match(default_node_name())


# ---------- 节点名跟着目录走 ----------


def test_the_directory_name_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """一台机器上放着 collab 和 collab-tst 时，`collab` / `collab-tst` 一眼看得懂；
    主机名派生出来的 `cs` / `cs-2` 什么也没说。"""
    from anthill.core.workspace import default_node_name

    monkeypatch.setattr("socket.gethostname", lambda: "cs")

    assert default_node_name(directory=Path("/x/collab")) == "collab"
    assert default_node_name(directory=Path("/x/collab-tst")) == "collab-tst"


@pytest.mark.parametrize("generic", ["workspace", "tmp", "src", "demo", "app"])
def test_a_generic_directory_name_falls_back_to_the_hostname(
    generic: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`workspace` 这种目录名说明不了任何事，不如退回主机名 ——
    主机名至少能在局域网里对上是哪台机器。"""
    from anthill.core.workspace import default_node_name

    monkeypatch.setattr("socket.gethostname", lambda: "cs")

    assert default_node_name(directory=Path("/x") / generic) == "cs"


@pytest.mark.parametrize(
    ("dirname", "expected"),
    [
        ("My Proj!", "my-proj"),
        ("2024-demo", "2024-demo"),  # NODE_NAME_RE 允许数字开头，别卡得比它还严
        ("我的项目", "cs"),  # 非 ASCII 整个作废，退回主机名
        (".hidden", "hidden"),
    ],
)
def test_odd_directory_names_still_yield_legal_node_names(
    dirname: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`str.isalnum()` 对中文返回 True —— 直接用它的话 `我的项目` 会被放过去，
    然后在 NODE_NAME_RE 那关炸掉。这个坑在密钥的变量名上刚踩过一次。"""
    from anthill.core.envelope import NODE_NAME_RE
    from anthill.core.workspace import default_node_name

    monkeypatch.setattr("socket.gethostname", lambda: "cs")
    name = default_node_name(directory=Path("/x") / dirname)

    assert name == expected
    assert NODE_NAME_RE.match(name)


def test_two_workspaces_with_the_same_directory_name_still_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本机唯一是硬要求 —— 信封上的收件人靠这个名字指人。"""
    from anthill.core.workspace import default_node_name

    monkeypatch.setattr("socket.gethostname", lambda: "cs")

    assert default_node_name(["collab"], directory=Path("/a/collab")) == "collab-2"
