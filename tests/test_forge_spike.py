"""CP0 spike: which MCP protocol revision the pinned SDK speaks, in process and over stdio."""

import sys
from pathlib import Path

import anyio
import pytest

mcp = pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters  # noqa: E402
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, LATEST_MODERN_VERSION  # noqa: E402

SERVER = Path(__file__).with_name("forge_spike_server.py")


async def negotiate(target, mode="auto") -> tuple[str, str]:
    async with Client(target, mode=mode) as client:
        result = await client.call_tool("ping", {})
        return client.protocol_version, result.structured_content["protocol_version"]


def stdio():
    return StdioServerParameters(command=sys.executable, args=[str(SERVER)])


def test_sdk_targets_the_stateless_revision():
    assert LATEST_MODERN_VERSION == "2026-07-28"


def test_stdio_auto_negotiates_the_stateless_revision():
    client_side, server_side = anyio.run(negotiate, stdio())
    assert client_side == server_side == LATEST_MODERN_VERSION


def test_stdio_legacy_falls_back_to_the_handshake():
    client_side, server_side = anyio.run(negotiate, stdio(), "legacy")
    assert client_side == server_side == HANDSHAKE_PROTOCOL_VERSIONS[-1]
