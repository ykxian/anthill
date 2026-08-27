"""Known-good command-agent presets.

The command adapter itself deliberately stays client-neutral.  Presets live in
this small module so UI/config helpers can offer a reliable starting point
without teaching the runtime about individual coding agents.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class CommandPreset:
    command: tuple[str, ...]
    prompt_via: Literal["arg", "stdin"] = "arg"


CODEX = CommandPreset(
    command=(
        "codex",
        "exec",
        # Non-interactive Codex cannot stop to ask the terminal user.  Automatic
        # review keeps it inside workspace-write instead of bypassing the sandbox.
        "--approve-for-me",
        # AntHill workspaces do not have to be Git repositories.
        "--skip-git-repo-check",
        # stdout is the delivery channel; keep it stable and free of ANSI escapes.
        "--color",
        "never",
        # AntHill persists thread history itself, so one Codex transcript per
        # envelope would only accumulate duplicate state.
        "--ephemeral",
    ),
    # Prompts can contain history and arbitrary message text.  stdin avoids shell
    # quoting and argv-size limits entirely.
    prompt_via="stdin",
)
