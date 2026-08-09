# Références upstream officielles

Dernière vérification documentaire : **2026-08-09**.

Ce fichier conserve les références utilisées pour concevoir les connecteurs. Les URLs de données effectives restent dans `registry/sources.csv` et les connecteurs.

| Système | Référence officielle | Usage IvoireData |
|---|---|---|
| dlt OSS | https://dlthub.com/docs/ | moteur Extract/Normalize/Load et destination filesystem |
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

## Règle de maintenance

Lorsqu’une API ou une page de téléchargement change :

1. vérifier en priorité la documentation officielle du producteur ;
2. modifier le connecteur et les tests ;
3. mettre à jour ce fichier si l’URL de référence change ;
4. ne jamais basculer silencieusement vers une source non officielle lorsque la source officielle est indisponible ;
5. conserver l’ancienne donnée locale jusqu’à ce qu’une nouvelle synchronisation soit validée.

## WHO

La documentation WHO indique que l’ancienne interface Athena est retirée et que l’interface OData GHO historique devait être remplacée. Pour cette raison, IvoireData v0.5 ne fige pas un endpoint WHO en transition : la source WHO est actuellement suivie via le portail public. Un connecteur API spécialisé ne doit être activé qu’après validation de l’interface officielle actuelle.

## Bulk catalogs

FAOSTAT et UIS peuvent exposer des fichiers très volumineux. IvoireData suit leurs catalogues automatiquement mais exige une sélection (`download_patterns`, `max_downloads`, `max_bytes`) avant de matérialiser de gros fichiers. Cette règle protège le disque local et rend la composition du corpus explicite.
