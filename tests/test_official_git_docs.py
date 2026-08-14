from __future__ import annotations

import re

from ivoiredata.connectors import official_docs_strategy as strategy
from ivoiredata.connectors.official_git_docs import _path_allowed, parse_github_tree_url


def test_parse_github_tree_url():
    assert parse_github_tree_url("https://github.com/laravel/docs/tree/13.x") == ("laravel/docs", "13.x")
    assert parse_github_tree_url("https://github.com/filamentphp/filament/tree/5.x") == ("filamentphp/filament", "5.x")
    assert parse_github_tree_url("https://laravel.com/docs/13.x") is None


def test_git_path_filter_supports_filament_package_docs():
    excludes = [re.compile(r"^packages/(?![^/]+/docs/)", re.I)]
    prefixes = ["docs/", "packages/"]
    assert _path_allowed("docs/01-introduction/02-installation.md", prefixes, excludes)
    assert _path_allowed("packages/widgets/docs/03-charts.md", prefixes, excludes)
    assert not _path_allowed("packages/widgets/README.md", prefixes, excludes)
    assert not _path_allowed("docs-assets/app/database/seeders/DatabaseSeeder.php", prefixes, excludes)


def test_strategy_routes_github_tree_to_git_connector(monkeypatch):
    called = {}

    def fake_git(**kwargs):
        called.update(kwargs)
        return "git-resource"

    monkeypatch.setattr(strategy, "official_git_docs_resource", fake_git)
    result = strategy.official_docs_resource(
        source_id="prog_php_laravel",
        url="https://github.com/laravel/docs/tree/13.x",
        include_prefixes=[],
        max_pages=1000,
        max_bytes_per_page=1000000,
        max_new_bytes_per_run=10000000,
        user_agent="test",
    )
    assert result == "git-resource"
    assert called["repository"] == "laravel/docs"
    assert called["ref"] == "13.x"
    assert called["source_id"] == "prog_php_laravel"


def test_strategy_keeps_web_connector_for_normal_docs(monkeypatch):
    called = {}

    def fake_web(**kwargs):
        called.update(kwargs)
        return "web-resource"

    monkeypatch.setattr(strategy, "_original_official_docs_resource", fake_web)
    result = strategy.official_docs_resource(
        source_id="prog_python_core",
        url="https://docs.python.org/3/",
        include_prefixes=["/3/"],
    )
    assert result == "web-resource"
    assert called["url"] == "https://docs.python.org/3/"
