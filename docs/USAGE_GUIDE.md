# Guide d’utilisation IvoireData v0.7.2

Ce guide couvre l’installation, le démarrage, les synchronisations manuelles et automatiques, le contrôle dynamique des sources, l’audit, l’API, la sauvegarde et le handoff downstream.

> IvoireData s’arrête à la livraison du data lake. Le nettoyage ML avancé, la déduplication du corpus, le tokenizer et l’entraînement appartiennent au pipeline downstream.

## 1. Prérequis

### Docker — recommandé

- Git ;
- Docker Engine / Docker Desktop ;
- Docker Compose v2 ;
- accès Internet aux sources publiques ;
- espace disque suffisant pour `data_lake/`.

### Sans Docker

- Python 3.10+ ;
- Git ;
- environnement virtuel Python recommandé.

## 2. Installation / mise à jour

```bash
git clone https://github.com/bozz33/IvoireData.git
cd IvoireData
```

Si le dépôt existe déjà :

```bash
git status
git pull
```

Ne jamais supprimer `data_lake/` ou `.ivoiredata/` pendant une mise à jour.

Sans Docker :

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell : .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

## 3. Démarrage Docker

```bash
docker compose build
docker compose --profile run up -d
```

Vérifier :

```bash
docker compose ps
docker compose logs -f api
docker compose logs -f scheduler
```

Arrêt :

```bash
docker compose down
```

Les données et réglages dynamiques restent sur l’hôte.

## 4. Stockage

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

.ivoiredata/state/
├── freshness.json
└── runtime_overrides.json
```

- `raw/` : snapshots/fichiers source ;
- `tables/` : Parquet ;
- `documents/` : pages Web/PDF ;
- `manifest.json` : provenance, droits, métriques, fraîcheur, transport, warnings ;
- `freshness.json` : dernier état de synchronisation ;
- `runtime_overrides.json` : réglages utilisateur AUTO/MANUAL/DISABLED et scheduler.

`configs/runtime_sources.json` reste la configuration par défaut versionnée. Les changements utilisateur sont séparés dans `.ivoiredata/state/runtime_overrides.json` afin de survivre aux rebuilds Docker et d’être partagés entre `api`, `scheduler` et `sync-once`.

## 5. Diagnostic de base

```bash
ivoiredata --help
ivoiredata coverage
ivoiredata sources --public
ivoiredata status --public
ivoiredata audit
ivoiredata inventory
ivoiredata updates status
```

Avec Docker :

```bash
docker compose exec api ivoiredata audit
docker compose exec api ivoiredata updates status
```

## 6. Synchronisation manuelle

Une source :

```bash
ivoiredata sync civ_worldbank_wdi
ivoiredata sync civ_faostat --force
```

Toutes les sources publiques actives :

```bash
ivoiredata sync --all-public --force
```

Avec Docker :

```bash
docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync civ_faostat --force"
```

ou :

```bash
docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync --all-public --force"
```

La synchronisation manuelle reste disponible même lorsque les mises à jour automatiques sont globalement désactivées.

## 7. Contrôle global des mises à jour automatiques

État :

```bash
ivoiredata updates status
```

Désactiver l’automatique :

```bash
ivoiredata updates disable
```

Réactiver :

```bash
ivoiredata updates enable
```

Changer l’intervalle de réveil du scheduler, minimum 300 secondes :

```bash
ivoiredata updates interval 1800
```

Avec Docker, exécuter par exemple :

```bash
docker compose exec api ivoiredata updates disable
docker compose exec api ivoiredata updates enable
docker compose exec api ivoiredata updates interval 1800
```

Le scheduler relit l’état persistant à chaque cycle. `updates disable` empêche réellement ses synchronisations ; il continue seulement à tourner en attente d’une éventuelle réactivation.

## 8. Modes par source

Voir le mode :

```bash
ivoiredata source status civ_faostat
```

Automatique :

```bash
ivoiredata source auto civ_faostat
```

Manuel uniquement :

```bash
ivoiredata source manual civ_faostat
```

Désactiver totalement :

```bash
ivoiredata source disable civ_faostat
```

Réactiver :

```bash
ivoiredata source enable civ_faostat
```

Changer `refresh_hours` :

```bash
ivoiredata source refresh civ_faostat 72
```

Sémantique :

```text
enabled=false                         -> DISABLED
enabled=true + auto_sync=false       -> MANUAL
enabled=true + auto_sync=true        -> AUTOMATIC
```

Une source `DISABLED` est exclue du scheduler, de `sync --all-public` et des appels directs `sync <source_id>` jusqu’à réactivation.

Voir aussi [`DYNAMIC_UPDATES.md`](DYNAMIC_UPDATES.md).

## 9. Scheduler

Une passe :

```bash
ivoiredata scheduler --once
```

Permanent :

```bash
ivoiredata scheduler
```

Intervalle ponctuel explicite :

```bash
ivoiredata scheduler --interval 1800
```

Avec Docker :

```bash
docker compose --profile run up -d scheduler
```

Par défaut, le scheduler utilise `scheduler_interval_seconds` du runtime persistant. Une variable d’environnement `IVOIREDATA_SCHEDULER_INTERVAL` explicitement définie peut la remplacer pour un déploiement particulier.

## 10. Lire l’audit

```bash
ivoiredata audit
```

### `sync_status`

- `SUCCESS` ;
- `ERROR` ;
- `NEVER`.

### `delivery_status`

- `FULL_STRUCTURED` : données métier structurées ;
- `DOCUMENTS_ONLY` : pages/PDF/chunks texte ;
- `SNAPSHOT_ONLY` : snapshot brut/binaire, ex. OSM PBF ;
- `METADATA_ONLY` : métadonnées publiques uniquement ;
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

Ne jamais utiliser `sync_status=SUCCESS` seul comme preuve de couverture.

## 11. Vérifier une source

```bash
ivoiredata source-path civ_faostat
ivoiredata source status civ_faostat
```

Inspecter ensuite :

```text
manifest.json
raw/
tables/
documents/
```

Champs importants :

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

## 12. Requêtes locales

```bash
ivoiredata query civ_worldbank_wdi \
  "SELECT * FROM worldbank_wdi LIMIT 20"
