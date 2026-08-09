# IvoireData Engine v0.6.0

IvoireData utilise **dlt OSS** comme moteur Extract/Normalize/Load. IvoireData ajoute : registre des sources, routage, politiques de fraîcheur, provenance, snapshots locaux, classement par domaine/source, manifests et catalogue global.

## Responsabilité officielle

```text
source officielle
→ acquisition
→ conservation brute lorsque appropriée
→ normalisation dlt
→ Parquet
→ classement domaine/source
→ manifest
→ catalog global
→ mise à jour automatique
```

Le nettoyage ML avancé, les filtres PII du corpus, la déduplication fuzzy, la mixture, le tokenizer, le packing et les shards d'entraînement sont documentés mais exécutés par le pipeline de l'équipe modèle.

## Composants

### `Settings`

Définit `data_lake/`, l'état local, le registre et la configuration runtime.

### `SourceRegistry`

Fusionne :

1. `registry/sources.csv` ;
2. `configs/runtime_sources.json` ;
3. les options runtime de chaque source.

### `IvoireDataEngine`

Responsabilités :

- résoudre la source ;
- appliquer la politique d'accès ;
- créer son arborescence ;
- choisir le connecteur ;
- lancer un **pipeline dlt isolé par source** ;
- charger les tables en Parquet ;
- enregistrer succès/erreur ;
- mettre à jour `manifest.json` ;
- reconstruire `data_lake/catalog.json`.

### `delivery.py`

Définit le contrat physique :

```text
data_lake/domains/<domain>/<source_id>/
├── raw/
├── tables/
├── documents/
└── manifest.json
```

### `snapshots.py`

Archive certains payloads bruts sous nom content-addressed et écrit un sidecar `.meta.json` contenant provenance, MIME, taille, date et SHA-256.

### `FreshnessStore`

`.ivoiredata/state/freshness.json` contient les derniers essais/succès. `refresh_hours` décide quand une source doit être revue.

### dlt

Chaque source dispose de sa propre destination filesystem locale et de son propre état dlt. Le format normalisé de v0.6 est **Parquet**.

## Connecteurs spécialisés

- `data_gouv_ci` : catalogue Data Fair + datasets CSV bruts + tables ;
- `world_bank_wdi` : API WDI + réponses JSON archivées ;
- `ilostat_ref_area` : RDS CIV archivé + tables ;
- `geoboundaries` ;
- `osm_geofabrik` : PBF/GPKG/SHP local avec checksum.

## Connecteurs génériques

- `http_file` : CSV/JSON/JSONL/XLS/XLSX/Parquet + snapshot ;
- `bulk_catalog` : découverte + téléchargements explicitement sélectionnés ;
- `public_web` : HTML/PDF/text, crawler borné et `robots.txt`, snapshots documentaires.

Voir [`CONNECTORS.md`](CONNECTORS.md).

## Mise à jour

Deux niveaux :

1. le scheduler n'appelle une source que lorsque `refresh_hours` l'exige ;
2. le connecteur compare hash/signature/checksum/état pour éviter le retraitement inutile.

## Permissions

`SourceSpec.public` signifie « autorisé par la politique IvoireData pour ingestion automatique ».

- `OPEN` / `OPEN_PUBLIC` : automatique possible ;
- `MIXED` : uniquement avec `metadata_only=true` ;
- `D_*` : non automatique.

## CLI

```bash
ivoiredata sources --public
ivoiredata coverage
ivoiredata inventory
ivoiredata status --public
ivoiredata source-path SOURCE_ID
ivoiredata sync SOURCE_ID
ivoiredata sync --due
ivoiredata scheduler --once
ivoiredata scheduler --interval 3600
ivoiredata query SOURCE_ID "SELECT ..."
```

## API

FastAPI expose santé, sources, statut, couverture, inventaire, chemin d'une source, synchronisation, recherche documentaire et requêtes SQL par source.

## Handoff entraînement

- [`DATA_HANDOFF_CONTRACT.md`](DATA_HANDOFF_CONTRACT.md) : contrat physique/logique ;
- [`DOWNSTREAM_AUTOMATION.md`](DOWNSTREAM_AUTOMATION.md) : nettoyage → filtres → PII → qualité → dedup → corpus → tokenizer → packing/sharding → loader.

## Limites assumées

- pas de DB serveur ;
- pas de contournement d'accès ;
- les microdonnées contrôlées restent hors auto-sync ;
- certains portails demandent encore un connecteur spécialisé pour une extraction exhaustive ;
- un catalogue bulk n'est pas équivalent à « tout télécharger » : les gros payloads restent soumis à une sélection explicite afin de protéger le stockage local et respecter les conditions de la source.
