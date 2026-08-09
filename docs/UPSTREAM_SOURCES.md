# Références upstream officielles

Dernière vérification documentaire : **2026-08-09**.

Ce fichier conserve les références utilisées pour concevoir les connecteurs **et** les références techniques utilisées par le guide downstream. Les URLs de données effectives restent dans `registry/sources.csv` et les connecteurs.

| Système | Référence officielle | Usage |
|---|---|---|
| dlt OSS | https://dlthub.com/docs/ | moteur Extract/Normalize/Load et destination filesystem |
| dlt filesystem | https://dlthub.com/docs/dlt-ecosystem/destinations/filesystem | data lake local, Parquet/JSONL, état et SQL DuckDB |
| Data Fair | https://data-fair.github.io/ | modèle d’API derrière data.gouv.ci |
| data.gouv.ci | https://data.gouv.ci/ | open data national |
| World Bank API | https://datahelpdesk.worldbank.org/knowledgebase/topics/125589-developer-information | WDI / données structurées |
| FAOSTAT | https://www.fao.org/faostat/ | agriculture, API/bulk catalog |
| ILOSTAT bulk | https://ilostat.ilo.org/data/bulk/ | statistiques du travail |
| UNESCO UIS resources | https://databrowser.uis.unesco.org/resources | API et bulk download |
| UNESCO UIS bulk | https://databrowser.uis.unesco.org/resources/bulk | archives CSV officielles |
| WHO GHO | https://www.who.int/data/gho/ | santé |
| WHO API transition | https://www.who.int/data/gho/legacy | suivi de la transition des interfaces WHO |
| geoBoundaries API | https://www.geoboundaries.org/api.html | limites administratives |
| Geofabrik Côte d’Ivoire | https://download.geofabrik.de/africa/ivory-coast.html | snapshot OpenStreetMap PBF/GPKG/SHP |
| ANStat NADA | https://nada.anstat.ci/index.php/catalog | métadonnées statistiques nationales |
| Hugging Face Tokenizers | https://huggingface.co/docs/tokenizers/en/training_from_memory | exemple d’entraînement tokenizer depuis itérateur/fichiers |
| Tokenizers components | https://huggingface.co/docs/tokenizers/v0.22.2/en/components | normalizers, pre-tokenizers, trainers |
| NVIDIA NeMo pretraining data | https://docs.nvidia.com/nemo-framework/user-guide/25.09/data/pretrain_data.html | conversion texte/tokenisé pour pré-entraînement |
| NVIDIA Megatron Core datasets | https://docs.nvidia.com/megatron-core/developer-guide/latest/api-guide/core/datasets.html | IndexedDataset, `.bin/.idx`, loaders et mélange de datasets |

## Règle de maintenance

Lorsqu’une API ou une page de téléchargement change :

1. vérifier en priorité la documentation officielle du producteur ;
2. modifier le connecteur et les tests ;
3. mettre à jour ce fichier si l’URL de référence change ;
4. ne jamais basculer silencieusement vers une source non officielle lorsque la source officielle est indisponible ;
5. conserver l’ancienne donnée locale jusqu’à ce qu’une nouvelle synchronisation soit validée.

## WHO

La documentation WHO indique que l’ancienne interface Athena est retirée et que l’interface OData GHO historique devait être remplacée. Pour cette raison, IvoireData ne fige pas un endpoint WHO en transition sans validation : la source WHO reste suivie via le portail public tant qu’un connecteur API spécialisé actuel n’a pas été validé.

## Bulk catalogs

FAOSTAT et UIS peuvent exposer des fichiers très volumineux. IvoireData suit leurs catalogues automatiquement mais exige une sélection (`download_patterns`, `max_downloads`, `max_bytes`) avant de matérialiser de gros fichiers. Cette règle protège le disque local et rend la composition de la collecte explicite.

## Pipeline downstream

Les références Hugging Face/NVIDIA ne rendent pas ces frameworks obligatoires. [`DOWNSTREAM_AUTOMATION.md`](DOWNSTREAM_AUTOMATION.md) définit une chaîne indépendante du framework : nettoyage, filtres, PII, qualité, déduplication, contamination, mixture, release corpus, tokenizer, tokenisation, packing, sharding puis adapter final vers le framework réellement utilisé par l’équipe modèle.
