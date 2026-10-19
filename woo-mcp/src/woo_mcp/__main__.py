"""Start the server:  python -m woo_mcp   (settings come from environment variables, see config.py)"""

import logging

import uvicorn

from woo_mcp.config import Settings
from woo_mcp.server import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
