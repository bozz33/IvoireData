# Architecture IvoireData v0.6

IvoireData est un moteur **local-only pour les données**. Git contient le code, le registre et la documentation ; le PC contient les payloads réels.

## Vue générale

```text
registry/sources.csv + configs/runtime_sources.json
                      │
                      ▼
                SourceRegistry
                      │
                Connector Router
                      │
      API / Web / PDF / Files / Bulk / Geo
                      │
                      ▼
                     dlt
                      │
             pipeline PAR SOURCE
                      │
                      ▼
 data_lake/domains/<domain>/<source_id>/
      ├── raw/
      ├── tables/          ← Parquet dlt
      ├── documents/
      └── manifest.json
                      │
                      ▼
             data_lake/catalog.json
                      │
        ┌─────────────┴──────────────┐
        ▼                            ▼
 API/CLI/SQL local          pipeline équipe modèle
                             clean/filter/dedup/...
```

## Pourquoi un pipeline par source

Avant v0.6, plusieurs sources partageaient le même dataset filesystem dlt. v0.6 isole chaque source afin de garantir :

- arborescence lisible ;
- état/checkpoints isolés ;
- évolution de schéma indépendante ;
- requêtes source par source ;
- suppression/restauration d'une source sans toucher les autres ;
- handoff direct au pipeline d'entraînement.

## Control plane

- `registry/sources.csv` : identité, producteur, domaine, URL, droits, priorité ;
- `configs/runtime_sources.json` : connecteur, fréquence, auto-sync, options ;
- `.ivoiredata/state/freshness.json` : dernier état opérationnel ;
- état dlt : état technique de chaque pipeline source ;
- `data_lake/catalog.json` : inventaire machine-readable du data lake.

Aucune base PostgreSQL n'est requise.

## Data plane

```text
data_lake/
├── catalog.json
└── domains/
    ├── agriculture/
    │   ├── civ_faostat/
    │   └── civ_agriculture_ministry/
    ├── health/
    ├── education/
    ├── economy/
    ├── geography/
    └── ...
```

Chaque package source contient :

- `raw/` : payload source original lorsque conservable ;
- `tables/` : tables normalisées dlt en Parquet ;
- `documents/` : pages/PDF/documentation collectés ;
- `manifest.json` : provenance, statut, fréquence et inventaire.

## Fraîcheur

```text
scheduler
  ↓
FreshnessStore.due(source)
  ├─ non → ne rien faire
  └─ oui
      ↓
   connector
      ↓
 hash/signature/checksum/state
      ↓
 source package
      ↓
 manifest.json + catalog.json
```

## Frontière avec l'entraînement

IvoireData **ne fabrique plus officiellement le corpus/tokenizer**. Il livre le data lake vivant et organisé. Le pipeline de l'équipe modèle fige ensuite un état du `catalog.json` et construit ses releases.

Voir :

- [`DATA_HANDOFF_CONTRACT.md`](DATA_HANDOFF_CONTRACT.md)
- [`DOWNSTREAM_AUTOMATION.md`](DOWNSTREAM_AUTOMATION.md)

## Accès contrôlé

Une source `MIXED` avec `metadata_only=true` autorise uniquement les pages/catalogues publics. Les routes et formats identifiés comme microdonnées sont filtrés avant téléchargement. Les sources `D_*` restent hors ingestion automatique.

## Query layer

Chaque pipeline filesystem dlt peut être interrogé localement via DuckDB/SQL. La CLI utilise :

```bash
ivoiredata query SOURCE_ID "SELECT ..."
```

Les tables sont stockées en Parquet pour faciliter aussi l'accès direct par d'autres outils.

## Extension

Ajouter une source = registre + runtime config + connecteur existant ou spécialisé + tests + documentation. Voir [`ADDING_SOURCE.md`](ADDING_SOURCE.md).
