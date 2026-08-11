# IvoireData v0.8.2 — synchronisation upstream officielle, incrémentale et robuste

Ce document décrit les endpoints réellement utilisés, la politique anti-retéléchargement, les garanties de concurrence et la procédure de migration/validation locale.

## Principe

IvoireData ne confond plus **« vérifier une source »** avec **« télécharger à nouveau son contenu »**.

Ordre de décision :

1. **version/signature officielle de la source** lorsqu'elle existe ;
2. **ETag / Last-Modified** et requête conditionnelle HTTP ;
3. **SHA-256** lorsque le fournisseur n'expose pas de version/validator.

Une petite requête de métadonnées reste nécessaire pour savoir si une source distante a changé. Sans version, validator ou notification du fournisseur, il est impossible de garantir la fraîcheur sans contacter le serveur.

`--force` signifie : **vérifier immédiatement, même si `refresh_hours` n'est pas atteint**. Il ne signifie pas : télécharger deux fois une version identique.

## État persistant et concurrence

```text
.ivoiredata/state/upstreams.json
.ivoiredata/state/freshness.json
.ivoiredata/state/runtime_overrides.json
.ivoiredata/state/ci_gold_qualification.json
.ivoiredata/state/locks/
```

`upstreams.json` contient signatures, validators, chemins de cache et dernier résultat par artefact.

Le cache upstream n'est **pas** la preuve qu'une donnée est matérialisée dans `tables/` : l'état transactionnel dlt reste l'autorité. Si le processus tombe après téléchargement mais avant commit dlt, le snapshot local peut être rejoué sans nouveau transfert.

### Garanties de robustesse

- JSON critiques écrits par remplacement atomique ;
- `fsync` avant remplacement ;
- JSON tronqué/corrompu renommé en `*.corrupt-<timestamp>` ;
- verrou inter-processus sur les mutations de `upstreams.json`, `freshness.json`, overrides et qualification ;
- verrou **par source** autour d'un run dlt : API, scheduler et `sync-once` ne peuvent pas exécuter la même source simultanément ;
- deux sources différentes restent indépendantes ;
- `catalog.json` est reconstruit sous verrou global puis remplacé atomiquement ;
- manifests écrits atomiquement ;
- les tables dynamiques inchangées ne sont pas supprimées lorsque le connecteur n'émet que les tables modifiées.

## Data.gouv.ci / Data Fair

Endpoints :

```text
GET https://data.gouv.ci/data-fair/api/v1/datasets?size=1000&page=1
GET https://data.gouv.ci/data-fair/api/v1/datasets/{id}/full
GET https://data.gouv.ci/data-fair/api/v1/datasets/{id}/lines?size=10000&page=1&count=exact
```

Le catalogue est celui visible par un utilisateur anonyme : il représente les jeux effectivement publics au moment du contrôle, pas les anciennes entrées techniques historiques.

### Pagination robuste

Le moteur :

- commence à `page=1` ;
- déduplique les identifiants ;
- poursuit tant que le `count` annoncé n'est pas atteint ;
- ne suppose pas que le serveur honorera forcément `size=1000` ;
- détecte une page répétée/stagnante avant d'atteindre `count` et échoue explicitement plutôt que de tronquer silencieusement le catalogue ;
- pour `/lines`, suit **le champ `next` retourné par Data Fair jusqu'à son absence**.

