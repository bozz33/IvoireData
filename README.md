# IvoireData 🇨🇮

**v0.6.0 — moteur local de collecte, mise à jour et livraison de données classées par domaine, construit sur dlt OSS**

IvoireData est l’infrastructure de données destinée à alimenter les projets IA : il découvre les sources, récupère les vraies données publiques, conserve leur provenance, détecte les mises à jour et les range localement **par domaine puis par source**.

**Toutes les données réelles restent sur le PC.** Aucun S3, R2 ou MinIO n’est nécessaire. PostgreSQL n’est pas requis : les gros volumes restent en fichiers locaux et les tables normalisées sont produites en Parquet.

## Frontière de responsabilité

IvoireData s’arrête à la livraison du data lake :

```text
Internet / APIs / sites officiels / PDF / CSV / XLSX / PBF
                           │
                           ▼
                       IvoireData
                           │
          collecte / update / provenance / classement
                           │
                           ▼
                  data_lake/domains/
                           │
                           ▼
                pipeline équipe modèle
                           │
      nettoyage avancé / filtres / PII / dédup
                           │
                           ▼
               corpus / tokenizer / shards
                           │
                           ▼
                      entraînement
```

La partie corpus/tokenizer n’est plus l’interface opérationnelle principale d’IvoireData. Elle est documentée pour l’équipe modèle dans [`docs/DOWNSTREAM_AUTOMATION.md`](docs/DOWNSTREAM_AUTOMATION.md).

## Architecture locale v0.6

```text
data_lake/
├── catalog.json
└── domains/
    ├── agriculture/
    │   ├── civ_faostat/
    │   │   ├── raw/
    │   │   ├── tables/
    │   │   ├── documents/
    │   │   └── manifest.json
    │   └── ...
    ├── health/
    ├── education/
    ├── economy/
    ├── geography/
    └── ...

.ivoiredata/state/
└── fraîcheur/checkpoints
```

Chaque source a son propre pipeline dlt, son propre état, son dossier de données et son `manifest.json`. `data_lake/catalog.json` fournit l’index global consommable par un autre projet.

## Connecteurs

- Data.gouv.ci / Data Fair : catalogue et datasets structurés + snapshots CSV locaux ;
- World Bank WDI : API structurée + réponses JSON archivées ;
- ILOSTAT : statistiques CIV + RDS source archivé ;
- geoBoundaries : limites administratives ;
- OpenStreetMap/Geofabrik : snapshot PBF local avec checksum ;
- FAOSTAT et UNESCO UIS : catalogues bulk et téléchargement sélectif ;
- ANStat/NADA : métadonnées publiques uniquement lorsque l’accès aux microdonnées est contrôlé ;
- DGI, OHADA, CNPS, Justice, Santé, Éducation, Agriculture, ARTCI, Douanes, DGMP, SODEXAM, etc. : crawler public borné, même domaine et respect `robots.txt` ;
- CSV, JSON, JSONL, XLS/XLSX, Parquet, HTML, PDF.

Les payloads récupérés sont conservés dans `raw/` ou `documents/` lorsque cela est approprié ; les représentations structurées sont stockées dans `tables/` au format Parquet.

## Installation

```bash
python -m pip install -e '.[dev]'
ivoiredata coverage
ivoiredata sources --public
ivoiredata inventory
```

Synchroniser une source :

```bash
ivoiredata sync civ_worldbank_wdi
```

Synchroniser les sources arrivées à échéance :

```bash
ivoiredata scheduler --once
```

Scheduler local permanent :

```bash
ivoiredata scheduler --interval 3600
```

Trouver le dossier d’une source :

```bash
ivoiredata source-path civ_worldbank_wdi
```

## Manifest et catalogue

Après synchronisation :

```text
data_lake/domains/<domain>/<source_id>/manifest.json
```

contient notamment : source, domaine, provider, URL, droits, connecteur, statut, dates et inventaire local.

Le fichier :

```text
data_lake/catalog.json
```

regroupe toutes les sources et sert de **contrat de handoff** au projet d’entraînement.

Voir [`docs/DATA_HANDOFF_CONTRACT.md`](docs/DATA_HANDOFF_CONTRACT.md).

## API locale

```bash
uvicorn ivoiredata.api:app --host 127.0.0.1 --port 8000
```

L’API expose l’état, les sources, la couverture et les synchronisations. Elle reste optionnelle : le data lake local est l’artefact principal.

## Documentation

| Document | Rôle |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | architecture complète et flux de données |
| [`docs/ENGINE.md`](docs/ENGINE.md) | fonctionnement interne du moteur |
| [`docs/CONNECTORS.md`](docs/CONNECTORS.md) | types de connecteurs et comportement |
| [`docs/SOURCES.md`](docs/SOURCES.md) | familles de sources, stratégie et fréquence |
| [`docs/UPSTREAM_SOURCES.md`](docs/UPSTREAM_SOURCES.md) | références officielles upstream |
| [`docs/SOURCE_COVERAGE.md`](docs/SOURCE_COVERAGE.md) | couverture réellement implémentée |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | exploitation quotidienne, sync, incidents |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | installation locale Windows/Linux/Docker |
| [`docs/STORAGE.md`](docs/STORAGE.md) | organisation des fichiers locaux |
| [`docs/DATA_HANDOFF_CONTRACT.md`](docs/DATA_HANDOFF_CONTRACT.md) | contrat IvoireData → équipe modèle |
| [`docs/DOWNSTREAM_AUTOMATION.md`](docs/DOWNSTREAM_AUTOMATION.md) | automatisation nettoyage → filtres → corpus → tokenizer → shards |
| [`docs/CORPUS.md`](docs/CORPUS.md) | frontière de responsabilité corpus |
| [`docs/QUALITY_ASSURANCE.md`](docs/QUALITY_ASSURANCE.md) | qualité des données du moteur |
| [`docs/RIGHTS_AND_ACCESS.md`](docs/RIGHTS_AND_ACCESS.md) | droits et accès public/contrôlé |
| [`docs/ADDING_SOURCE.md`](docs/ADDING_SOURCE.md) | ajouter une nouvelle source/connecteur |
| [`docs/DATAGOUV_ACCESS.md`](docs/DATAGOUV_ACCESS.md) | détail Data.gouv.ci |

Le registre machine-readable reste [`registry/sources.csv`](registry/sources.csv) et les fréquences d’exécution sont dans [`configs/runtime_sources.json`](configs/runtime_sources.json).

## Principes

1. GitHub contient le code/config/docs, jamais le data lake réel.
2. Pas de contournement d’authentification, CAPTCHA, paywall ou contrôle par rôle.
3. Une source `MIXED` peut être `metadata_only`, sans téléchargement de microdonnées contrôlées.
4. Les données gardent leur provenance et, lorsque possible, un SHA-256.
5. Une source défaillante ne doit pas casser le stockage des autres : pipelines et dossiers séparés.
6. IvoireData reste vivant et actualisé ; le pipeline de l’équipe modèle choisit ensuite quand figer un snapshot pour construire un corpus.
