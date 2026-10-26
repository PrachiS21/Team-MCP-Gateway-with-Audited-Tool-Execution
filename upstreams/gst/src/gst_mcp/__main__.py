"""Start the server:  python -m gst_mcp   (MCP_SERVER_TOKEN required; STORE_STATE, MCP_PORT optional)"""

import logging
import os

import uvicorn

from gst_mcp.server import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8000")), log_level="info")


if __name__ == "__main__":
    main()
