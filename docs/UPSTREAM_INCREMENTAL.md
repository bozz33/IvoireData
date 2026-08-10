# IvoireData v0.8.2 — synchronisation upstream officielle et incrémentale

Ce document décrit les endpoints réellement utilisés, la politique anti-retéléchargement et la procédure de migration/validation locale.

## Principe

IvoireData ne confond plus « vérifier une source » avec « télécharger à nouveau son contenu ».

Ordre de décision :

1. **version officielle de la source** lorsqu'elle existe ;
2. **ETag / Last-Modified** et requête conditionnelle HTTP ;
3. **SHA-256** uniquement lorsque le fournisseur n'expose pas de version/validator.

Une petite requête de métadonnées reste nécessaire pour savoir si une source distante a changé. Zéro échange réseau et fraîcheur garantie sont incompatibles sauf si le fournisseur fournit un mécanisme externe de notification.

`--force` signifie : **vérifier immédiatement, même si `refresh_hours` n'est pas atteint**. Il ne signifie pas : télécharger deux fois une version identique.

## État persistant

```text
.ivoiredata/state/upstreams.json
```

Cet état contient les signatures/validators et le dernier résultat réseau par artefact. Il est partagé par les conteneurs Docker via `.ivoiredata/`.

Le fichier upstream n'est **pas** la preuve qu'une donnée est matérialisée dans `tables/` : l'état transactionnel dlt reste la source d'autorité. Si le processus tombe après téléchargement mais avant commit dlt, le snapshot local peut être rejoué sans nouveau transfert.

Tous les fichiers d'état JSON principaux sont écrits par remplacement atomique. En cas de JSON tronqué, le fichier est renommé `*.corrupt-<timestamp>` et le moteur redémarre avec un état propre au lieu de devenir indisponible.

## Data.gouv.ci / Data Fair

Endpoints :

```text
GET https://data.gouv.ci/data-fair/api/v1/datasets?size=1000&page=1
GET https://data.gouv.ci/data-fair/api/v1/datasets/{id}/full
GET https://data.gouv.ci/data-fair/api/v1/datasets/{id}/lines?size=10000&page=1&count=exact
```

Le contrat Data Fair impose `page >= 1`. Pour `/lines`, IvoireData suit ensuite **le champ `next` retourné par Data Fair jusqu'à son absence**. Il ne calcule pas lui-même des offsets profonds.

Algorithme :

```text
catalogue public anonyme
  -> signature dataset
     -> version déjà matérialisée : UNCHANGED
     -> nouvelle/modifiée : /full
          -> succès : snapshot + Parquet
          -> échec : /lines + curseur next
               -> succès : snapshot canonique + Parquet
               -> échec : failure détaillée (codes HTTP full/lines)
```

Rapport :

```text
data_lake/domains/.../civ_datagouv_catalog/raw/datagouv_sync_stats.json
```

Champs importants : `catalog_visible_anonymous`, `selected`, `unchanged`, `downloaded`, `via_full`, `via_lines`, `failed`, `removed_upstream`.

La cible CI Gold est `failed=0` pour les datasets publics tabulaires réellement exposés par l'API. Les échecs restants doivent être analysés individuellement ; ils ne sont plus masqués par un simple « dataset ignoré ».

## ILOSTAT

Endpoints officiels :

```text
GET https://rplumber.ilo.org/metadata/toc/indicator/?lang=en
GET https://rplumber.ilo.org/data/indicator/?id=<INDICATOR>&ref_area=CIV&lang=en&type=code&format=.csv
```

L'ancien appel `ref_area=CIV` sans `id` n'était pas exhaustif. La v0.8.2 lit d'abord le TOC officiel puis interroge chaque indicateur nouveau/modifié pour la Côte d'Ivoire.

Signature : `id/freq/size/data.start/data.end/last.update/n.records/collection`.

Le RDS n'est pas réintroduit : l'ancien parser RDS a déjà causé un crash natif. Le CSV REST officiel est plus lent au premier passage, mais sûr et incrémental ensuite.

Le connecteur utilise retries exponentiels sur `429`, `5xx`, timeouts/connexions et une petite pause configurable.

Rapport : `ilostat_sync_stats.json` avec notamment `toc_indicators`, `selected_indicators`, `network_queries`, `unchanged`, `with_country_rows`, `without_country_rows`, `failed`, `business_rows`.

## FAOSTAT

Catalogue officiel :

```text
https://bulks-faostat.fao.org/production/datasets_E.json
```

Il fournit pour chaque domaine : `DatasetCode`, `DatasetName`, `DateUpdate`, `FileSize`, `FileRows`, `FileLocation`.

La v0.8.2 découvre automatiquement les domaines courants et exclut par défaut les jeux dont le nom commence par `Discontinued archives and data series:`.

`DateUpdate + FileSize + FileRows + FileLocation` forment la signature. Un ZIP inchangé n'est pas téléchargé.

Protection du serveur :

```text
max_bytes_per_file       = 500 MB
max_new_bytes_per_run    = 1.5 GB
```

Si le premier élargissement FAOSTAT dépasse le budget, le reste passe en backlog ; relancer `civ_faostat --force` continue la migration sans retélécharger les domaines déjà terminés.

Rapport : `faostat_sync_stats.json`. CI Gold bloque tant que `backlog_count > 0` ou `failed > 0`.

## World Bank WDI

API V2 officielle. Avant de télécharger les milliers d'indicateurs, IvoireData lit :

```text
GET https://api.worldbank.org/v2/sources/2?format=json
```

Le `lastupdated` officiel devient la signature de la source WDI. Si elle est identique à la version dlt déjà chargée, aucune récupération massive n'est faite.

