# Stockage local v0.6

IvoireData utilise uniquement le disque local. **Aucun PostgreSQL, S3, R2 ou MinIO n'est requis.**

## Arborescence officielle

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

## `raw/`

Payloads sources conservés localement lorsque cela est approprié : CSV/JSON/XLSX/RDS/PBF/archives. Les fichiers archivés par le helper de snapshot utilisent un nom content-addressed contenant un préfixe SHA-256 et un sidecar `.meta.json`.

## `tables/`

Chaque source possède une destination filesystem dlt isolée. Les tables normalisées sont chargées en **Parquet** afin d'être lisibles directement par DuckDB, pandas, PyArrow ou un autre pipeline.

Les sous-dossiers techniques `_dlt_*` appartiennent à dlt et permettent de restaurer l'état et le schéma du pipeline source.

## `documents/`

Pages HTML, PDF et autres documents textuels collectés par les connecteurs Web sont archivés ici avec provenance/hash lorsque le mode d'accès le permet.

## `manifest.json`

Résumé local de la source : identité, domaine, provider, URL, droits, connecteur, dates, statut et inventaire des dossiers.

## `catalog.json`

Index global de toutes les sources. C'est le premier fichier à lire pour un consommateur externe.

```bash
ivoiredata inventory
```

Voir [`DATA_HANDOFF_CONTRACT.md`](DATA_HANDOFF_CONTRACT.md).

## Pourquoi pas de DB serveur

Le produit principal est un data lake de fichiers :

- facile à inspecter ;
- facile à sauvegarder ;
- compatible avec les pipelines ML ;
- pas de serveur à maintenir ;
- isolation naturelle par domaine/source ;
- Parquet est directement exploitable analytiquement.

Une DB serveur ne devient utile que si plusieurs machines/utilisateurs doivent écrire simultanément ou si une application transactionnelle multi-utilisateur est ajoutée plus tard.

## Sauvegarde

Sauvegarder au minimum :

```text
data_lake/
.ivoiredata/
```

sur un second disque. Le code/configuration reste dans GitHub et peut être recloné.

Le pipeline de l'équipe modèle doit stocker ses propres snapshots/releases hors du `data_lake` vivant.
