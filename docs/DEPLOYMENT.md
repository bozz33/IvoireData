# Déploiement local — v0.6.0

IvoireData stocke toutes les données sur la machine qui exécute le moteur. Aucun S3/R2/MinIO ni serveur PostgreSQL n'est requis.

## Installation Python

```bash
python -m pip install -e '.[dev]'
ivoiredata coverage
ivoiredata sources --public
ivoiredata inventory
```

Première synchronisation de contrôle :

```bash
ivoiredata sync civ_datagouv_catalog
ivoiredata sync civ_worldbank_wdi
ivoiredata sync civ_ilostat
ivoiredata sync civ_geoboundaries
```

Puis :

```bash
ivoiredata inventory
```

## Dossiers

```text
data_lake/
├── catalog.json
└── domains/
    └── <domain>/<source_id>/
        ├── raw/
        ├── tables/
        ├── documents/
        └── manifest.json

.ivoiredata/state/
```

Changer les chemins :

```text
IVOIREDATA_DATA_DIR=D:/IvoireData/data_lake
IVOIREDATA_STATE_DIR=D:/IvoireData/state
```

Utiliser de préférence des chemins absolus sur la machine d'exploitation.

## Mise à jour automatique

Une passe :

```bash
ivoiredata scheduler --once
```

Processus continu :

```bash
ivoiredata scheduler --interval 3600
```

Le scheduler se réveille toutes les heures mais respecte le `refresh_hours` propre à chaque source.

## Windows

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_windows_scheduler.ps1
```

La tâche Windows doit démarrer dans le dossier du projet ou recevoir `IVOIREDATA_DATA_DIR`, `IVOIREDATA_STATE_DIR`, `IVOIREDATA_REGISTRY` et `IVOIREDATA_RUNTIME_CONFIG` en chemins absolus.

## Linux

Utiliser systemd, supervisor ou un équivalent pour exécuter :

```bash
ivoiredata scheduler --interval 3600
```

Le service doit redémarrer automatiquement après reboot et conserver les mêmes chemins de données.

## API locale

```bash
uvicorn ivoiredata.api:app --host 127.0.0.1 --port 8000
```

N'utiliser `0.0.0.0` que si l'API doit être accessible depuis le LAN et après configuration du pare-feu.

## Docker local

Le dépôt fournit un `Dockerfile` multi-stage (image `ivoiredata:0.5.0`) et un `docker-compose.yml`
prêt pour la production locale. L'image tourne en non-root (user `ivoire`) et les UID/GID sont
configurables via `PUID`/`PGID` (défaut `1000:1000`) pour correspondre à l'utilisateur hôte et
éviter les problèmes de droits sur les bind-mounts.

### Build et services

```bash
# Construire l'image (inclut [training] + [dev], donc tokenizers et pytest).
docker compose build

# Démarrer uniquement l'API (avec healthcheck, restart: unless-stopped).
docker compose up -d api

# Démarrer l'API + le scheduler permanent (synchro toutes les heures).
docker compose --profile run up -d

# Une passe de synchro unique (toutes les sources dues), puis arrêt.
docker compose --profile sync run --rm sync-once

# Forcer la resynchronisation de toutes les sources publiques.
docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync --due --all-public --force"
```

L'API est exposée sur `http://127.0.0.1:8000` (`IVOIREDATA_API_PORT` pour changer le port).
Endpoints utiles : `/health`, `/coverage`, `/status?public_only=true`, `/sources`, `/sync/{source_id}`.

### Volumes persistants

| Volume hôte       | Chemin container       | Rôle                                     |
|-------------------|------------------------|------------------------------------------|
| `./data_lake`     | `/app/data_lake`       | tables dlt + raw_external + manifests     |
| `./.ivoiredata`   | `/app/.ivoiredata`     | fraîcheur / checkpoints                  |
| `./corpora`       | `/app/corpora`         | corpus versionnés (IvoireCorpus)         |
| `./tokenizer`     | `/app/tokenizer`       | tokenizer entraîné localement            |

Le service `init-volumes` (alpine, root) crée ces dossiers et les chown vers `PUID:PGID` avant
chaque démarrage : indispensable car Docker crée les bind-mounts en `root` et le container
applicatif est non-root.

### Variables d'environnement

```text
IVOIREDATA_API_PORT=8000             # port exposé par l'API
IVOIREDATA_SCHEDULER_INTERVAL=3600   # réveil du scheduler (secondes)
PUID=1000                            # UID de l'utilisateur applicatif (matcher l'hôte)
PGID=1000                            # GID
IVOIREDATA_DATASET_NAME=ivoiredata
IVOIREDATA_PIPELINE_NAME=ivoiredata_engine
```

### Adapter à un autre UID hôte

Si l'utilisateur de la machine hôte n'est pas `1000:1000` :

```bash
PGID=$(id -g) PUID=$(id -u) docker compose build
PGID=$(id -g) PUID=$(id -u) docker compose up -d api
```

### Lancer les tests dans l'image

```bash
docker run --rm -v "$PWD:/app" -w /app --entrypoint sh ivoiredata:0.5.0 \
  -c "python -m pytest -q"
```

### Sources connues en échec (problèmes serveur, non bugs moteur)

Ces sources peuvent échouer lors d'une synchro complète pour des raisons externes.
Elles sont marquées en `error` dans l'état de fraîcheur sans interrompre le reste de la synchro :

- `civ_anstat_nada` : `nada.anstat.ci` injoignable (SSL / connexion refusée).
- `civ_ilostat` : `pyreadr`/`librdata` segfault sur le `.rds` CIV (lib C tierce). Le parsing est
  isolé dans un subprocess dédié, le snapshot brut est conservé pour un connecteur spécialisé.
- `civ_worldbank_projects` : URL source pointant vers la racine de l'API WB (pas de page HTML).
  À remplacer par un connecteur dédié (voir `ADDING_SOURCE.md`).

## Sauvegarde

GitHub ne contient pas les données. Sauvegarder :

```text
data_lake/
.ivoiredata/
```

sur un second disque physique ou un autre support local contrôlé.

Les données produites ensuite par l'équipe modèle (snapshots, corpus, tokenizer, shards, checkpoints) appartiennent à son propre espace de travail et ne sont pas nécessaires au fonctionnement d'IvoireData.

Voir [`OPERATIONS.md`](OPERATIONS.md), [`STORAGE.md`](STORAGE.md) et [`DATA_HANDOFF_CONTRACT.md`](DATA_HANDOFF_CONTRACT.md).