### Algorithme

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
data_lake/domains/multidomain/civ_datagouv_catalog/raw/datagouv_sync_stats.json
```

Champs importants :

```text
catalog_visible_anonymous
selected
unchanged
downloaded
via_full
via_lines
failed
failures
removed_upstream
removed_upstream_ids
archived_removed_tables
reappeared
business_rows_changed
```

### Datasets retirés

Un jeu qui disparaît du catalogue public n'est jamais supprimé historiquement :

```text
raw/legacy/removed_upstream/<timestamp>/
```

La table active est déplacée dans cette archive et ne pollue plus la livraison courante. Les tables anciennes pré-v0.8.2 qui ne peuvent pas être reliées au catalogue courant sont archivées après un run Data.gouv réussi sous :

```text
raw/legacy/orphan_tables/<timestamp>/
```

Si le jeu réapparaît, IvoireData le rematérialise et réutilise le snapshot local correspondant lorsque possible.

La cible n'est donc plus un ancien chiffre du type « 471/471 ». La vérité opérationnelle est le **nombre `catalog_visible_anonymous` retourné aujourd'hui** et, parmi ceux-ci, `failed=0` pour les jeux réellement exposés par l'API.

## ILOSTAT

Endpoints officiels utilisés :

```text
GET https://rplumber.ilo.org/metadata/toc/ref_area/?lang=en
GET https://rplumber.ilo.org/metadata/toc/indicator/?lang=en
GET https://rplumber.ilo.org/data/indicator/?id=<INDICATOR>&ref_area=CIV&lang=en&type=code&format=.csv
```

L'ancien appel `ref_area=CIV` sans `id` n'était pas exhaustif.

### Barrière pays `REF_AREA`

ILOSTAT publie les datasets de référence pays/fréquence tels que :

```text
CIV_A
CIV_Q
CIV_M
```

lorsqu'ils existent. Leurs métadonnées (`last.update`, taille, période, nombre de lignes...) forment une **signature pays**.

```text
petit TOC REF_AREA
  -> signature CIV identique
       -> arrêt : pas de TOC indicateurs, pas de requêtes de données
  -> signature CIV modifiée
       -> TOC indicateurs
       -> uniquement indicateurs nouveaux/modifiés
       -> CSV REST filtré ref_area=CIV