Le mécanisme historique de subdivision récursive des lots HTTP 400 est conservé. Les indicateurs isolés impossibles à requêter sont maintenant listés dans `ignored_http400_indicators` et deviennent visibles dans l'audit qualité.

## World Bank Projects

```text
https://search.worldbank.org/api/v2/projects?countrycode_exact=CI
```

Le premier appel utilise ETag/Last-Modified quand disponibles. Sinon, le contenu complet paginé est canonisé et hashé ; aucune réécriture n'a lieu si le hash est identique.

## UNESCO UIS

```text
https://api.uis.unesco.org/api/public/definitions/indicators
https://api.uis.unesco.org/api/public/definitions/geounits
https://api.uis.unesco.org/api/public/data/indicators?geoUnit=CIV
```

Chaque artefact utilise HTTP validators. Si le serveur ne les fournit pas, SHA-256 empêche les snapshots/écritures dupliqués.

## geoBoundaries

Métadonnées ADM et GeoJSON utilisent ETag/Last-Modified + SHA-256. Les niveaux absents retournant 404 sont ignorés comme auparavant.

## Geofabrik / OSM

Pour le gros PBF, la priorité est le sidecar officiel :

```text
<extract>-latest.osm.pbf.md5
```

MD5 identique = aucun transfert PBF. Si le sidecar n'est pas disponible, IvoireData utilise ETag/Last-Modified.

## Portails institutionnels `public_web`

Chaque URL connue est vérifiée avec `If-None-Match` / `If-Modified-Since`. Un HTTP 304 ne transfère pas le body. Les liens enfants déjà découverts sont conservés dans l'état upstream afin que le crawl ne s'arrête pas au niveau de la page racine.

Si le serveur ne fournit aucun validator, le body doit être reçu pour déterminer s'il a changé ; SHA-256 empêche toutefois les snapshots et chunks en double.

## Audit

```bash
ivoiredata upstreams
ivoiredata upstreams civ_datagouv_catalog
ivoiredata upstreams civ_ilostat
ivoiredata upstreams civ_faostat
```

API :

```text
GET /upstreams
GET /upstreams/{source_id}
```

CI Gold intègre désormais les rapports structurés. Les états suivants bloquent un P0 :

```text
UPSTREAM_PARTIAL_FAILURE
UPSTREAM_BACKLOG
```

## Mise à niveau serveur v0.8.1 -> v0.8.2

### 1. Sauvegarder et arrêter l'automatique

```bash
docker compose stop scheduler
mkdir -p backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
tar -czf "backups/ivoiredata-state-pre-082-${STAMP}.tgz" .ivoiredata
```

Conserver également la sauvegarde normale de `data_lake/` sur le second disque avant migration.

### 2. Mettre à jour

```bash
git status
git pull
docker compose build
docker compose --profile run up -d

docker compose exec api ivoiredata --version
curl -fsS http://127.0.0.1:8000/health
```

Attendu : `0.8.2` et chemin `incremental_upstream_state`.

### 3. Migrer les sources structurées une par une

```bash
docker compose exec api ivoiredata sync civ_datagouv_catalog --force
docker compose exec api ivoiredata sync civ_ilostat --force
docker compose exec api ivoiredata sync civ_faostat --force
docker compose exec api ivoiredata sync civ_worldbank_wdi --force
docker compose exec api ivoiredata sync civ_worldbank_projects --force
docker compose exec api ivoiredata sync civ_uis --force
docker compose exec api ivoiredata sync civ_geoboundaries --force
docker compose exec api ivoiredata sync civ_osm_geofabrik --force
```

Pour FAOSTAT, répéter uniquement :

```bash
docker compose exec api ivoiredata sync civ_faostat --force
```

jusqu'à ce que `faostat_sync_stats.json` affiche `backlog_count=0` et `failed=0`. Les domaines déjà terminés seront `unchanged` et non retéléchargés.

### 4. Prouver l'absence de retéléchargement

Une fois la première migration terminée, relancer immédiatement :

```bash
docker compose exec api ivoiredata sync civ_datagouv_catalog --force
docker compose exec api ivoiredata sync civ_ilostat --force
docker compose exec api ivoiredata sync civ_faostat --force
docker compose exec api ivoiredata sync civ_worldbank_wdi --force
docker compose exec api ivoiredata sync civ_osm_geofabrik --force
```

Attendus lorsque rien n'a changé :

- Data.gouv : `downloaded=0`, datasets surtout `unchanged` ;
- ILOSTAT : `network_queries=0` pour les indicateurs, TOC léger seulement ;
- FAOSTAT : `downloaded=0`, `backlog_count=0`, domaines `unchanged` ;
- WDI : `unchanged=true` après lecture du petit metadata source ;
- OSM : `changed=false`, PBF non transféré.

Puis :

```bash
docker compose exec api ivoiredata upstreams civ_datagouv_catalog
docker compose exec api ivoiredata upstreams civ_ilostat
docker compose exec api ivoiredata upstreams civ_faostat
docker compose exec api ivoiredata quality-audit
docker compose exec api ivoiredata ci-gold
```

### 5. Qualification CI Gold

La v0.8.2 change matériellement la couverture ILOSTAT/FAOSTAT/Data.gouv. La qualification 14 jours doit être redémarrée **après** une migration propre, lorsque :

```text
Data.gouv failed = 0
ILOSTAT failed = 0
FAOSTAT backlog_count = 0
FAOSTAT failed = 0
quality critical = 0
structured_upstreams_complete = true
```

Puis :

```bash
docker compose exec api ivoiredata qualification start
```
