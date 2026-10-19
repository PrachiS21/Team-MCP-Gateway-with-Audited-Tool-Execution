"""Lets the backend's end-to-end test use the sibling woo-mcp project (a separate package) in-process.
woo-mcp ships its own `tests` package, so its fake store is loaded by file path to avoid a name clash."""

import importlib.util
import sys
from pathlib import Path

WOO_MCP = Path(__file__).resolve().parents[2] / "woo-mcp"
if str(WOO_MCP / "src") not in sys.path:
    sys.path.insert(0, str(WOO_MCP / "src"))


def load_fake_woocommerce():
    path = WOO_MCP / "tests" / "fixtures" / "fake_woocommerce.py"
    spec = importlib.util.spec_from_file_location("fake_woocommerce", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fake_woocommerce"] = module
    spec.loader.exec_module(module)
    return module
