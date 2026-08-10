# IvoireData 🇨🇮

**v0.7.0 — moteur local de collecte, mise à jour, audit et livraison de données classées par domaine, construit sur dlt OSS**

IvoireData collecte les sources publiques utiles à la Côte d’Ivoire, conserve leur provenance, détecte les mises à jour et livre les données localement **par domaine puis par source**. Les données réelles restent sur la machine : GitHub ne contient que le code, la configuration et la documentation.

## Frontière de responsabilité

```text
Internet / APIs / sites officiels / PDF / CSV / ZIP / PBF
                           │
                           ▼
                       IvoireData
                           │
       collecte / update / provenance / audit / classement
                           │
                           ▼
                  data_lake/domains/
                           │
                           ▼
                pipeline équipe modèle
                           │
      nettoyage / filtres / PII / dédup / corpus
                           │
                 tokenizer / shards
                           │
                           ▼
                      entraînement
```

IvoireData s’arrête au data lake. La chaîne downstream est documentée dans [`docs/DOWNSTREAM_AUTOMATION.md`](docs/DOWNSTREAM_AUTOMATION.md) et son contrat d’entrée dans [`docs/DATA_HANDOFF_CONTRACT.md`](docs/DATA_HANDOFF_CONTRACT.md).

## Stockage local

```text
data_lake/
├── catalog.json
└── domains/
    └── <domain>/
        └── <source_id>/
            ├── raw/
            ├── tables/
            ├── documents/
            └── manifest.json

.ivoiredata/state/
└── freshness.json
```

Les tables normalisées sont en Parquet. PostgreSQL, S3, R2 et MinIO ne sont pas requis pour la v0.7.

## Un `success` ne signifie plus « données disponibles »

Le manifest v2 sépare quatre dimensions :

- `sync.status` : `SUCCESS` / `ERROR` ;
- `delivery.status` : `FULL_STRUCTURED`, `DOCUMENTS_ONLY`, `SNAPSHOT_ONLY`, `METADATA_ONLY`, `EMPTY` ;
- `freshness.status` : `FRESH`, `DUE`, `STALE`, `NEVER_SYNCED` ;
- `transport.security` : `VERIFIED_TLS`, `DEGRADED_TLS`, `HTTP`.

Exemples de warnings : `EMPTY_AFTER_SUCCESS`, `SYNC_ERROR_WITH_STALE_DATA`, `TLS_VERIFICATION_DISABLED`, `METADATA_ONLY_SOURCE`.

Les lignes Parquet sont comptées via leurs **métadonnées**, sans relire les datasets complets.

## Audit

```bash
ivoiredata audit
```

renvoie pour chaque source : statut de sync, niveau réel de livraison, fraîcheur, sécurité transport, nombre de lignes, fichiers raw/tables/documents et warnings.

API équivalente :

```text
GET /audit
```

Voir [`docs/AUDIT.md`](docs/AUDIT.md).

## Connecteurs structurés principaux

- `data_gouv_ci` : catalogue Data.gouv.ci + datasets `/full`, raw + Parquet ;
- `world_bank_wdi` : World Bank WDI, JSON + Parquet ;
- `world_bank_projects` : projets World Bank Côte d’Ivoire via API de recherche ;
- `ilostat_ref_area` : backend CSV ILOSTAT filtré `ref_area=CIV`, sans RDS/pyreadr ;
- `faostat_country` : ZIP bulk FAOSTAT officiels, snapshots raw, filtrage Côte d’Ivoire, Parquet par domaine FAOSTAT ;
- `uis_country` : UIS Data API, définitions + séries `geoUnit=CIV`, raw JSON + Parquet ;
- `geoboundaries` : limites administratives ;
- `osm_geofabrik` : snapshot PBF Côte d’Ivoire ;
- `public_web` : sites/PDF institutionnels bornés, même domaine, robots.txt ;
- `http_file` : CSV/JSON/JSONL/XLS/XLSX/Parquet directs.

**FAOSTAT et UIS v0.7 doivent être resynchronisés sur la machine locale après mise à jour afin de confirmer leur volume réel.** Le code/CI valide les connecteurs hors réseau ; seul le sync local valide l’upstream vivant et matérialise les données.

## Installation / mise à jour

```bash
git pull
python -m pip install -e '.[dev]'

ivoiredata coverage
ivoiredata sources --public
ivoiredata audit
```

Docker :

```bash
docker compose build
docker compose --profile run up -d
```

## Validation ciblée v0.7

Après mise à jour, exécuter :

```bash
ivoiredata sync civ_ilostat --force
ivoiredata sync civ_faostat --force
ivoiredata sync civ_uis --force
ivoiredata sync civ_worldbank_projects --force
ivoiredata audit
```

Puis, pour toutes les sources publiques :

```bash
ivoiredata sync --all-public --force
ivoiredata audit
```

Une source ne doit être déclarée réellement couverte que si `delivery_status != EMPTY` et que son contenu correspond au niveau attendu.

## API locale

```bash
uvicorn ivoiredata.api:app --host 127.0.0.1 --port 8000
```

Endpoints principaux : `/health`, `/sources`, `/status`, `/coverage`, `/audit`, `/inventory`, `/sync/{source_id}`, `/query/source/{source_id}`.

## Documentation

| Document | Rôle |
|---|---|
| [`docs/USAGE_GUIDE.md`](docs/USAGE_GUIDE.md) | guide complet : installation, Docker, sync, audit, API, sauvegarde, diagnostic et handoff |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | architecture et flux |
| [`docs/ENGINE.md`](docs/ENGINE.md) | moteur interne |
| [`docs/CONNECTORS.md`](docs/CONNECTORS.md) | connecteurs |
| [`docs/SOURCES.md`](docs/SOURCES.md) | familles de sources |
| [`docs/UPSTREAM_SOURCES.md`](docs/UPSTREAM_SOURCES.md) | références upstream |
| [`docs/SOURCE_COVERAGE.md`](docs/SOURCE_COVERAGE.md) | couverture et validation live |
| [`docs/AUDIT.md`](docs/AUDIT.md) | contrat d’audit v0.7 |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | exploitation quotidienne |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | installation/Docker |
| [`docs/STORAGE.md`](docs/STORAGE.md) | stockage local |
| [`docs/DATA_HANDOFF_CONTRACT.md`](docs/DATA_HANDOFF_CONTRACT.md) | handoff équipe modèle |
| [`docs/DOWNSTREAM_AUTOMATION.md`](docs/DOWNSTREAM_AUTOMATION.md) | nettoyage → corpus → tokenizer → training |
| [`docs/RIGHTS_AND_ACCESS.md`](docs/RIGHTS_AND_ACCESS.md) | droits et accès |
| [`docs/ADDING_SOURCE.md`](docs/ADDING_SOURCE.md) | ajout d’une source |

## Principes

1. GitHub ne contient jamais le data lake réel.
2. Pas de contournement d’authentification, CAPTCHA, paywall ou contrôle d’accès.
3. Les microdonnées contrôlées restent exclues de l’ingestion automatique.
4. Provenance, URL et SHA-256 sont conservés lorsque possible.
5. Une erreur upstream ne doit pas supprimer la dernière livraison valide.
6. `success` technique et livraison exploitable sont toujours mesurés séparément.