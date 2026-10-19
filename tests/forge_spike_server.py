"""CP0 spike: a one-tool MCP server that reports the protocol revision it negotiated.

Run over stdio with `python tests/forge_spike_server.py`. test_forge_spike.py connects to it
with the SDK client; docs/FORGE.md records what Claude Code negotiated against the same file."""

from mcp.server.mcpserver import Context, MCPServer

server = MCPServer("forge-spike")


@server.tool()
def ping(ctx: Context) -> dict[str, str | None]:
    """Return the protocol revision this request arrived on."""
    return {"protocol_version": ctx.protocol_version}


if __name__ == "__main__":
    server.run("stdio")