```

Le RDS officiel n'est pas réintroduit dans le processus Python : l'ancien chemin de parsing natif a déjà provoqué un SIGSEGV. Le TOC `REF_AREA` sert de version officielle légère ; le CSV REST est le chemin de matérialisation sûr.

Le connecteur utilise retries exponentiels sur `429`, `5xx`, timeouts/connexions et une pause légère configurable.

Rapport :

```text
ilostat_sync_stats.json
```

Le **deuxième run immédiat**, si ILOSTAT n'a pas changé, doit notamment montrer :

```text
ref_area_unchanged_gate = true
indicator_toc_requested = false
network_queries = 0
```

## FAOSTAT

Catalogue officiel :

```text
https://bulks-faostat.fao.org/production/datasets_E.json
```

Il fournit notamment :

```text
DatasetCode
DatasetName
DateUpdate
FileSize
FileRows
FileLocation
```

IvoireData découvre automatiquement les domaines courants et exclut par défaut les jeux dont le nom commence par `Discontinued archives and data series:`.

`DateUpdate + FileSize + FileRows + FileLocation` forment la signature. Un ZIP inchangé n'est pas téléchargé.

Le portail développeur API FAOSTAT 2026 existe, mais IvoireData ne fabrique pas un endpoint non vérifié : le bulk officiel reste le chemin de production tant que le contrat exact du nouveau portail n'est pas intégré et testé dans le projet.

Protection du serveur :

- limite par fichier configurée dans `runtime_sources.json` ;
- budget total de nouveaux octets par run : 1,5 GB par défaut ;
- dataset dépassant la limite : backlog `FILE_TOO_LARGE` ;
- budget de run épuisé : backlog `RUN_BUDGET` ;
- relance suivante reprend sans retélécharger les domaines déjà matérialisés.

Rapport : `faostat_sync_stats.json`. CI Gold bloque tant que :

```text
backlog_count > 0
ou
failed > 0
```

Si un `FILE_TOO_LARGE` apparaît réellement, augmenter la limite ne doit se faire qu'après inspection de `FileSize` officiel et de l'espace disque/mémoire disponible.

## World Bank WDI

API V2 officielle. Avant les milliers d'indicateurs, IvoireData lit :

```text
GET https://api.worldbank.org/v2/sources/2?format=json
```

Le `lastupdated` officiel devient la signature de la source. Signature identique = aucune récupération massive.

Le mécanisme de subdivision récursive des lots HTTP 400 est conservé ; les indicateurs unitaires impossibles à requêter sont exposés dans le rapport au lieu d'être silencieux.

## World Bank Projects

```text
https://search.worldbank.org/api/v2/projects?countrycode_exact=CI
```

Le premier appel utilise ETag/Last-Modified lorsqu'ils existent. Sinon, le contenu paginé est canonisé et hashé ; une version identique n'est pas réécrite.

## UNESCO UIS

```text
https://api.uis.unesco.org/api/public/definitions/indicators
https://api.uis.unesco.org/api/public/definitions/geounits
https://api.uis.unesco.org/api/public/data/indicators?geoUnit=CIV
```

Chaque artefact utilise les validators HTTP. Sans validator, SHA-256 évite les snapshots et écritures en double.

## geoBoundaries

Les métadonnées ADM et GeoJSON utilisent ETag/Last-Modified + SHA-256. Les niveaux ADM absents retournant 404 restent normaux.

Après migration, les couches nécessaires peuvent être rejouées depuis cache local afin de reconstruire la table agrégée sans refaire un téléchargement identique.

## Geofabrik / OSM

Pour le gros PBF, priorité au checksum officiel :

```text
<extract>-latest.osm.pbf.md5
```

MD5 identique = aucun transfert PBF. Le PBF v0.8.1 existant peut être adopté si son MD5 correspond au checksum officiel.

## Portails institutionnels `public_web`

Chaque URL est vérifiée avec `If-None-Match` / `If-Modified-Since` lorsqu'un validator existe. HTTP 304 = aucun body.

Les liens enfants déjà découverts sont conservés dans l'état upstream afin qu'un 304 sur la racine ne fasse pas oublier les pages connues.

Sans validator, le serveur doit transmettre le body pour que le moteur puisse constater une modification ; SHA-256 empêche néanmoins snapshots et chunks dupliqués.

## Audit upstream

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

CI Gold bloque un P0 structuré sur :

```text
UPSTREAM_PARTIAL_FAILURE
UPSTREAM_BACKLOG
```

---

# Mise à niveau serveur v0.8.1 -> v0.8.2

## 1. Sauvegarder et arrêter le scheduler

Ne supprimer ni `data_lake/` ni `.ivoiredata/`.

```bash
docker compose stop scheduler
mkdir -p backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
tar -czf "backups/ivoiredata-state-pre-082-${STAMP}.tgz" .ivoiredata
```

Faire également la sauvegarde normale de `data_lake/` sur le second disque.

## 2. Mettre à jour le code et reconstruire

```bash
git status
git pull
docker compose build --pull
docker compose up -d api
```

Vérifier :

```bash
docker compose exec api ivoiredata --version
curl -fsS http://127.0.0.1:8000/health
```

Attendu : `0.8.2`.

Puis :

```bash
docker compose exec api ivoiredata upstreams
```

## 3. Première migration structurée — séquentielle

La première migration peut être plus longue : elle crée/adopte les signatures et élargit ILOSTAT/FAOSTAT.

```bash
for s in \
  civ_datagouv_catalog \
  civ_ilostat \
  civ_faostat \
  civ_worldbank_wdi \
  civ_worldbank_projects \
  civ_uis \
  civ_geoboundaries \
  civ_osm_geofabrik
do
  echo "===== $s ====="
  docker compose exec api ivoiredata sync "$s" --force || exit 1
done
```

`--force` vérifie maintenant ; il ne signifie plus « retélécharger identique ».

## 4. Inspecter les rapports réels

```bash
docker compose exec -T api python - <<'PY'
from ivoiredata.engine import IvoireDataEngine
from ivoiredata.delivery import source_paths
import json

e = IvoireDataEngine()
for sid, filename in [
    ("civ_datagouv_catalog", "datagouv_sync_stats.json"),
    ("civ_ilostat", "ilostat_sync_stats.json"),
    ("civ_faostat", "faostat_sync_stats.json"),
]:
    spec = e.registry.get(sid)
    path = source_paths(e.settings, spec)["raw"] / filename
    print(f"\n===== {sid} =====")
    if not path.exists():
        print("MISSING", path)
        continue
    print(json.dumps(json.loads(path.read_text()), ensure_ascii=False, indent=2))
