"""Start the server:  python -m gst_mcp   (MCP_SERVER_TOKEN required; STORE_STATE, MCP_PORT optional)"""

import logging
import os
from typing import Mapping

import uvicorn

from gst_mcp.server import create_app


def port_from_env(env: Mapping[str, str]) -> int:
    """MCP_PORT, else PORT (set by Cloud Run), else 8000."""
    return int(env.get("MCP_PORT") or env.get("PORT") or 8000)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=port_from_env(os.environ), log_level="info")


if __name__ == "__main__":
    main()
