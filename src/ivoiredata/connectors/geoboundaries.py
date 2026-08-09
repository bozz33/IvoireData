from __future__ import annotations

import hashlib
from typing import Any


def geoboundaries_resource(*, api_url: str, source_id: str = "civ_geoboundaries", user_agent: str = "IvoireData/0.4"):
    import dlt, requests

    @dlt.resource(name="geoboundaries", write_disposition="replace")
    def resource():
        session = requests.Session(); session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        meta_response = session.get(api_url, timeout=120); meta_response.raise_for_status(); meta: Any = meta_response.json()
        rows = meta if isinstance(meta, list) else [meta]
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            metadata = dict(row); metadata["__ivoiredata_source_url"] = api_url
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
    return resource()
