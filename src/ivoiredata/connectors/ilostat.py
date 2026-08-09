from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from ..snapshots import save_snapshot


# Sous-programme isolé pour parser un .rds avec pyreadr.
# pyreadr s'appuie sur librdata (lib C) qui peut segfaulter sur certains fichiers.
# En l'exécutant dans un process séparé, on garantit qu'un crash C ne tue pas le
# moteur principal : le parent détecte le segfault (exit 139) et lève une exception
# Python gérable, au lieu de faire planter toute la synchro.
_RDS_RUNNER = """
import json, sys
import pyreadr

path = sys.argv[1]
table = sys.argv[2]
try:
    frames = pyreadr.read_r(path)
except Exception as exc:
    json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)
    sys.exit(2)

out = sys.stdout
for key, frame in frames.items():
    if frame is None:
        continue
    for row in frame.to_dict(orient="records"):
        out.write(json.dumps(row, default=str, ensure_ascii=False))
        out.write("\\n")
        out.flush()
"""


def _read_rds_rows(path: Path):
    """Itère les lignes d'un .rds via un subprocess isolé.

    Lève RdsParseError si pyreadr crash (segfault) ou renvoie une erreur.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _RDS_RUNNER, str(path), path.stem],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line:
                yield json.loads(line)
        return
    # exit 139 (ou -11) = SIGSEGV : pyreadr/librdata a crashé en C.
    if proc.returncode in (139, -11):
        raise RdsParseError(
            f"pyreadr segfaulted (SIGSEGV) parsing {path.name} — librdata cannot read this file"
        )
    # exit 2 = erreur Python explicite du runner.
    if proc.returncode == 2 and proc.stdout.strip():
        try:
            err = json.loads(proc.stdout.strip().splitlines()[-1])
            raise RdsParseError(f"pyreadr failed parsing {path.name}: {err.get('error')}")
        except json.JSONDecodeError:
            pass
    raise RdsParseError(
        f"pyreadr exited with code {proc.returncode} parsing {path.name}: {proc.stderr[:500]}"
    )


class RdsParseError(RuntimeError):
    """Le parsing d'un fichier .rds a échoué (crash lib C ou erreur)."""


def ilostat_ref_area_resource(
    *,
    country: str = "CIV",
    frequencies: Iterable[str] = ("A",),
    base_url: str = "https://rplumber.ilo.org/files/ref_area",
    user_agent: str = "IvoireData/0.6",
    snapshot_dir: Path | None = None,
):
    """Load ILOSTAT country datasets from the official bulk backend.

    Le parsing .rds est isolé dans un subprocess car pyreadr/librdata peut segfaulter
    sur certains fichiers : un crash C ne doit pas interrompre toute la synchro.
    """
    import dlt
    import requests

    @dlt.resource(name="ilostat_civ", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent})
        for frequency in frequencies:
            freq = str(frequency).upper().strip()
            url = f"{base_url.rstrip('/')}/{country.upper()}_{freq}.rds"
            response = session.get(url, timeout=240)
            response.raise_for_status()
            snapshot = save_snapshot(
                snapshot_dir,
                source_id="civ_ilostat",
                url=url,
                content=response.content,
                content_type=response.headers.get("content-type"),
                name=f"{country.upper()}_{freq}.rds",
            )
            digest = str(snapshot["sha256"])
            raw_path = snapshot.get("local_path")
            with tempfile.NamedTemporaryFile(suffix=".rds", delete=False) as tmp:
                tmp.write(response.content)
                tmp_path = Path(tmp.name)
            try:
                # Le snapshot brut est conservé quel que soit le résultat du parsing :
                # même si librdata ne sait pas lire ce fichier aujourd'hui, la donnée
                # source reste disponible pour un connecteur spécialisé ultérieur.
                for row in _read_rds_rows(tmp_path):
                    row["__ivoiredata_source_url"] = url
                    row["__ivoiredata_raw_sha256"] = digest
                    row["__ivoiredata_raw_path"] = raw_path
                    row["__ivoiredata_frequency"] = freq
                    yield dlt.mark.with_table_name(row, f"ilostat_{country.lower()}_{freq.lower()}")
            finally:
                tmp_path.unlink(missing_ok=True)

    return resource()
