# Couverture multisectorielle — v0.7.0

Cette matrice distingue **ce qui est implémenté dans le code** de **ce qui a été validé par un sync réel local**.

## Niveaux de livraison v0.7

Le moteur ne déduit plus la couverture du seul `status=success`.

- `FULL_STRUCTURED` : tables Parquet avec lignes métier ;
- `DOCUMENTS_ONLY` : pages/PDF/documents réellement archivés ;
- `SNAPSHOT_ONLY` : payload brut réel sans table métier ;
- `METADATA_ONLY` : limitation volontaire aux métadonnées publiques ;
- `EMPTY` : aucune livraison exploitable détectée.

Voir [`AUDIT.md`](AUDIT.md).

## Couverture

| Secteur | Source | Connecteur / livraison cible | État v0.7 |
|---|---|---|---|
| Open data national | data.gouv.ci | `data_gouv_ci` / FULL_STRUCTURED | validé en sync réel historique |
| Statistiques nationales | ANStat/NADA | `public_web metadata_only` | métadonnées publiques ; TLS parfois dégradé |
| Fiscalité/FNE | DGI | WEB_DOCUMENTS | opérationnel |
| Droit | OHADA | WEB_DOCUMENTS | opérationnel |
| Justice | Ministère Justice | WEB_DOCUMENTS | opérationnel |
| Administration | Service Public | WEB_DOCUMENTS | opérationnel |
| Dette | Trésor | WEB_DOCUMENTS | upstream peut renvoyer HTTP 500 ; stale conservé |
| Marchés publics | DGMP | WEB_DOCUMENTS | opérationnel |
| Douanes | Douanes | WEB_DOCUMENTS | opérationnel |
| Finance | BCEAO/APIF | WEB_DOCUMENTS | opérationnel ; connecteur structuré futur possible |
| Agriculture nationale | Data.gouv.ci + Ministère | FULL_STRUCTURED + DOCUMENTS | opérationnel |
| Agriculture internationale | FAOSTAT | `faostat_country` / FULL_STRUCTURED | **implémenté v0.7 ; sync live local requis** |
| Travail/emploi | ILOSTAT | `ilostat_ref_area` CSV / FULL_STRUCTURED | corrigé v0.7 ; conserver tous les `obs_status` |
| Santé nationale | RASS/E-DEPPS | WEB_DOCUMENTS | opérationnel |
| Santé internationale | WHO | WEB_DOCUMENTS | interface structurée à spécialiser ultérieurement |
| Éducation nationale | MENA | WEB_DOCUMENTS | opérationnel |
| Éducation internationale | UNESCO UIS | `uis_country` / FULL_STRUCTURED | **implémenté v0.7 ; sync live local requis** |
| Télécoms | ARTCI | WEB_DOCUMENTS | opérationnel |
| Mines/pétrole/énergie | MMPE/MNV | WEB_DOCUMENTS | opérationnel |
| Environnement | Ministère/SIE | WEB_DOCUMENTS | opérationnel |
| Climat international | World Bank Climate | WEB_DOCUMENTS | connecteur structuré futur possible |
| Transport | Ministère Transports | WEB_DOCUMENTS | opérationnel |
| Foncier/logement | IDUFCI/Construction | WEB_DOCUMENTS | opérationnel |
| Eau/assainissement | ONEP/ONAD | WEB_DOCUMENTS | opérationnel |
| Météo | SODEXAM | WEB_DOCUMENTS | opérationnel |
| Géographie administrative | geoBoundaries | FULL_STRUCTURED | opérationnel |
| Géographie OSM | Geofabrik | SNAPSHOT_ONLY PBF | opérationnel |
| Développement macro | World Bank WDI | FULL_STRUCTURED | opérationnel |
| Projets World Bank | World Bank Projects | FULL_STRUCTURED | opérationnel |

## FAOSTAT v0.7

L’ancien `bulk_catalog` avec `max_downloads=0` pouvait produire un `success` sans donnée. Il est remplacé par `faostat_country` qui :

- télécharge des ZIP bulk officiels sélectionnés ;
- conserve les ZIP dans `raw/` avec SHA-256 ;
- filtre les CSV normalisés sur la Côte d’Ivoire ;
- produit des Parquet par famille statistique ;
- échoue si aucune ligne pays n’est trouvée.

Le premier sync réel v0.7 doit confirmer les noms/format courants des archives et le volume livré.

## UIS v0.7

L’ancien catalogue vide est remplacé par `uis_country` :

- API publique UIS ;
- `geoUnit=CIV` ;
- définitions + séries + raw JSON ;
- Parquet local.

Le premier sync réel v0.7 doit confirmer le payload courant de l’API et le nombre de lignes Côte d’Ivoire.

## ILOSTAT v0.7

Le backend RDS/pyreadr a été abandonné. Le CSV pays est utilisé.

Correction critique : `obs_status` est conservé comme statut d’observation. Les valeurs révisées (`R`) ne sont plus supprimées par un faux filtre de fréquence.

## Validation après mise à jour

```bash
ivoiredata sync civ_ilostat --force
ivoiredata sync civ_faostat --force
ivoiredata sync civ_uis --force
ivoiredata sync civ_worldbank_projects --force
ivoiredata audit
```

Puis :

```bash
ivoiredata sync --all-public --force
ivoiredata audit
```

La sortie de `audit` devient la référence pour annoncer le nombre de sources réellement utilisables.

## Sources contrôlées

Restent hors téléchargement automatique : microdonnées ANStat soumises à conditions, sources `D_*`, payloads exigeant authentification/acceptation ou toute source dont les droits n’autorisent pas l’ingestion automatisée.
