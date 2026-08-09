# IvoireData Engine v0.5.0

IvoireData utilise **dlt OSS** comme moteur Extract/Normalize/Load. La valeur propre à IvoireData se situe autour de dlt : registre ivoirien, routage des sources, politiques de fraîcheur, provenance, connecteurs spécialisés, qualité, déduplication et fabrication d’IvoireCorpus.

## Composants

### `Settings`

Définit les chemins locaux : `data_lake`, état, registre et configuration runtime. Les variables d’environnement servent seulement à déplacer ces dossiers ou renommer le pipeline/dataset.

### `SourceRegistry`

Charge :

1. `registry/sources.csv` ;
2. `configs/runtime_sources.json` ;
3. applique les overrides ;
4. déduit un connecteur lorsque `connector=auto`.

### `IvoireDataEngine`

Responsabilités :

- résoudre une source ;
- vérifier qu’elle est autorisée pour l’ingestion automatique ;
- construire la ressource connecteur ;
- exécuter `pipeline.run(...)` ;
- enregistrer succès/erreur ;
- exposer la couverture du moteur.

### `FreshnessStore`

`.ivoiredata/state/freshness.json` contient `last_attempt`, `last_success`, `last_status` et les détails récents. `refresh_hours` détermine quand une nouvelle vérification est due.

### dlt pipeline

Le pipeline utilise la destination filesystem locale. dlt gère la normalisation, les tables, les schémas et son état technique. IvoireData ne réimplémente pas ces fonctions.

## Connecteurs

Connecteurs spécialisés :

- `data_gouv_ci` ;
- `world_bank_wdi` ;
- `ilostat_ref_area` ;
- `geoboundaries` ;
- `osm_geofabrik`.

Connecteurs génériques :

- `http_file` ;
- `bulk_catalog` ;
- `public_web`.

Voir [`CONNECTORS.md`](CONNECTORS.md).

## Mise à jour

Deux niveaux évitent le téléchargement inutile :

1. `FreshnessStore` évite de contacter trop souvent une source ;
2. le connecteur compare hash/signature/checksum/version lorsqu’il contacte la source.

Pour les sites publics, le hash du contenu évite le rechunking d’une page identique. Pour Data.gouv.ci, les signatures de métadonnées évitent de recharger un dataset inchangé. Pour Geofabrik, le checksum distant est utilisé lorsqu’il est disponible.

## Permissions

`SourceSpec.public` signifie « autorisé pour ingestion automatique par la politique IvoireData », pas « libre de tout droit ».

- `OPEN` / `OPEN_PUBLIC` : automatique possible ;
- `MIXED` : seulement si `metadata_only=true` ;
- `D_*` : bloqué automatiquement.

## Interfaces

### CLI

```bash
ivoiredata sources --public
ivoiredata coverage
ivoiredata status --public
ivoiredata sync SOURCE_ID
ivoiredata scheduler --once
ivoiredata scheduler --interval 3600
ivoiredata query 'SELECT ...'
ivoiredata corpus-build VERSION TABLE...
ivoiredata tokenizer-train CORPUS_DIR
```

### API

FastAPI expose l’état, les sources, la synchronisation, la recherche documentaire et les requêtes SQL locales.

## Corpus Factory

`corpus-build` lit une ou plusieurs tables dlt puis :

1. transforme chaque ligne en texte d’entraînement ;
2. nettoie Unicode/espaces ;
3. calcule un score qualité ;
4. rejette sous le seuil ;
5. déduplique exactement ;
6. écrit des shards JSONL ;
7. produit un manifest et son SHA-256.

Le corpus est versionné et marqué immutable. Voir [`CORPUS.md`](CORPUS.md).

## Limites assumées

- pas de base serveur dans la V1 ;
- pas d’ingestion automatique des microdonnées contrôlées ;
- les catalogues FAOSTAT/UIS n’aspirent pas tous les gros fichiers sans sélection explicite ;
- WHO reste sur un mode web public tant que l’interface de données officielle est en transition ;
- les connecteurs web ne garantissent pas l’extraction exhaustive d’un portail Javascript complexe.
