from __future__ import annotations

from typing import Any


def replace_table_item(dlt: Any, item: Any, table_name: str):
    """Dispatch one item to an independently replaceable dynamic table."""
    hints = dlt.mark.make_hints(table_name=table_name, write_disposition="replace")
    return dlt.mark.with_hints(item, hints, create_table_variant=True)


def make_incremental_replace_safe(resource: Any):
    """Make a multi-table replace resource safe when some tables are omitted.

    dlt full-loading semantics replace all tables belonging to a resource declared with
    `write_disposition="replace"`, even tables receiving no rows in the current run.
    Incremental IvoireData connectors intentionally omit unchanged tables, so the parent
    resource must be append-only while each actually emitted table is marked as its own
    replaceable table variant.

    Existing connector generators already dispatch rows with `dlt.mark.with_table_name`.
    `add_map` receives that TableNameMeta and upgrades it to a replace table variant
    without changing connector code or table names.
    """
    import dlt

    resource.apply_hints(write_disposition="append")
    resource_name = resource.name

    def _to_variant(item: Any, meta: Any = None):
        table_name = getattr(meta, "table_name", None) or resource_name
        return replace_table_item(dlt, item, str(table_name))

    resource.add_map(_to_variant)
    return resource
