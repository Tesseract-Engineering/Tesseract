"""
python -m forge_demo                   open the demo chat: Claude Code with only forge_demo's tools
python -m forge_demo serve --stdio     the MCP server itself (Claude Code starts this)

The chat starts with no other MCP servers, no built-in tools and the setup agent's prompt
(prompt.md), and its tool calls need no permission prompts, so every take runs the same way. Extra
arguments go to claude, e.g. `python -m forge_demo --model opus`.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOLS = ["describe_sources", "plan_surfaces", "deploy_surface", "smoke_test"]


def chat(extra: list[str]) -> None:
    claude = shutil.which("claude")
    if claude is None:
        sys.exit("Claude Code (claude) is not on PATH")
    # .env goes to the tools' server only: loaded into this process, its ANTHROPIC_API_KEY would
    # override the presenter's Claude login.
    dotenv = {}
    if (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                dotenv[k.strip()] = v.strip().strip("'\"")
    given = {**dotenv, **os.environ}
    env = {k: given[k] for k in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "FORGE_GRAPH_NS", "TESSERACT_URL",
                                 "MEETING_URL", "TICKET_DESK_URL", "FORGE_DEMO_AS", "FORGE_DEMO_CALL",
                                 "ADDIN_KEY", "TESSERACT_KEYS", "SLACKBOT_BOT_TOKEN") if given.get(k)}
    env.setdefault("NEO4J_PASSWORD", "neo4j-local-dev")
    env["PYTHONPATH"] = str(ROOT)
    config = {"mcpServers": {"forge": {"command": sys.executable, "args": ["-m", "forge_demo", "serve", "--stdio"],
                                       "env": env, "cwd": str(ROOT)}}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(config, f)
    os.chdir(ROOT)
    os.execv(claude, [claude, "--mcp-config", f.name, "--strict-mcp-config", "--tools", "",
                      "--allowedTools", ",".join(f"mcp__forge__{t}" for t in TOOLS),
                      "--append-system-prompt", (HERE / "prompt.md").read_text(encoding="utf-8"), *extra])


if __name__ == "__main__":
    if sys.argv[1:] == ["serve", "--stdio"]:
        from .server import mcp

        mcp.run("stdio")
    else:
        chat(sys.argv[1:])
