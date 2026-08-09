# IvoireData 🇨🇮

**v0.5.0 — moteur local de collecte, mise à jour, recherche et fabrication d’IvoireCorpus, construit sur dlt OSS**

IvoireData est l’infrastructure de données destinée à alimenter une IA ivoirienne : il découvre les sources, récupère les données publiques, conserve leur provenance, détecte les mises à jour, normalise les contenus et fabrique des corpus versionnés pour le pré-entraînement.

**Toutes les données réelles restent sur le PC.** Aucun S3, R2 ou MinIO n’est nécessaire. PostgreSQL n’est pas requis pour la V1 : les gros volumes restent en fichiers locaux et dlt/DuckDB fournissent la couche de requête.

## Architecture

```text
Sources officielles / APIs / sites / PDF / CSV / XLSX / PBF
                           │
                    Source Registry
                           │
                    Connector Router
                           │
                          dlt
                           │
       ┌───────────────────┼────────────────────┐
       │                   │                    │
 data_lake/          .ivoiredata/          raw_external/
 tables locales      état/fraîcheur         gros fichiers
       │
       ├──────── SQL / Search / API locale
       │
       └──────── Cleaning → Quality → Dedup → IvoireCorpus
                                                │
                                           tokenizer/
                                                │
                                          entraînement
```

## Ce que fait v0.5.0

- Data.gouv.ci / Data Fair : catalogue et jeux structurés dynamiques ;
- World Bank WDI : indicateurs Côte d’Ivoire ;
- ILOSTAT : statistiques du travail Côte d’Ivoire ;
- geoBoundaries : limites administratives GeoJSON ;
- OpenStreetMap/Geofabrik : snapshot PBF local avec contrôle de checksum ;
- FAOSTAT et UNESCO UIS : découverte des catalogues bulk et téléchargement sélectif ;
- ANStat/NADA : synchronisation des métadonnées publiques uniquement ;
- DGI, OHADA, CNPS, Justice, Santé, Éducation, Agriculture, ARTCI, Douanes, DGMP, SODEXAM, etc. : crawler public borné et respectueux de `robots.txt` ;
- scheduler local avec fréquence propre à chaque source ;
- SHA-256 / checkpoints / provenance ;
- CSV, JSON, JSONL, XLS/XLSX, Parquet, HTML, PDF ;
- requêtes SQL locales ;
- source ranking et cross-check ;
- recherche documentaire ;
- nettoyage, qualité et déduplication ;
- IvoireCorpus versionné et immuable ;
- tokenizer BPE optionnel ;
- FastAPI + CLI + Docker local ;
- CI GitHub pour valider le code uniquement.

## Installation

```bash
python -m pip install -e '.[dev]'
ivoiredata coverage
ivoiredata sources --public
ivoiredata status --public
```

Première synchronisation :

```bash
ivoiredata sync civ_datagouv_catalog
ivoiredata sync civ_worldbank_wdi
ivoiredata sync civ_ilostat
```

Synchronisation des sources arrivées à échéance :

```bash
ivoiredata scheduler --once
```

Scheduler permanent :

```bash
ivoiredata scheduler --interval 3600
```

## Stockage local

```text
data_lake/             tables et données normalisées dlt
  raw_external/        PBF/archives/fichiers volumineux sélectionnés
.ivoiredata/state/     fraîcheur et checkpoints
corpora/               versions immuables d’IvoireCorpus
tokenizer/             tokenizer local
```

Ces répertoires sont ignorés par Git : le dépôt contient le moteur et les règles, pas le corpus réel.

## API locale

```bash
uvicorn ivoiredata.api:app --host 127.0.0.1 --port 8000
```

Endpoints : `GET /health`, `GET /sources`, `GET /status`, `GET /coverage`, `POST /sync/{source_id}`, `GET /search/documents`, `POST /query/sql`.

## Corpus

```bash
ivoiredata corpus-build civ-0.1 TABLE1 TABLE2 --output corpora
python -m pip install -e '.[training]'
ivoiredata tokenizer-train corpora/civ-0.1 --vocab-size 32000
```

Une version de corpus utilisée pour un entraînement n’est jamais modifiée. Les nouvelles données servent à construire une version suivante.

## Documentation

| Document | Rôle |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | architecture complète et flux de données |
| [`docs/ENGINE.md`](docs/ENGINE.md) | fonctionnement interne du moteur |
| [`docs/CONNECTORS.md`](docs/CONNECTORS.md) | types de connecteurs et comportement |
| [`docs/SOURCES.md`](docs/SOURCES.md) | familles de sources, stratégie et fréquence |
| [`docs/SOURCE_COVERAGE.md`](docs/SOURCE_COVERAGE.md) | couverture réellement implémentée |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | exploitation quotidienne, sync, incidents |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | installation locale Windows/Linux/Docker |
| [`docs/STORAGE.md`](docs/STORAGE.md) | organisation des fichiers locaux |
| [`docs/CORPUS.md`](docs/CORPUS.md) | fabrication d’IvoireCorpus |
| [`docs/QUALITY_ASSURANCE.md`](docs/QUALITY_ASSURANCE.md) | qualité, conflits et déduplication |
| [`docs/RIGHTS_AND_ACCESS.md`](docs/RIGHTS_AND_ACCESS.md) | droits, accès public/contrôlé |
| [`docs/ADDING_SOURCE.md`](docs/ADDING_SOURCE.md) | ajouter une nouvelle source/connecteur |
| [`docs/DATAGOUV_ACCESS.md`](docs/DATAGOUV_ACCESS.md) | détail spécifique Data.gouv.ci |

Le registre machine-readable reste [`registry/sources.csv`](registry/sources.csv) et les fréquences d’exécution sont dans [`configs/runtime_sources.json`](configs/runtime_sources.json).

## Principes de sécurité et de provenance

IvoireData ne contourne ni authentification, ni CAPTCHA, ni paywall, ni autorisation. Pour une source `MIXED`, le moteur peut synchroniser automatiquement **uniquement les métadonnées publiques** lorsque `metadata_only=true`. Les fichiers soumis à des conditions de recherche ou à un accès contrôlé restent manuels.

Chaque donnée doit pouvoir être reliée à sa source, son URL et sa date de récupération ; les snapshots binaires conservent également un hash.