```

Les Parquet sont aussi utilisables avec DuckDB, pandas ou PyArrow.

## 13. API locale

```bash
uvicorn ivoiredata.api:app --host 127.0.0.1 --port 8000
```

Endpoints principaux :

```text
GET  /health
GET  /sources
GET  /status
GET  /coverage
GET  /audit
GET  /inventory
GET  /settings/updates
PUT  /settings/updates
GET  /sources/{source_id}/settings
PUT  /sources/{source_id}/settings
GET  /sources/{source_id}/path
POST /sync/{source_id}
GET  /search/documents
POST /query/source/{source_id}
```

Exemple pour couper l’automatique :

```bash
curl -X PUT http://127.0.0.1:8000/settings/updates \
  -H "Content-Type: application/json" \
  -d '{"automatic_enabled":false}'
```

Exemple source en mode manuel :

```bash
curl -X PUT http://127.0.0.1:8000/sources/civ_faostat/settings \
  -H "Content-Type: application/json" \
  -d '{"update_mode":"MANUAL","refresh_hours":72}'
```

## 14. Connecteurs clés

- Data.gouv.ci : données structurées `/full` ;
- World Bank WDI / Projects : API structurée + raw ;
- ILOSTAT : CSV CIV, `obs_status` conservé ;
- FAOSTAT : ZIP bulk filtrés Côte d’Ivoire ;
- UNESCO UIS : API `geoUnit=CIV` ;
- `public_web` : documents, pas données métier structurées ;
- OSM/Geofabrik : PBF `SNAPSHOT_ONLY` ;
- ANStat/NADA : `METADATA_ONLY` selon politique actuelle.

## 15. Warnings

- `EMPTY_AFTER_SUCCESS` : succès technique mais aucune donnée utile ;
- `SYNC_ERROR_WITH_STALE_DATA` : ancienne livraison conservée après échec upstream ;
- `TLS_VERIFICATION_DISABLED` : TLS non vérifié, donc `DEGRADED_TLS` ;
- `METADATA_ONLY_SOURCE` : limitation volontaire aux métadonnées.

## 16. Diagnostic incident

Ordre conseillé :

1. `ivoiredata audit` ;
2. `ivoiredata updates status` ;
3. `ivoiredata status --public` ;
4. ouvrir le `manifest.json` ;
5. lire les logs Docker ;
6. vérifier l’upstream ;
7. corriger le connecteur si nécessaire ;
8. ajouter un test de régression ;
9. resynchroniser uniquement la source ;
10. refaire l’audit.

Une erreur upstream ne doit jamais effacer automatiquement la dernière livraison valide.

## 17. Sauvegarde

Sauvegarder sur un second disque :

```text
data_lake/
.ivoiredata/
```

Le second dossier contient à la fois la fraîcheur et les préférences dynamiques. GitHub contient le code/config/docs, pas les données réelles.

## 18. Mise à jour du moteur sans perdre les réglages

```bash
git pull
docker compose build
docker compose --profile run up -d
```

Puis :

```bash
docker compose exec api ivoiredata updates status
docker compose exec api ivoiredata audit
```

Les réglages dynamiques restent dans `.ivoiredata/state/runtime_overrides.json` et ne sont pas écrasés par le `git pull` ou le rebuild.

## 19. Handoff downstream

Le downstream consomme notamment :

```text
data_lake/catalog.json
data_lake/domains/**/manifest.json
data_lake/domains/**/tables/*.parquet
data_lake/domains/**/documents/*
data_lake/domains/**/raw/*
```

Chaîne :

```text
freeze/snapshot
→ droits
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

Voir `DATA_HANDOFF_CONTRACT.md`, `DOWNSTREAM_AUTOMATION.md` et `RIGHTS_AND_ACCESS.md`.

## 20. Validation avant release

```bash
python -m pytest -q
ivoiredata coverage
ivoiredata updates status
ivoiredata sync --all-public --force
ivoiredata audit
```

Le bilan doit distinguer au minimum :

```text
FULL_STRUCTURED
DOCUMENTS_ONLY
SNAPSHOT_ONLY
METADATA_ONLY
EMPTY
DEGRADED_TLS
```

Et tester explicitement :

```text
updates disable -> scheduler ne synchronise rien
sync manuel -> fonctionne malgré updates disable
source manual -> absente de la sélection automatique
source disable -> bloquée même en sync direct
rebuild/restart -> runtime_overrides.json conservé
```
