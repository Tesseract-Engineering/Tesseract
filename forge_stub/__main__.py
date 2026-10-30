"""python -m forge_stub serve --stdio: the stand-in forge, over stdio, as FORGE_VIEWER_EMAIL."""

import sys

from .server import mcp

if __name__ == "__main__":
    if sys.argv[1:] != ["serve", "--stdio"]:
        sys.exit("usage: python -m forge_stub serve --stdio")
    mcp.run("stdio")
