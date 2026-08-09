from __future__ import annotations

from pathlib import Path
from typing import Any

from ..snapshots import save_snapshot

# API officielle des projets World Bank par pays (endpoint de recherche).
# NB : le filtre pays utilise le code ISO2 (CI pour la Côte d'Ivoire), pas l'ISO3 (CIV).
SEARCH_API = "https://search.worldbank.org/api/v2/projects"


def world_bank_projects_resource(
    *,
    country_code: str = "CI",
    page_size: int = 50,
    user_agent: str = "IvoireData/0.6",
    snapshot_dir: Path | None = None,
):
    """Load World Bank projects for a country from the official search API.

    L'endpoint /api/v2/projects accepte countrycode_exact=<ISO2> et renvoie les projets
    (actifs et fermés) avec leurs métadonnées : nom, statut, montant, date d'approbation,
    secteur, type de prêt, documents, etc. Le JSON brut de chaque page est snapshoté.
    """
    import dlt
    import requests

    page_size = max(1, min(int(page_size), 100))

    @dlt.resource(name="worldbank_projects", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        page = 1
        seen_ids: set[str] = set()
        while True:
            params = {
                "countrycode_exact": country_code,
                "format": "json",
                "rows": page_size,
                "os": (page - 1) * page_size,
            }
            response = session.get(SEARCH_API, params=params, timeout=120)
            response.raise_for_status()
            save_snapshot(
                snapshot_dir,
                source_id="civ_worldbank_projects",
                url=response.url,
                content=response.content,
                content_type=response.headers.get("content-type"),
                name=f"projects-page-{page:04d}.json",
            )
            payload: Any = response.json()
            total = int(payload.get("total") or 0)
            projects = payload.get("projects") or []
            if isinstance(projects, dict):
                projects = list(projects.values())
            if not projects:
                return
            for project in projects:
                if not isinstance(project, dict):
                    continue
                pid = str(project.get("id") or "")
                if pid and pid in seen_ids:
                    continue
                if pid:
                    seen_ids.add(pid)
                row = dict(project)
                row["__ivoiredata_source_url"] = SEARCH_API
                row["__ivoiredata_country"] = country_code
                yield dlt.mark.with_table_name(row, "worldbank_projects")
            # Pagination : arrêt si on a récupéré tout le total ou si la page est incomplète.
            if total and len(seen_ids) >= total:
                return
            if len(projects) < page_size:
                return
            page += 1

    return resource()
