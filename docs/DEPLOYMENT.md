# Déploiement local — v0.5.0

IvoireData stocke toutes les données sur la machine qui exécute le moteur. Aucun S3/R2/MinIO ni serveur PostgreSQL n’est requis pour la V1.

## Installation Python

```bash
python -m pip install -e '.[dev,training]'
ivoiredata coverage
ivoiredata sources --public
ivoiredata status --public
```

Première synchronisation recommandée :

```bash
ivoiredata sync civ_datagouv_catalog
ivoiredata sync civ_worldbank_wdi
ivoiredata sync civ_ilostat
ivoiredata sync civ_geoboundaries
```

## Dossiers

```text
data_lake/             tables dlt
  raw_external/        OSM et gros fichiers sélectionnés
.ivoiredata/state/     fraîcheur/checkpoints
corpora/               corpus versionnés
tokenizer/             tokenizer local
```

Changer les chemins :

```text
IVOIREDATA_DATA_DIR=D:/IvoireData/data_lake
IVOIREDATA_STATE_DIR=D:/IvoireData/state
```

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

Après installation :

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_windows_scheduler.ps1
```

La tâche Windows doit démarrer dans le dossier du projet ou utiliser des variables d’environnement avec des chemins absolus.

## Linux

Le plus simple est un service systemd ou un processus supervisé exécutant :

```bash
ivoiredata scheduler --interval 3600
```

## API locale

```bash
uvicorn ivoiredata.api:app --host 127.0.0.1 --port 8000
```

N’utiliser `0.0.0.0` que si l’API doit être accessible depuis le LAN et après avoir configuré le pare-feu.

## Docker local

```bash
docker compose up --build
```

Docker monte directement les dossiers locaux du projet. Il ne lance aucun stockage externe.

## Sauvegarde

GitHub ne contient pas les données. Sauvegarder `data_lake/`, `.ivoiredata/`, `corpora/` et `tokenizer/` sur un autre disque.

Voir aussi [`OPERATIONS.md`](OPERATIONS.md) et [`STORAGE.md`](STORAGE.md).
