# Références upstream officielles

Dernière vérification documentaire : **2026-08-09**.

| Système | Référence officielle | Usage IvoireData |
|---|---|---|
| dlt OSS | https://dlthub.com/docs/ | Extract/Normalize/Load |
| dlt filesystem | https://dlthub.com/docs/dlt-ecosystem/destinations/filesystem | data lake local / Parquet |
| Data Fair | https://data-fair.github.io/ | API derrière data.gouv.ci |
| data.gouv.ci | https://data.gouv.ci/ | open data national |
| World Bank API | https://datahelpdesk.worldbank.org/knowledgebase/topics/125589-developer-information | WDI |
| FAOSTAT | https://www.fao.org/faostat/ | agriculture, API et bulk download |
| FAOSTAT bulk host | https://bulks-faostat.fao.org/ | ZIP bulk officiels utilisés par `faostat_country` |
| ILOSTAT bulk/data | https://ilostat.ilo.org/data/bulk/ | statistiques du travail |
| UNESCO UIS resources | https://databrowser.uis.unesco.org/resources | Data API + Bulk Data Download Service |
| UNESCO UIS API documentation | https://api.uis.unesco.org/api/public/documentation/ | endpoints publics UIS |
| UNESCO UIS bulk | https://databrowser.uis.unesco.org/resources/bulk | archives CSV officielles |
| WHO GHO | https://www.who.int/data/gho/ | santé |
| geoBoundaries API | https://www.geoboundaries.org/api.html | limites administratives |
| Geofabrik Côte d’Ivoire | https://download.geofabrik.de/africa/ivory-coast.html | OSM PBF |
| ANStat NADA | https://nada.anstat.ci/index.php/catalog | métadonnées nationales |
| Hugging Face Tokenizers | https://huggingface.co/docs/tokenizers/en/training_from_memory | documentation downstream |
| NVIDIA NeMo pretraining data | https://docs.nvidia.com/nemo-framework/user-guide/25.09/data/pretrain_data.html | documentation downstream |
| NVIDIA Megatron Core datasets | https://docs.nvidia.com/megatron-core/developer-guide/latest/api-guide/core/datasets.html | documentation downstream |

## FAOSTAT v0.7

FAOSTAT annonce un accès libre à ses données agricoles et fournit à la fois un portail API et un service bulk. Le connecteur `faostat_country` utilise des ZIP bulk officiels sélectionnés, les conserve dans `raw/`, puis filtre les CSV normalisés sur la Côte d’Ivoire avant chargement Parquet.

Les URL de ZIP sont dans le code du connecteur et doivent être retestées lors d’un changement upstream. Une réponse ZIP valide ne suffit pas : le sync doit aussi confirmer la présence de lignes Côte d’Ivoire.

## UNESCO UIS v0.7

Le portail UIS Resources annonce explicitement une **Data API** pour accès programmatique et un **Bulk Data Download Service**. IvoireData utilise l’API publique `api.uis.unesco.org/api/public` avec `geoUnit=CIV` pour les séries, plus les endpoints de définitions.

Les données UIS sont publiques avec exigence d’attribution ; voir aussi les conditions d’utilisation UIS.

## ILOSTAT

Le connecteur v0.7 utilise le backend CSV pays. Le champ `obs_status` est un flag/statut d’observation et doit être conservé ; il ne sert pas de dimension de fréquence dans IvoireData.

## Règle de maintenance

Lorsqu’un upstream change :

1. vérifier la documentation officielle ;
2. tester l’endpoint vivant ;
3. modifier connecteur + tests ;
4. lancer un sync réel ciblé ;
5. vérifier `ivoiredata audit` ;
6. conserver l’ancienne livraison locale tant que la nouvelle n’est pas validée ;
7. ne jamais basculer silencieusement vers un miroir non officiel.

## Pipeline downstream

Les références Hugging Face/NVIDIA servent uniquement de documentation à l’équipe modèle. Le moteur de collecte n’exécute pas officiellement corpus/tokenizer/training. Voir [`DOWNSTREAM_AUTOMATION.md`](DOWNSTREAM_AUTOMATION.md).
