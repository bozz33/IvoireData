from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urljoin, urlparse


# Types de limites administratives géoBoundaries, du pays entier jusqu'au niveau local.
# Si l'URL configurée est un « directory listing » HTML (cas de gbOpen/CIV/), on explore
# automatiquement ces niveaux pour récupérer les métadonnées JSON de chaque couche.
_ADM_LEVELS = ("ADM0", "ADM1", "ADM2", "ADM3", "ADM4", "ADM5")


def _resolve_meta_urls(api_url: str) -> list[str]:
    """Renvoie les URLs à interroger pour les métadonnées géoBoundaries.

    Si api_url pointe vers un directory listing (path du type .../CIV/ sans niveau ADM),
    on génère une URL par niveau ADM ; sinon on utilise api_url tel quel.
    """
    parsed = urlparse(api_url)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/"
    last_segment = parsed.path.rstrip("/").rsplit("/", 1)[-1].upper()
    # Directory listing : le dernier segment est le code pays (ex. CIV), pas un niveau ADM.
    if last_segment not in _ADM_LEVELS:
        return [urljoin(base, lvl) + "/" for lvl in _ADM_LEVELS]
    return [api_url]


def geoboundaries_resource(*, api_url: str, source_id: str = "civ_geoboundaries", user_agent: str = "IvoireData/0.4"):
    import dlt, requests

    @dlt.resource(name="geoboundaries", write_disposition="replace")
    def resource():
        session = requests.Session(); session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        index = 0
        for meta_url in _resolve_meta_urls(api_url):
            meta_response = session.get(meta_url, timeout=120)
            if meta_response.status_code == 404:
                continue  # niveau ADM indisponible pour ce pays, on passe au suivant
            meta_response.raise_for_status()
            ctype = meta_response.headers.get("content-type", "")
            # Directory listing HTML : on ignore silencieusement (géré par les autres URLs).
            if "html" in ctype.lower():
                continue
            try:
                meta: Any = meta_response.json()
            except ValueError:
                continue  # pas du JSON exploitable
            rows = meta if isinstance(meta, list) else [meta]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                metadata = dict(row); metadata["__ivoiredata_source_url"] = meta_url
                yield dlt.mark.with_table_name(metadata, "geoboundaries_metadata")
                download_url = row.get("gjDownloadURL") or row.get("gjDownloadUrl") or row.get("geoJSON") or row.get("geojson")
                if not isinstance(download_url, str) or not download_url:
                    continue
                response = session.get(download_url, timeout=180); response.raise_for_status(); digest = hashlib.sha256(response.content).hexdigest(); payload = response.json()
                features = payload.get("features", []) if isinstance(payload, dict) else []
                for feature_index, feature in enumerate(features):
                    if not isinstance(feature, dict):
                        continue
                    item = {"feature_index": feature_index, "feature_id": feature.get("id"), "properties": feature.get("properties") or {}, "geometry": feature.get("geometry"), "__ivoiredata_source_url": download_url, "__ivoiredata_raw_sha256": digest, "__ivoiredata_boundary_index": index}
                    yield dlt.mark.with_table_name(item, "geoboundaries_features")
                index += 1
    return resource()
