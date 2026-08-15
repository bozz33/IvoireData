"""Compatibility facade for the official Data Fair collector."""
from __future__ import annotations

from .data_gouv_ci_v2 import (
    API,
    PORTAL,
    _classification,
    _dataset_id,
    _discover_official,
    _full_download_streaming,
    _items,
    _legacy_signature,
    _lines_download_streaming,
    _rows,
    _safe_table,
    _signature,
    dataset_id_from_public_url,
)
from .data_gouv_ci_v3 import data_gouv_ci_resource_v3

# Keep the historical public name so engine/plugins do not need a breaking change.
data_gouv_ci_resource = data_gouv_ci_resource_v3

__all__ = [
    "API",
    "PORTAL",
    "data_gouv_ci_resource",
    "data_gouv_ci_resource_v3",
    "dataset_id_from_public_url",
]
