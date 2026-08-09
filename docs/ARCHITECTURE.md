# Architecture IvoireData v0.5

IvoireData est un moteur **local-first**. Git contient le code et les règles ; le PC contient les données.

## Vue générale

```text
                    registry/sources.csv
                           │
               configs/runtime_sources.json
                           │
                           ▼
                    SourceRegistry
                           │
                    Connector Router
                           │
       ┌───────────────────┼──────────────────────────────┐
       │                   │                              │
 API structurées      sites/documents              gros snapshots
 Data Fair/WB/ILO     HTML/PDF/bulk                OSM/archives
       │                   │                              │
       └───────────────────┼──────────────────────────────┘
                           ▼
                          dlt
                           │
              Extract → Normalize → Load
                           │
              ┌────────────┴────────────┐
              │                         │
        data_lake/                .ivoiredata/
     tables + raw_external        état/fraîcheur
              │
       ┌──────┼──────────────┐
       │      │              │
      SQL   Search       Corpus Factory
                             │
                    clean → quality → dedup
                             │
                         corpora/
                             │
                        tokenizer/
                             │
                       pré-entraînement
```

## Control plane

Le control plane est constitué de petits fichiers Git :

- `registry/sources.csv` : identité, producteur, domaine, URL, droits, priorité ;
- `configs/runtime_sources.json` : connecteur, fréquence, auto-sync, options ;
- `.ivoiredata/state/freshness.json` : état runtime local, hors Git ;
- état dlt : checkpoints et métadonnées techniques.

Aucune base serveur n’est requise.

## Data plane

`data_lake/` contient les payloads réellement acquis. Les gros fichiers spécifiques sont rangés dans `data_lake/raw_external/<source_id>/`.

Exemples :

```text
data_lake/
├── ivoiredata/              tables dlt
└── raw_external/
    ├── civ_osm_geofabrik/
    ├── civ_faostat/
    └── civ_uis/
```

## Fraîcheur

Le scheduler local ne télécharge pas toutes les sources à chaque réveil :

```text
scheduler
   │
   ▼
FreshnessStore.due(source)
   │
   ├── non → rien
   └── oui → connector → dlt → succès/erreur → freshness.json
```

Les connecteurs utilisent signatures, hash, checksum ou état dlt pour éviter le retraitement inutile.

## Séparation entraînement / données fraîches

Le moteur reste dynamique, le corpus d’entraînement reste figé :

```text
sources mises à jour → data_lake courant
                           │
                           ▼
                    corpus-build civ-0.1
                           │
                     corpus immutable
                           │
                       training

nouvelles sources → data_lake courant → corpus-build civ-0.2
```

## Accès contrôlé

Une source `MIXED` n’autorise pas automatiquement ses microdonnées. `metadata_only=true` permet seulement la récupération du catalogue et des métadonnées publiques. Les droits `D_*` restent bloqués pour l’ingestion automatique.

## Query layer

Le pipeline filesystem dlt expose un client SQL local. L’API et la CLI utilisent cette couche pour lire les tables sans PostgreSQL.

## Extension

L’architecture est modulaire : ajouter une API nécessite généralement un connecteur + une entrée de configuration, pas une modification du corpus ou du scheduler. Voir [`CONNECTORS.md`](CONNECTORS.md) et [`ADDING_SOURCE.md`](ADDING_SOURCE.md).
