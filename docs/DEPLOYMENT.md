# Déploiement IvoireData v0.7

IvoireData est conçu pour fonctionner localement sur un PC ou serveur. Les données restent dans `data_lake/` et l’état opérationnel dans `.ivoiredata/`.

## Installation Python

```bash
git pull
python -m pip install -e '.[dev]'
ivoiredata --help
ivoiredata audit
```

## Docker

### Build

```bash
docker compose build
```

Le container tourne en non-root. `PUID` et `PGID` valent `1000` par défaut et peuvent être adaptés à l’utilisateur hôte :

```bash
PUID=$(id -u) PGID=$(id -g) docker compose build
PUID=$(id -u) PGID=$(id -g) docker compose up -d api
```

### Services

```bash
# API uniquement
docker compose up -d api

# API + scheduler permanent
docker compose --profile run up -d

# une passe de synchro due
docker compose --profile sync run --rm sync-once
```

Image : `ivoiredata:0.7.0`.

### Volumes

| Hôte | Container | Rôle |
|---|---|---|
| `./data_lake` | `/app/data_lake` | raw, Parquet, documents, manifests, catalog |
| `./.ivoiredata` | `/app/.ivoiredata` | fraîcheur/checkpoints |

`corpora/` et `tokenizer/` ne sont plus montés par IvoireData : ils appartiennent au pipeline downstream documenté séparément.

Le service `init-volumes` crée les dossiers et applique `PUID:PGID` avant le démarrage des services applicatifs.

## API

Par défaut :

```text
http://127.0.0.1:8000
```

Endpoints :

```text
GET /health
GET /sources
GET /status
GET /coverage
GET /audit
GET /inventory
POST /sync/{source_id}
```

Ne pas exposer l’API directement sur Internet sans authentification/reverse proxy approprié.

## Première validation v0.7

Après migration depuis v0.6 :

```bash
ivoiredata sync civ_ilostat --force
ivoiredata sync civ_faostat --force
ivoiredata sync civ_uis --force
ivoiredata sync civ_worldbank_projects --force
ivoiredata audit
```

Pour recalculer tous les manifests v2 :

```bash
ivoiredata sync --all-public --force
ivoiredata audit
```

Les chiffres annoncés comme « réellement couverts » doivent provenir de cet audit local, pas de la seule CI GitHub.

## Scheduler

```bash
ivoiredata scheduler --interval 3600
```

Le moteur se réveille toutes les heures et vérifie `refresh_hours`. Une source non due n’est pas retraitée.

## Résilience

- erreur upstream : dernière livraison valide conservée ;
- nouvelle erreur + ancienne donnée : `freshness_status=STALE` ;
- TLS désactivé pour un upstream mal configuré : `DEGRADED_TLS` + warning ;
- succès sans donnée : `delivery_status=EMPTY` + `EMPTY_AFTER_SUCCESS` ;
- source contrôlée : microdonnées exclues, métadonnées seulement si autorisé.

## Tests

```bash
python scripts/validate_registry.py
python scripts/validate_runtime_config.py
python -m compileall -q src scripts
pytest -q
```

Dans Docker :

```bash
docker run --rm -v "$PWD:/app" -w /app --entrypoint sh ivoiredata:0.7.0 \
  -c "python -m pytest -q"
```

## Sauvegarde

Sauvegarder régulièrement :

```text
data_lake/
.ivoiredata/
```

sur un second disque. GitHub ne constitue pas une sauvegarde des données réelles.
