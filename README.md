# IvoireData 🇨🇮

**v0.4.0 — moteur fédéré de données + usine IvoireCorpus, construit sur dlt OSS**

IvoireData ne stocke pas les gros datasets dans GitHub. Le dépôt contient le moteur, les connecteurs, le registre, les règles de fraîcheur, la qualité, la provenance, la fabrication de corpus et l'API. Les données réelles vont vers un filesystem local ou un stockage S3-compatible (MinIO, R2, S3...).

## Fonctionnalités
- dlt comme moteur Extract/Normalize/Load et état incrémental ;
- `data.gouv.ci` Data Fair : catalogue + table par dataset + détection de changement ;
- pages/PDF publics avec SHA-256 et chunks ;
- CSV/JSON/JSONL/XLS/XLSX/Parquet ;
- registre multisectoriel et résolution automatique du connecteur ;
- updater/scheduler avec `refresh_hours` ;
- destination locale ou S3/MinIO ;
- SQL read-only via le client filesystem/DuckDB de dlt ;
- ranking et cross-check des sources ;
- recherche documentaire ;
- nettoyage, qualité, déduplication, IvoireCorpus versionné ;
- tokenizer BPE optionnel ;
- FastAPI + CLI + Docker Compose ;
- CI et synchronisations GitHub planifiées.

## Démarrage
```bash
python -m pip install -e '.[dev]'
ivoiredata sources --public
ivoiredata sync civ_datagouv_catalog
ivoiredata query 'SELECT * FROM datagouv_catalog LIMIT 5'
uvicorn ivoiredata.api:app --host 0.0.0.0 --port 8000
```

## Mise à jour continue
`configs/runtime_sources.json` contrôle la fréquence. `civ_datagouv_catalog` est synchronisé quotidiennement. Les connecteurs utilisent état dlt + signatures/hash pour ne retélécharger que les changements.

## Corpus
```bash
ivoiredata corpus-build civ-0.1 TABLE1 TABLE2 --output corpora
```
Les corpus sont immuables ; une mise à jour produit une nouvelle version.

Voir `docs/ENGINE.md`, `docs/DEPLOYMENT.md`, `docs/CORPUS.md`.
