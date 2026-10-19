"""Undo rules for the WooCommerce MCP server's changing tools (see compensation.py).

create_product       undo: delete the product it created. No "still unchanged" check on purpose: the delete
                     is idempotent (an already-deleted product counts as success), and a product that was
                     edited since is still a creation the audit log never recorded.
update_order_status  before: read the order to capture its status. undo: set that status back. Guard:
                     re-read first; if the status is no longer the one we set, someone else changed the
                     order and the undo is skipped (conflict). Calls on the same order never overlap.
delete_product       internal: hidden from clients, used only by the undo worker.
"""

from gateway.compensation import Compensation, CompensationRegistry


def register_woo_compensations(registry: CompensationRegistry, slug: str = "woo") -> None:
    registry.register(slug, "create_product", Compensation(
        undo_tool="delete_product",
        build_undo_args=lambda args, result, before: {"product_id": result["id"]},
    ))

    def undo_status(args, result, before):
        previous = (before or {}).get("status")
        if not previous or previous == args["status"]:
            return None  # nothing changed, so nothing to put back
        return {"order_id": args["order_id"], "status": previous}

    registry.register(slug, "update_order_status", Compensation(
        before_tool="get_order",
        build_before_args=lambda args: {"order_id": args["order_id"]},
        undo_tool="update_order_status",
        build_undo_args=undo_status,
        verify_tool="get_order",
        build_verify_args=lambda args, result, before: {"order_id": args["order_id"]},
        still_as_we_left_it=lambda current, args, result, before: (current or {}).get("status") == args["status"],
        lock_key=lambda args: f"order:{args['order_id']}",
    ))

    registry.hide(slug, "delete_product")
