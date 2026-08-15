from __future__ import annotations

import math
import time
from typing import Any

import requests

from .technology_discovery import WIKIDATA_SPARQL, _now, normalize_repository_url, officiality_status

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_CLASSES = (
    ("Q9143", "LANGUAGE"),          # programming language
    ("Q271680", "FRAMEWORK"),      # software framework
)
_RETRY_STATUSES = {429, 502, 503, 504}


def _request_json(
    session: requests.Session,
    url: str,
    *,
    user_agent: str,
    params: dict[str, Any],
    accept: str = "application/json",
    timeout: int = 45,
    attempts: int = 3,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            response = session.get(
                url,
                headers={"User-Agent": user_agent, "Accept": accept},
                params=params,
                timeout=timeout,
            )
            status = int(getattr(response, "status_code", 200) or 200)
            if status in _RETRY_STATUSES and attempt + 1 < attempts:
                retry_after = str((getattr(response, "headers", {}) or {}).get("Retry-After") or "").strip()
                try:
                    delay = min(8.0, max(0.5, float(retry_after))) if retry_after else min(4.0, 0.75 * (2**attempt))
                except ValueError:
                    delay = min(4.0, 0.75 * (2**attempt))
                time.sleep(delay)
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"unexpected JSON payload from {url}")
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(min(4.0, 0.75 * (2**attempt)))
    raise RuntimeError(f"Wikidata request failed after retries: {last_error}")


def _statement_values(entity: dict[str, Any], property_id: str) -> list[Any]:
    claims = entity.get("claims") or {}
    statements = claims.get(property_id) or [] if isinstance(claims, dict) else []
    values: list[Any] = []
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        mainsnak = statement.get("mainsnak") or {}
        datavalue = mainsnak.get("datavalue") or {} if isinstance(mainsnak, dict) else {}
        value = datavalue.get("value") if isinstance(datavalue, dict) else None
        if value not in (None, "") and value not in values:
            values.append(value)
    return values


def _first_string(entity: dict[str, Any], property_id: str) -> str | None:
    for value in _statement_values(entity, property_id):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _label(entity: dict[str, Any], qid: str) -> str:
    labels = entity.get("labels") or {}
    if isinstance(labels, dict):
        for language in ("en", "fr"):
            row = labels.get(language)
            if isinstance(row, dict) and row.get("value"):
                return str(row["value"])
    return qid


def _discover_class_ids(engine: Any, class_qid: str, limit: int) -> list[str]:
    # Deliberately keep WDQS cheap: only resolve entity IDs here. Labels and
    # metadata are fetched in batches through the Wikibase Action API.
    query = f"SELECT ?item WHERE {{ ?item wdt:P31 wd:{class_qid} . }} LIMIT {max(1, int(limit))}"
    payload = _request_json(
        engine.session,
        WIKIDATA_SPARQL,
        user_agent=engine.user_agent,
        params={"query": query, "format": "json"},
        accept="application/sparql-results+json",
    )
    bindings = (((payload.get("results") or {}).get("bindings")) or [])
    out: list[str] = []
    for row in bindings:
        if not isinstance(row, dict):
            continue
        value = str(((row.get("item") or {}).get("value")) or "")
        qid = value.rsplit("/", 1)[-1]
        if qid.startswith("Q") and qid[1:].isdigit() and qid not in out:
            out.append(qid)
    return out


def _fetch_entities(engine: Any, qids: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for start in range(0, len(qids), 50):
        batch = qids[start : start + 50]
        payload = _request_json(
            engine.session,
            WIKIDATA_API,
            user_agent=engine.user_agent,
            params={
                "action": "wbgetentities",
                "ids": "|".join(batch),
                "props": "labels|claims",
                "languages": "en|fr",
                "languagefallback": "1",
                "format": "json",
                "formatversion": "2",
            },
        )
        entities = payload.get("entities") or {}
        if isinstance(entities, dict):
            for qid, entity in entities.items():
                if isinstance(entity, dict) and not entity.get("missing"):
                    result[str(qid)] = entity
        elif isinstance(entities, list):
            for entity in entities:
                if isinstance(entity, dict) and entity.get("id") and not entity.get("missing"):
                    result[str(entity["id"])] = entity
    return result


def discover_wikidata_resilient(engine: Any, *, limit: int = 500) -> list[dict[str, Any]]:
    """Discover a bounded Wikidata seed without an expensive monolithic SPARQL query.

    WDQS is used only for cheap direct-instance ID discovery, split by class. The
    Wikibase Action API then enriches those IDs in batches. This intentionally avoids
    recursive P279* property paths, many OPTIONAL joins and SERVICE wikibase:label.
    """
    requested = max(1, min(int(limit), 5000))
    per_class = max(1, math.ceil(requested / len(WIKIDATA_CLASSES)))
    category_by_qid: dict[str, str] = {}
    class_counts: dict[str, int] = {}
    errors: list[dict[str, str]] = []

    for class_qid, category in WIKIDATA_CLASSES:
        try:
            ids = _discover_class_ids(engine, class_qid, per_class)
        except Exception as exc:
            errors.append({"class": class_qid, "category": category, "error": str(exc)[:500]})
            ids = []
        class_counts[category] = len(ids)
        for qid in ids:
            category_by_qid.setdefault(qid, category)

    qids = list(category_by_qid)[:requested]
    entities: dict[str, dict[str, Any]] = {}
    if qids:
        try:
            entities = _fetch_entities(engine, qids)
        except Exception as exc:
            errors.append({"class": "ENTITY_BATCH", "category": "ALL", "error": str(exc)[:500]})

    output: list[dict[str, Any]] = []
    for qid in qids:
        entity = entities.get(qid)
        if not entity:
            continue
        item: dict[str, Any] = {
            "qid": qid,
            "name": _label(entity, qid),
            "category": category_by_qid[qid],
            "discovery_sources": ["wikidata"],
            "official_website": _first_string(entity, "P856"),
            "canonical_repository": normalize_repository_url(_first_string(entity, "P1324")),
            "documentation_url": _first_string(entity, "P2078"),
            "latest_stable_version": _first_string(entity, "P348"),
        }
        score = 0
        evidence = ["WIKIDATA_ENTITY"]
        if item.get("canonical_repository"):
            score += 35
            evidence.append("WIKIDATA_REPOSITORY")
        if item.get("official_website"):
            score += 20
            evidence.append("WIKIDATA_OFFICIAL_WEBSITE")
        if item.get("documentation_url"):
            score += 25
            evidence.append("WIKIDATA_DOCUMENTATION")
        if item.get("latest_stable_version"):
            score += 10
            evidence.append("WIKIDATA_VERSION")
        item["officiality_score"] = min(score, 90)
        item["officiality_status"] = officiality_status(item["officiality_score"])
        item["officiality_evidence"] = evidence
        output.append(engine._upsert(f"wikidata:{qid}", item))

    run = {
        "kind": "wikidata-resilient",
        "requested": requested,
        "discovered_ids": len(qids),
        "materialized": len(output),
        "class_counts": class_counts,
        "errors": errors,
        "at": _now(),
    }
    engine.data["wikidata_last_run"] = run
    engine.data.setdefault("runs", []).append(run)
    engine._save()
    return output
