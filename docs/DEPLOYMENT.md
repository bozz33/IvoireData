# Déploiement local

IvoireData v0.4.1 stocke toutes les données sur la machine qui exécute le moteur. Aucun stockage S3/R2/MinIO n'est nécessaire.

## Installation Python

```bash
python -m pip install -e '.[dev,training]'
ivoiredata sources --public
ivoiredata sync civ_datagouv_catalog
ivoiredata status --public
```

Par défaut :

```text
data_lake/          données dlt
.ivoiredata/state/  état de fraîcheur/checkpoints
corpora/            corpus versionnés
tokenizer/          tokenizer local
```

Pour changer le dossier de données :

```text
IVOIREDATA_DATA_DIR=D:/IvoireData/data_lake
IVOIREDATA_STATE_DIR=D:/IvoireData/state
```

## Mise à jour automatique locale

Test unique :

```bash
ivoiredata scheduler --once
```

Processus continu, vérification toutes les heures :

```bash
ivoiredata scheduler --interval 3600
```

Le scheduler respecte `refresh_hours` et ne synchronise que les sources arrivées à échéance.

### Windows

Après installation de `ivoiredata`, le script `scripts/install_windows_scheduler.ps1` peut créer une tâche Windows horaire :

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_windows_scheduler.ps1
```

## API locale

```bash
uvicorn ivoiredata.api:app --host 127.0.0.1 --port 8000
```

L'API reste locale sur `http://127.0.0.1:8000`.

## Docker local

```bash
docker compose up --build
```

Docker monte directement `data_lake/`, `.ivoiredata/`, `corpora/` et `tokenizer/` depuis le PC. Aucun service de stockage externe n'est démarré.
