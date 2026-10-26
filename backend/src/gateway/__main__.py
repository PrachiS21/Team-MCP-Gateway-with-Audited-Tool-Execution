"""Start the gateway:  python -m gateway   (settings come from GATEWAY_* environment variables)"""

import logging
import sys

import uvicorn

from gateway.config import Settings
from gateway.seed import SeedError
from gateway.server import create_gateway


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    try:
        gw = create_gateway(settings)
    except SeedError as exc:
        raise SystemExit(f"refusing to start: {exc}") from None

    if settings.admin_email:
        admin, issued = gw.auth.ensure_admin(settings.admin_email)
        if issued is not None:  # only when the admin has no usable key yet; shown once, never stored
            print(f"\nFirst admin: {admin.email}\nAPI key (shown once, store it safely):\n  {issued.key}\n",
                  file=sys.stderr, flush=True)

    uvicorn.run(gw.http_app(), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
