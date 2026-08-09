# Catalogue des sources

Le fichier de référence machine-readable est [`registry/sources.csv`](../registry/sources.csv). Les fréquences, connecteurs et options d’exécution sont dans [`configs/runtime_sources.json`](../configs/runtime_sources.json).

Ce document explique comment les principales familles sont exploitées. Il ne remplace pas les conditions d’utilisation propres à chaque producteur.

## Niveaux d’accès

| Niveau | Comportement IvoireData |
|---|---|
| `OPEN` | données publiques synchronisables |
| `OPEN_PUBLIC` | pages/documents publics synchronisables |
| `MIXED` | données non synchronisées par défaut ; métadonnées publiques possibles avec `metadata_only=true` |
| accès recherche/contrôlé | pas de téléchargement automatique des microdonnées |

Les droits `D_*` sont toujours exclus de l’ingestion automatique.

## Sources structurées prioritaires

| Source ID | Producteur | Domaine | Connecteur | Fréquence indicative | Contenu local |
|---|---|---|---|---:|---|
| `civ_datagouv_catalog` | data.gouv.ci | multidomaine | `data_gouv_ci` | 24 h | catalogue + datasets accessibles |
| `civ_worldbank_wdi` | World Bank | multidomaine | `world_bank_wdi` | 168 h | indicateurs CIV |
| `civ_ilostat` | ILOSTAT | emploi/travail | `ilostat_ref_area` | 168 h | séries CIV annuelles |
| `civ_geoboundaries` | geoBoundaries | géographie | `geoboundaries` | 720 h | GeoJSON + métadonnées |
| `civ_osm_geofabrik` | OSM/Geofabrik | géographie | `osm_geofabrik` | 24 h | snapshot PBF local |

## Catalogues bulk internationaux

| Source ID | Producteur | Domaine | Mode par défaut | Remarque |
|---|---|---|---|---|
| `civ_faostat` | FAO/FAOSTAT | agriculture | `bulk_catalog` | catalogue automatique ; gros fichiers téléchargés seulement sur sélection |
| `civ_uis` | UNESCO UIS | éducation | `bulk_catalog` | catalogue bulk automatique ; sélection de fichiers configurable |
| `civ_who_profile` | WHO | santé | `public_web` | portail/documentation actuelle, la couche API WHO étant en transition |
| `civ_worldbank_projects` | World Bank | économie/projets | `public_web` | documentation et pages publiques ; connecteur spécialisé futur possible |
| `civ_worldbank_climate` | World Bank | climat | `public_web` | documentation et pages publiques ; connecteur spécialisé futur possible |

## ANStat / NADA

| Source ID | Accès | Politique |
|---|---|---|
| `civ_anstat_nada` | `MIXED` | synchronisation automatique **des métadonnées publiques seulement** |
| `civ_anstat_eaa_2024_2025` | accès à revoir selon conditions du dataset | jamais téléchargé automatiquement comme microdonnée |

IvoireData peut indexer les pages de catalogue, descriptions, dictionnaires et liens publics sans considérer cela comme une autorisation de republier les fichiers de microdonnées.

## Institutions ivoiriennes synchronisées par crawler public

| Source ID | Producteur | Domaine | Fréquence indicative |
|---|---|---|---:|
| `civ_dgi` | Direction Générale des Impôts | fiscalité/FNE | 24 h |
| `civ_dgmp` | DGMP | marchés publics | 24 h |
| `civ_sodexam` | SODEXAM | météo/climat | 24 h |
| `civ_ohada` | OHADA | droit des affaires | 72 h |
| `civ_agriculture_ministry` | Ministère Agriculture | agriculture | 72 h |
| `civ_mena` | Ministère Éducation | éducation | 72 h |
| `civ_health_rass` | Ministère Santé | santé | 72 h |
| `civ_artci` | ARTCI | télécom/TIC | 72 h |
| `civ_customs` | Douanes | commerce/statistiques | 72 h |
| `civ_cnps` | CNPS | protection sociale | 72 h |
| `civ_justice` | Ministère Justice | justice/droit | 72 h |
| `civ_bceao_bdef` | BCEAO | économie/finance | 168 h |
| `civ_health_e_depps` | DEPPS | santé | 168 h |
| `civ_treasury_debt` | Trésor | dette publique | 168 h |
| `civ_apif` | APIF | inclusion financière | 168 h |
| `civ_servicepublic` | Service Public | administration | 168 h |
| `civ_transport` | Ministère Transports | transport | 168 h |
| `civ_environment` | Ministère Environnement | environnement | 168 h |
| `civ_environment_sie` | SIE | environnement | 168 h |
| `civ_mining` | Ministère Mines | mines | 168 h |
| `civ_hydrocarbon` | Ministère Énergie | pétrole/gaz | 168 h |
| `civ_mnv_energy` | Direction Générale Énergie | énergie/climat | 168 h |
| `civ_idufci` | Construction/MULCV | foncier | 168 h |
| `civ_construction_services` | Construction/MULCV | urbanisme/logement | 168 h |
| `civ_onad` | ONAD | assainissement | 168 h |
| `civ_pnd_2026_2030` | Ministère du Plan | gouvernance/planification | 720 h |
| `civ_onep` | ONEP/DGPE | eau potable | 720 h |

Le crawler est borné par `max_pages`, reste sur le même domaine et respecte `robots.txt`.

## Jeux Data.gouv.ci explicitement référencés

Le registre contient aussi plusieurs entrées Data.gouv.ci prioritaires :

- RGPH 2021 ;
- circonscriptions administratives et communes ;
- importations/exportations ;
- population urbaine ;
- BAC ;
- cacao/café ;
- pluviométrie ;
- hévéa ;
- insécurité alimentaire ;
- prix bord champ et prix vivriers ;
- productions forestières ;
- émissions GES ;
- ménages agricoles ;
- pétrole/gaz ;
- télévision/bouquets autorisés ;
- cheptel/lait ;
- autres jeux ajoutés au catalogue Data Fair.

Le connecteur `civ_datagouv_catalog` découvre dynamiquement le catalogue : il n’est donc pas nécessaire d’ajouter manuellement une entrée pour chaque nouveau dataset pour qu’il soit visible.

## Sources contrôlées ou à conditions spécifiques

Exemples : microdonnées d’enquêtes, certains recensements ou fichiers de recherche. Elles peuvent être référencées dans le registre pour la découverte et la provenance, mais ne sont pas automatiquement intégrées au corpus tant que leurs conditions ne le permettent pas.

## Fraîcheur

`refresh_hours` indique quand une source doit être **revérifiée**, pas qu’elle est forcément modifiée. Le scheduler :

1. vérifie si la source est arrivée à échéance ;
2. appelle le connecteur ;
3. compare signature/hash/checkpoint lorsque le connecteur le permet ;
4. ne remplace le contenu local que si nécessaire ;
5. enregistre la date du dernier succès dans `.ivoiredata/state/freshness.json`.

## Ajouter ou modifier une source

Voir [`ADDING_SOURCE.md`](ADDING_SOURCE.md). Toute modification doit mettre à jour `registry/sources.csv`, `configs/runtime_sources.json` si nécessaire, les tests et cette documentation lorsque la stratégie d’accès change.
