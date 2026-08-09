from __future__ import annotations

import csv
import io
import tempfile
from pathlib import Path
from typing import Iterable

from ..snapshots import save_snapshot


# L'ILOSTAT expose deux backends :
#   - /files/ref_area/{COUNTRY}_{FREQ}.rds  -> binaire R, parsé par pyreadr/librdata
#     (instable : lib C peut segfaulter sur certains fichiers)
#   - /data/indicator?ref_area={COUNTRY}    -> CSV natif, filtrable côté serveur par pays
# On privilégie le CSV : pas de dépendance native, pas de risque de SIGSEGV, et le serveur
# ne renvoie que les lignes du pays demandé (~18 Ko pour CIV au lieu de 3,5 Mo mondiaux).
CSV_BASE = "https://rplumber.ilo.org/data/indicator"


class RdsParseError(RuntimeError):
    """Le parsing d'un fichier .rds a échoué (conservé pour la compatibilité descendante)."""


def _read_rds_rows(path: Path):
    """Legacy : conservé pour ne pas casser d'éventuels appels, mais le connecteur
    principal utilise désormais le chemin CSV. Lève toujours RdsParseError."""
    raise RdsParseError(f"RDS parsing disabled, use CSV backend ({path.name})")


def ilostat_ref_area_resource(
    *,
    country: str = "CIV",
    frequencies: Iterable[str] = ("A",),
    base_url: str = CSV_BASE,
    user_agent: str = "IvoireData/0.6",
    snapshot_dir: Path | None = None,
):
    """Load ILOSTAT country datasets via the official CSV backend.

    Le backend CSV (/data/indicator) accepte un filtre ref_area={country} côté serveur et
    renvoie directement les séries emploi/travail du pays au format texte. Cela évite
    complètement le fichier .rds (et donc la lib C pyreadr/librdata qui segfaultait).
    Le snapshot brut CSV est conservé dans raw/ pour traçabilité.
    """
    import dlt
    import requests

    # On force les fréquences demandées en majuscules ; le backend CSV expose une colonne
    # `obs_status` qui correspond à la fréquence (A=annuelle, Q=trimestrielle...).
    wanted_freqs = {str(f).upper().strip() for f in frequencies if str(f).strip()}

    @dlt.resource(name="ilostat_civ", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent, "Accept": "text/csv"})
        url = f"{base_url.rstrip('/')}"
        response = session.get(url, params={"ref_area": country}, timeout=240)
        response.raise_for_status()
        snapshot = save_snapshot(
            snapshot_dir,
            source_id="civ_ilostat",
            url=response.url,
            content=response.content,
            content_type=response.headers.get("content-type"),
            name=f"{country}_indicator.csv",
        )
        digest = str(snapshot["sha256"])
        raw_path = snapshot.get("local_path")
        reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig", "replace")))
        count = 0
        for row in reader:
            # Filtrer par fréquence si demandé (colonne obs_status). Si aucune fréquence
            # n'est fournie, on garde tout.
            freq = (row.get("obs_status") or "").strip().upper()
            if wanted_freqs and freq not in wanted_freqs:
                continue
            out = {str(k): v for k, v in row.items() if k is not None}
            out["__ivoiredata_source_url"] = response.url
            out["__ivoiredata_raw_sha256"] = digest
            out["__ivoiredata_raw_path"] = raw_path
            out["__ivoiredata_ref_area"] = country
            yield dlt.mark.with_table_name(out, f"ilostat_{country.lower()}")
            count += 1

    return resource()
