from __future__ import annotations

from . import official_docs as base
from .official_git_docs import official_git_docs_resource, parse_github_tree_url

_original_official_docs_resource = base.official_docs_resource


def official_docs_resource(*, source_id: str, url: str, **kwargs):
    parsed = parse_github_tree_url(url)
    if parsed is None:
        return _original_official_docs_resource(source_id=source_id, url=url, **kwargs)

    repository, ref = parsed
    return official_git_docs_resource(
        source_id=source_id,
        repository=repository,
        ref=ref,
        user_agent=kwargs.get("user_agent", "IvoireData/0.8.3"),
        include_prefixes=kwargs.get("include_prefixes", ()),
        exclude_patterns=kwargs.get("exclude_patterns", ()),
        max_pages=kwargs.get("max_pages", 100_000),
        max_bytes_per_page=kwargs.get("max_bytes_per_page", 12_000_000),
        max_new_bytes_per_run=kwargs.get("max_new_bytes_per_run", 500_000_000),
        request_pause_seconds=kwargs.get("request_pause_seconds", 0.0),
        snapshot_dir=kwargs.get("snapshot_dir"),
        metadata_base=kwargs.get("metadata_base"),
        upstream_state_path=kwargs.get("upstream_state_path"),
        license_name=kwargs.get("license_name"),
        license_url=kwargs.get("license_url"),
        training_eligible=kwargs.get("training_eligible", False),
        license_review_status=kwargs.get("license_review_status", "UNREVIEWED"),
    )


# Engine imports official_docs_resource from the base module only after package __init__
# runs. Patch the exported function once so existing connector names/config remain stable.
base.official_docs_resource = official_docs_resource
