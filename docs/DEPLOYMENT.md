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

```bash
docker compose up --build
```

Docker doit monter `data_lake/` et `.ivoiredata/` depuis le PC. Aucun stockage externe ne doit être démarré.

## Sauvegarde

GitHub ne contient pas les données. Sauvegarder :

```text
data_lake/
.ivoiredata/
```

sur un second disque physique ou un autre support local contrôlé.

Les données produites ensuite par l'équipe modèle (snapshots, corpus, tokenizer, shards, checkpoints) appartiennent à son propre espace de travail et ne sont pas nécessaires au fonctionnement d'IvoireData.

Voir [`OPERATIONS.md`](OPERATIONS.md), [`STORAGE.md`](STORAGE.md) et [`DATA_HANDOFF_CONTRACT.md`](DATA_HANDOFF_CONTRACT.md).
