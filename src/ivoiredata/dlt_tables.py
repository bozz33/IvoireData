from __future__ import annotations

from typing import Any


def replace_table_item(dlt: Any, item: Any, table_name: str):
    """Dispatch one item to an independently replaceable dynamic table.

    The parent resource must keep its default write disposition (`append`). dlt documents
    that a resource-level `replace` truncates every table belonging to that resource,
    even tables that receive no rows. A table variant gives each dynamically dispatched
    table its own replace hint, so only tables actually emitted in the current run are
    refreshed.
    """
    hints = dlt.mark.make_hints(table_name=table_name, write_disposition="replace")
    return dlt.mark.with_hints(item, hints, create_table_variant=True)
