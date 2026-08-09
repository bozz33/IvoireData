# Couverture multisectorielle — v0.6.0

Cette matrice décrit **ce que le moteur livre réellement**, pas seulement les URLs connues.

## Niveaux de livraison

- **FULL_STRUCTURED** : payload réel + représentation structurée locale.
- **SNAPSHOT** : fichier réel local vérifiable + métadonnées/checksum.
- **WEB_DOCUMENTS** : pages/documents publics réellement récupérés + table documentaire.
- **CATALOG_ONLY** : catalogue/links réels, mais gros payloads non téléchargés sans sélection.
- **METADATA_ONLY** : métadonnées publiques uniquement, payload contrôlé volontairement exclu.
- **CONTROLLED** : référencé mais téléchargement automatique interdit par la politique.

Toutes les sorties v0.6 sont rangées sous `data_lake/domains/<domain>/<source_id>/`.

| Secteur | Source | Livraison v0.6 |
|---|---|---|
| Open data national | data.gouv.ci | **FULL_STRUCTURED** : CSV bruts + tables Parquet + catalogue |
| Statistiques nationales | ANStat/NADA | **METADATA_ONLY** : catalogue public ; microdonnées contrôlées exclues |
| Fiscalité/FNE | DGI | **WEB_DOCUMENTS** |
| Droit | OHADA | **WEB_DOCUMENTS** |
| Justice | Ministère Justice | **WEB_DOCUMENTS** |
| Administration | Service Public | **WEB_DOCUMENTS** |
| Dette | Trésor | **WEB_DOCUMENTS** |
| Marchés publics | DGMP | **WEB_DOCUMENTS** |
| Douanes/commerce | Douanes | **WEB_DOCUMENTS** |
| Finance | BCEAO/APIF | **WEB_DOCUMENTS** actuellement |
| Agriculture nationale | data.gouv.ci + Ministère Agriculture | **FULL_STRUCTURED + WEB_DOCUMENTS** |
| Agriculture internationale | FAOSTAT | **CATALOG_ONLY** par défaut ; payloads via sélection `download_patterns` |
| Travail/emploi | ILOSTAT | **FULL_STRUCTURED** : backend CSV `/data/indicator?ref_area=CIV` + Parquet |
| Santé nationale | RASS/E-DEPPS | **WEB_DOCUMENTS** |
| Santé internationale | WHO | **WEB_DOCUMENTS** tant que l'interface structurée actuelle n'est pas validée |
| Éducation nationale | MENA | **WEB_DOCUMENTS** |
| Éducation internationale | UNESCO UIS | **CATALOG_ONLY** par défaut |
| Télécoms | ARTCI | **WEB_DOCUMENTS** |
| Mines/pétrole/énergie | MMPE/MNV | **WEB_DOCUMENTS** |
| Environnement | Ministère/SIE | **WEB_DOCUMENTS** |
| Climat international | World Bank Climate | **WEB_DOCUMENTS** actuellement |
| Transport | Ministère Transports | **WEB_DOCUMENTS** |
| Foncier/logement | IDUFCI/Construction | **WEB_DOCUMENTS** |
| Eau/assainissement | ONEP/ONAD | **WEB_DOCUMENTS** |
| Météo | SODEXAM | **WEB_DOCUMENTS** |
| Géographie administrative | geoBoundaries | **FULL_STRUCTURED** |
| Géographie OSM | Geofabrik | **SNAPSHOT** PBF local + checksum |
| Développement macro | World Bank WDI | **FULL_STRUCTURED** : réponses JSON + tables Parquet |
| Projets World Bank | World Bank Projects | **FULL_STRUCTURED** : API `search.worldbank.org` (ISO2=CI) + Parquet |

## Pourquoi `CATALOG_ONLY` est distingué

Un catalogue FAOSTAT/UIS est une vraie donnée utile pour découvrir les fichiers disponibles, mais ce n'est **pas** l'équivalent d'avoir les séries statistiques dans le data lake. IvoireData ne doit donc jamais présenter `CATALOG_ONLY` comme couverture complète.

La transition vers `FULL_STRUCTURED` se fait après validation d'un connecteur/API ou sélection de fichiers bulk pertinents.

### État constaté au premier full sync (v0.5.0)

| Source | Niveau annoncé | État réel constaté | Action requise |
|--------|----------------|--------------------|----------------|
| `civ_faostat` | CATALOG_ONLY | La page `source_url` est une SPA JS qui ne liste pas les fichiers bulk → le connecteur `bulk_catalog` ne découvre aucun lien, donc la table d'inventaire elle-même est vide. Marquée `success` à tort. | Connecteur spécialisé : `source_url` doit pointer vers `https://bulks-faostat.fao.org/` ou utiliser l'API pays (area=38). Roadmap point 2. |
| `civ_uis` | CATALOG_ONLY | Idem : le bulk UIS réel et l'API SDMX (`api.on.unesco.org`) ne répondent pas. Inventaire vide. | Connecteur spécialisé à construire. Roadmap point 2. |
| `civ_ilostat` | FULL_STRUCTURED | **Résolu (v0.6)** : abandon du backend RDS (`pyreadr`/`librdata` segfaultait). Connecteur réécrit sur le backend CSV officiel `/data/indicator?ref_area=CIV` (filtrage serveur par pays, ~218 lignes CIV, snapshot CSV conservé). | — |
| `civ_worldbank_projects` | FULL_STRUCTURED | **Résolu (v0.6)** : connecteur API dédié `search.worldbank.org/api/v2/projects?countrycode_exact=CI` (192 projets CIV avec montants, secteurs, statut). | — |

Tant que ces sources restent vides, une synchro marquée `success` ne garantit pas qu'il y a des
données exploitables. Toujours vérifier l'inventaire du manifest (`inventory.tables.files`) et le
nombre de lignes Parquet avant de considérer une source comme réellement couverte.

## Vérification runtime

```bash
ivoiredata coverage
ivoiredata inventory
ivoiredata status --public
```

`coverage` décrit le registre/configuration ; `inventory` décrit les packages présents dans le data lake.

## Sources contrôlées

Restent hors téléchargement automatique :

- microdonnées ANStat soumises à conditions ;
- datasets `D_*` ;
- payloads nécessitant authentification/acceptation spécifique ;
- fichiers dont le mode d'accès n'autorise pas l'ingestion automatique selon la politique du projet.

## Cible

La cible opérationnelle est de faire progresser les sources prioritaires de :

```text
WEB_DOCUMENTS / CATALOG_ONLY
            ↓
connecteur spécialisé validé
            ↓
FULL_STRUCTURED
```

lorsque l'upstream fournit une interface officielle adaptée.