PY
```

### Data.gouv attendu

Lire la vérité actuelle dans :

```text
catalog_visible_anonymous
failed
via_full
via_lines
removed_upstream
archived_removed_tables
```

Ne pas comparer au vieux chiffre `224` : l'ancien connecteur mélangeait échecs `/full`, anciennes entrées techniques et états historiques.

### ILOSTAT attendu

Le premier run doit produire beaucoup plus qu'un appel `ref_area=CIV` sans indicateur et exposer les indicateurs avec/sans lignes CIV et les échecs éventuels.

### FAOSTAT attendu

```text
failed = 0
backlog_count = 0
```

Si `RUN_BUDGET`, relancer seulement FAOSTAT :

```bash
docker compose exec api ivoiredata sync civ_faostat --force
```

Les domaines déjà faits restent inchangés.

## 5. Deuxième run immédiat — preuve anti-retéléchargement

Relancer immédiatement :

```bash
for s in \
  civ_datagouv_catalog \
  civ_ilostat \
  civ_faostat \
  civ_worldbank_wdi \
  civ_worldbank_projects \
  civ_uis \
  civ_geoboundaries \
  civ_osm_geofabrik
do
  echo "===== SECOND RUN $s ====="
  docker compose exec api ivoiredata sync "$s" --force || exit 1
done
```

Si aucun fournisseur n'a réellement changé entre les deux runs :

- Data.gouv : `downloaded=0` ; datasets majoritairement `unchanged` ;
- ILOSTAT : `ref_area_unchanged_gate=true`, `indicator_toc_requested=false`, `network_queries=0` ;
- FAOSTAT : `downloaded=0`, `backlog_count=0` ;
- WDI : metadata `lastupdated` identique, pas de sweep massif ;
- OSM : checksum MD5 identique, pas de PBF ;
- UIS/Projects/geoBoundaries : 304/validators ou SHA identique selon ce que fournit l'upstream.

Contrôler :

```bash
docker compose exec api ivoiredata upstreams civ_datagouv_catalog
docker compose exec api ivoiredata upstreams civ_ilostat
docker compose exec api ivoiredata upstreams civ_faostat
docker compose exec api ivoiredata upstreams civ_worldbank_wdi
docker compose exec api ivoiredata upstreams civ_osm_geofabrik
```

## 6. Vérifier qu'aucune donnée active n'a été perdue

```bash
docker compose exec api ivoiredata audit
docker compose exec api ivoiredata quality-audit
docker compose exec api ivoiredata coverage-audit
docker compose exec api ivoiredata ci-gold
```

Vérifier aussi les archives Data.gouv éventuelles :

```bash
find data_lake/domains/multidomain/civ_datagouv_catalog/raw/legacy -maxdepth 4 -type f 2>/dev/null | sort
```

Une archive signifie « retiré/orphelin du catalogue courant », pas « donnée effacée ».

## 7. Full public après migration structurée

Seulement après les vérifications précédentes :

```bash
docker compose exec api ivoiredata sync --all-public --force
```

Puis :

```bash
docker compose exec api ivoiredata audit
docker compose exec api ivoiredata quality-audit
docker compose exec api ivoiredata coverage-audit
docker compose exec api ivoiredata upstreams
docker compose exec api ivoiredata ci-gold
```

## 8. Reprendre le scheduler et redémarrer la qualification

La sémantique Data.gouv/ILOSTAT/FAOSTAT a changé matériellement. Redémarrer la fenêtre CI Gold seulement lorsque :

```text
Data.gouv failed = 0
ILOSTAT failed = 0
FAOSTAT failed = 0
FAOSTAT backlog_count = 0
quality critical = 0
structured_upstreams_complete = true
```

Puis :

```bash
docker compose --profile run up -d scheduler
docker compose exec api ivoiredata qualification start
```

Suivre avec :

```bash
docker compose exec api ivoiredata qualification status
docker compose exec api ivoiredata ci-gold
```
