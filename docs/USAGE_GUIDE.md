# Guide d’utilisation IvoireData

Ce document explique comment installer, démarrer, synchroniser, auditer et exploiter IvoireData au quotidien.

> IvoireData est le moteur de collecte et de livraison du data lake. Il ne réalise pas le nettoyage ML avancé, la déduplication du corpus, l’entraînement du tokenizer ni l’entraînement du modèle. Ces opérations commencent après le handoff décrit dans `DATA_HANDOFF_CONTRACT.md` et `DOWNSTREAM_AUTOMATION.md`.

## 1. Prérequis

### Avec Docker — méthode recommandée

- Git ;
- Docker Engine / Docker Desktop ;
- Docker Compose v2 ;
- accès Internet aux sources publiques ;
- espace disque suffisant pour `data_lake/`.

### Sans Docker

- Python 3.10 ou supérieur ;
- Git ;
- un environnement virtuel Python recommandé.

## 2. Récupérer ou mettre à jour le projet

```bash
git clone https://github.com/bozz33/IvoireData.git
cd IvoireData
```

Si le dépôt existe déjà :

```bash
git pull
```

Avant un pull, conserver les modifications locales utiles avec un commit ou un stash :

```bash
git status
git stash push -u -m "local-before-update"
git pull
```

Ne jamais supprimer `data_lake/` ou `.ivoiredata/` pendant une mise à jour du code.

## 3. Démarrage avec Docker

Construire les images :

```bash
docker compose build
```

Démarrer l’API et le scheduler permanent :

```bash
docker compose --profile run up -d
```

Vérifier les conteneurs :

```bash
docker compose ps
```

Suivre les logs :

```bash
docker compose logs -f api
docker compose logs -f scheduler
```

Arrêter la stack :

```bash
docker compose down
```

Les données restent sur le disque hôte dans `data_lake/` et `.ivoiredata/`.

## 4. Installation sans Docker

Créer un environnement virtuel :

```bash
python -m venv .venv
```

Linux/macOS :

```bash
source .venv/bin/activate
```

Windows PowerShell :

```powershell
.\.venv\Scripts\Activate.ps1
```

Installer IvoireData :

```bash
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Vérifier :

```bash
ivoiredata --help
ivoiredata coverage
```

## 5. Comprendre le stockage

Chaque source possède son propre dossier :

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
```

- `raw/` : snapshots bruts ou fichiers source ;
- `tables/` : tables Parquet produites par dlt ;
- `documents/` : pages Web/PDF et snapshots documentaires ;
- `manifest.json` : état, provenance, métriques, droits et warnings de la source ;
- `catalog.json` : index global du data lake.

L’état du scheduler est conservé dans :

```text
.ivoiredata/state/freshness.json
```

## 6. Commandes de diagnostic de base

Lister les sources publiques :

```bash
ivoiredata sources --public
```

Voir la couverture configurée :

```bash
ivoiredata coverage
```

Voir le dernier état des sources :

```bash
ivoiredata status --public
```

Voir l’inventaire du data lake :

```bash
ivoiredata inventory
```

Voir l’audit réel :

```bash
ivoiredata audit
```

Avec Docker :

```bash
docker compose exec api ivoiredata audit
```

## 7. Synchroniser une source

Exemples :

```bash
ivoiredata sync civ_worldbank_wdi
ivoiredata sync civ_ilostat --force
ivoiredata sync civ_faostat --force
ivoiredata sync civ_uis --force
```

Avec Docker :

```bash
docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync civ_faostat --force"
```

`--force` force une nouvelle vérification/synchronisation même si la source n’est pas encore due.

Après chaque synchronisation importante :

```bash
ivoiredata audit
```

## 8. Synchronisation complète

Toutes les sources publiques activées :

```bash
ivoiredata sync --all-public --force
```

Avec Docker :

```bash
docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync --all-public --force"
```

Puis :

```bash
docker compose exec api ivoiredata audit
```

`--due` n’est pas obligatoire avec `--all-public`. Pour une exploitation normale, préférer le scheduler plutôt qu’un full sync forcé fréquent.

## 9. Scheduler

Une seule passe des sources dues :

```bash
ivoiredata scheduler --once
```

Scheduler permanent :

```bash
ivoiredata scheduler --interval 3600
```

Avec Docker :

```bash
docker compose --profile run up -d
```

Chaque source conserve sa propre valeur `refresh_hours`. Le scheduler ne doit pas télécharger inutilement toutes les sources à chaque réveil.

## 10. Lire l’audit

IvoireData sépare quatre dimensions.

### `sync_status`

- `SUCCESS` : dernier run terminé sans exception ;
- `ERROR` : dernier run en erreur ;
- `NEVER` : jamais synchronisé.

### `delivery_status`

- `FULL_STRUCTURED` : vraie donnée métier structurée en Parquet ;
- `DOCUMENTS_ONLY` : pages Web, PDF ou texte documentaire ;
- `SNAPSHOT_ONLY` : snapshot brut/binaire, par exemple OSM PBF ;
- `METADATA_ONLY` : limitation volontaire aux métadonnées publiques ;
- `EMPTY` : aucune livraison exploitable.

### `freshness_status`

- `FRESH` ;
- `DUE` ;
- `STALE` ;
- `NEVER_SYNCED`.

### `transport_security`

- `VERIFIED_TLS` ;
- `DEGRADED_TLS` ;
- `HTTP`.

Une source n’est pas considérée réellement couverte uniquement parce que `sync_status=SUCCESS`. Il faut aussi regarder `delivery_status`, les lignes/fichiers et les warnings.

## 11. Vérifier une source précise

Trouver son dossier :

