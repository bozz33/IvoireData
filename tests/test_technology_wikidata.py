from __future__ import annotations

from pathlib import Path

from ivoiredata.technology_wikidata import discover_wikidata_resilient


class FakeResponse:
    def __init__(self, payload, *, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)


class FakeEngine:
    def __init__(self, tmp_path: Path, session: FakeSession):
        self.session = session
        self.user_agent = "IvoireData-test"
        self.state_path = tmp_path / "technology_catalog.json"
        self.data = {"technologies": {}, "runs": []}

    def _upsert(self, key, record):
        row = dict(record)
        row["technology_id"] = key
        row.setdefault("enabled_for_corpus", False)
        self.data["technologies"][key] = row
        return row

    def _save(self):
        return None


def _url_claim(value: str):
    return [{"mainsnak": {"datavalue": {"value": value}}}]


def test_wikidata_discovery_splits_class_lookup_and_enriches_via_action_api(tmp_path: Path):
    session = FakeSession([
        FakeResponse({
            "results": {"bindings": [
                {"item": {"value": "http://www.wikidata.org/entity/Q100"}},
                {"item": {"value": "http://www.wikidata.org/entity/Q101"}},
            ]}
        }),
        FakeResponse({
            "results": {"bindings": [
                {"item": {"value": "http://www.wikidata.org/entity/Q200"}},
            ]}
        }),
        FakeResponse({
            "entities": {
                "Q100": {
                    "labels": {"en": {"language": "en", "value": "ExampleLang"}},
                    "claims": {
                        "P856": _url_claim("https://examplelang.dev"),
                        "P1324": _url_claim("https://github.com/example/lang.git"),
                        "P2078": _url_claim("https://examplelang.dev/docs"),
                        "P348": _url_claim("2.1.0"),
                    },
                },
                "Q101": {
                    "labels": {"fr": {"language": "fr", "value": "Langue Deux"}},
                    "claims": {},
                },
                "Q200": {
                    "labels": {"en": {"language": "en", "value": "ExampleFramework"}},
                    "claims": {
                        "P1324": _url_claim("https://gitlab.com/example/framework"),
                    },
                },
            }
        }),
    ])
    engine = FakeEngine(tmp_path, session)

    rows = discover_wikidata_resilient(engine, limit=4)

    assert len(rows) == 3
    by_name = {row["name"]: row for row in rows}
    assert by_name["ExampleLang"]["category"] == "LANGUAGE"
    assert by_name["ExampleLang"]["canonical_repository"] == "https://github.com/example/lang"
    assert by_name["ExampleLang"]["documentation_url"] == "https://examplelang.dev/docs"
    assert by_name["ExampleLang"]["latest_stable_version"] == "2.1.0"
    assert by_name["ExampleFramework"]["category"] == "FRAMEWORK"
    assert by_name["ExampleFramework"]["canonical_repository"] == "https://gitlab.com/example/framework"
    assert all(row["enabled_for_corpus"] is False for row in rows)

    # Two cheap WDQS ID-only calls, then one batched Action API enrichment call.
    assert len(session.calls) == 3
    first_query = session.calls[0][1]["params"]["query"]
    second_query = session.calls[1][1]["params"]["query"]
    assert "wdt:P31 wd:Q9143" in first_query
    assert "wdt:P31 wd:Q271680" in second_query
    assert "P279" not in first_query + second_query
    assert "SERVICE wikibase:label" not in first_query + second_query
    assert session.calls[2][1]["params"]["action"] == "wbgetentities"
    assert session.calls[2][1]["params"]["props"] == "labels|claims"

    run = engine.data["wikidata_last_run"]
    assert run["materialized"] == 3
    assert run["errors"] == []
