"""Start the gateway:  python -m gateway   (settings come from GATEWAY_* environment variables)"""

import sys

import uvicorn

from gateway.config import Settings
from gateway.logs import setup_logging
from gateway.seed import SeedError
from gateway.server import create_gateway


def main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.log_format, settings.gcp_project)
    try:
        gw = create_gateway(settings)
    except SeedError as exc:
        raise SystemExit(f"refusing to start: {exc}") from None

    if settings.admin_email:
        admin, issued = gw.auth.ensure_admin(settings.admin_email)
        if issued is not None:  # only when the admin has no usable key yet; shown once, never stored
            print(f"\nFirst admin: {admin.email}\nAPI key (shown once, store it safely):\n  {issued.key}\n",
                  file=sys.stderr, flush=True)

    uvicorn_logging = {}
    if settings.log_format == "json":
        # uvicorn's own lines go through our JSON format too (log_config=None), and its access log is off:
        # Cloud Run already writes a request log for every request (status, latency, trace).
        uvicorn_logging = {"log_config": None, "access_log": False}
    uvicorn.run(gw.http_app(), host=settings.host, port=settings.port, log_level="info", **uvicorn_logging)


if __name__ == "__main__":
    main()