```bash
ivoiredata source-path civ_faostat
```

Inspecter ensuite :

```text
manifest.json
raw/
tables/
documents/
```

Points à vérifier dans le manifest :

```text
schema_version
sync.status
delivery.status
delivery.rows
freshness.status
transport.security
warnings
rights
```

## 12. Interroger les Parquet

Exemple via la CLI :

```bash
ivoiredata query civ_worldbank_wdi \
  "SELECT * FROM worldbank_wdi LIMIT 20"
```

Les fichiers Parquet peuvent aussi être consommés avec DuckDB, pandas, PyArrow ou le pipeline downstream.

## 13. API locale

Démarrage manuel :

```bash
uvicorn ivoiredata.api:app --host 127.0.0.1 --port 8000
```

Avec Docker, l’API est exposée par le service `api`.

Endpoints utiles :

```text
GET  /health
GET  /sources
GET  /status
GET  /coverage
GET  /audit
GET  /inventory
GET  /sources/{source_id}/path
POST /sync/{source_id}
GET  /search/documents
POST /query/source/{source_id}
```

Test rapide :

```bash
curl http://127.0.0.1:8000/health
```

## 14. Types de sources importantes

### Data.gouv.ci

Connecteur : `data_gouv_ci`.

Livraison cible : `FULL_STRUCTURED`.

### World Bank WDI / Projects

Connecteurs structurés, réponses archivées puis Parquet.

### ILOSTAT

Le backend CSV est utilisé. `obs_status` est conservé tel quel ; les valeurs révisées ne doivent pas être filtrées.

### FAOSTAT

Les ZIP bulk officiels sont archivés dans `raw/`, filtrés sur la Côte d’Ivoire puis matérialisés en Parquet.

### UNESCO UIS

Les définitions et données `geoUnit=CIV` sont archivées en JSON puis matérialisées en Parquet.

### Sites institutionnels

`public_web` produit du contenu documentaire. Même s’il existe des lignes Parquet de chunks texte, la livraison doit être `DOCUMENTS_ONLY` et non `FULL_STRUCTURED`.

### OpenStreetMap / Geofabrik

Le PBF est la donnée principale. La livraison doit être `SNAPSHOT_ONLY`.

### ANStat/NADA

La collecte automatisée est volontairement limitée aux métadonnées autorisées : `METADATA_ONLY`.

## 15. Warnings importants

### `EMPTY_AFTER_SUCCESS`

La requête a fonctionné mais aucune donnée exploitable n’a été produite. La source doit être investiguée.

### `SYNC_ERROR_WITH_STALE_DATA`

Le dernier run a échoué mais une ancienne livraison valide est conservée.

### `TLS_VERIFICATION_DISABLED`

La vérification TLS est désactivée pour un upstream mal configuré. La source doit apparaître `DEGRADED_TLS`.

### `METADATA_ONLY_SOURCE`

La limitation aux métadonnées est volontaire.

## 16. Que faire en cas d’erreur

Ordre recommandé :

1. `ivoiredata audit` ;
2. `ivoiredata status --public` ;
3. ouvrir le `manifest.json` de la source ;
4. consulter les logs Docker ;
5. vérifier l’URL upstream ;
6. vérifier `robots.txt` pour les crawlers ;
7. vérifier un éventuel changement d’API/format ;
8. corriger le connecteur et ajouter un test de régression ;
9. resynchroniser uniquement la source ;
10. refaire `ivoiredata audit`.

Ne jamais supprimer automatiquement une ancienne livraison valide simplement parce que l’upstream est temporairement indisponible.

## 17. Sauvegarde

À sauvegarder sur un second disque :

```text
data_lake/
.ivoiredata/
```

GitHub contient le code, les configurations et la documentation, pas le data lake réel.

Une sauvegarde simple peut être réalisée avec les outils du système (`rsync`, `robocopy`, sauvegarde disque, etc.).

## 18. Mise à jour du moteur sans perdre les données

```bash
git status
git pull
docker compose build
docker compose --profile run up -d
```

Puis :

```bash
docker compose exec api ivoiredata audit
```

Après une évolution de schéma de manifest, resynchroniser les sources pour régénérer leurs manifests si la release le demande.

## 19. Handoff downstream

Le pipeline de l’équipe modèle doit consommer :

```text
data_lake/catalog.json
data_lake/domains/**/manifest.json
data_lake/domains/**/tables/*.parquet
data_lake/domains/**/documents/*
data_lake/domains/**/raw/*
```

Le handoff doit respecter les droits déclarés dans les manifests et le registre.

Chaîne downstream recommandée :

```text
freeze/snapshot
→ validation des droits
→ nettoyage
→ filtres qualité
→ PII
→ déduplication
→ corpus versionné
→ tokenizer
→ tokenisation
→ packing/sharding
→ entraînement
```

Voir :

- `DATA_HANDOFF_CONTRACT.md` ;
- `DOWNSTREAM_AUTOMATION.md` ;
- `RIGHTS_AND_ACCESS.md`.

## 20. Validation avant une release gelée

Avant de déclarer une version stable :

```bash
python -m pytest -q
ivoiredata coverage
ivoiredata sync --all-public --force
ivoiredata audit
```

Avec Docker :

```bash
docker compose build
docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync --all-public --force"
docker compose exec api ivoiredata audit
```

Le bilan final doit distinguer au minimum :

```text
FULL_STRUCTURED
DOCUMENTS_ONLY
SNAPSHOT_ONLY
METADATA_ONLY
EMPTY
DEGRADED_TLS
```

Une release ne doit pas être gelée avec une source `EMPTY` présentée comme couverte ou avec une source non résolue incluse artificiellement dans le taux de couverture.
