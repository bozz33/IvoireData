# Déploiement IvoireData v0.8.0 — CI Gold

## Principe

IvoireData est déployé localement. Les données, états, overrides et preuves CI Gold restent sur la machine hôte.

Persistant :

```text
./data_lake
./.ivoiredata
```

Le code/config/docs viennent de Git.

## Prérequis

- Git ;
- Docker Engine/Desktop ;
- Docker Compose v2 ;
- accès Internet ;
- espace disque suffisant ;
- `PUID`/`PGID` adaptés si nécessaire.

## Construction

```bash
git pull
docker compose build
```

Image :

```text
ivoiredata:0.8.0
```

## Démarrage API + scheduler

```bash
docker compose --profile run up -d
```

Vérifier :

```bash
docker compose ps
docker compose exec api ivoiredata --help
docker compose exec api ivoiredata updates status
```

Health :

```bash
curl http://127.0.0.1:8000/health
```

La réponse doit annoncer `version=0.8.0` et `country_code=CIV`.

## Sync manuel ponctuel

```bash
docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync civ_faostat --force"
```

Full sync :

```bash
docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync --all-public --force"
```

## Migration depuis v0.7.2

v0.8.0 introduit manifest/catalog schema v3 et métadonnées CI Gold. Les données existantes ne doivent pas être supprimées.

Procédure :

```bash
git pull
docker compose build
docker compose --profile run up -d

docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync --all-public --force"
```

Puis :

```bash
docker compose exec api ivoiredata audit
docker compose exec api ivoiredata coverage-audit
docker compose exec api ivoiredata quality-audit
docker compose exec api ivoiredata ci-gold
```

Le full sync régénère les manifests v3 et les nouvelles colonnes documentaires.

## Qualification

Une fois les audits initiaux propres :

```bash
docker compose exec api ivoiredata qualification start
```

Laisser le scheduler fonctionner normalement. Consulter :

```bash
docker compose exec api ivoiredata qualification status
```

La qualification exige 14 jours réels, au moins 14 cycles et aucune erreur automatique.

Ne pas redémarrer artificiellement la qualification avec des dates modifiées sur une machine de production.

## Contrôles dynamiques

Global :

```bash
docker compose exec api ivoiredata updates disable
docker compose exec api ivoiredata updates enable
docker compose exec api ivoiredata updates interval 1800
```

Source :

```bash
docker compose exec api ivoiredata source manual civ_faostat
docker compose exec api ivoiredata source auto civ_faostat
docker compose exec api ivoiredata source disable civ_faostat
```

Les overrides vivent dans `.ivoiredata/state/runtime_overrides.json`, partagé par tous les conteneurs.

## Volumes

`docker-compose.yml` monte uniquement :

```text
./data_lake:/app/data_lake
./.ivoiredata:/app/.ivoiredata
```

Le corpus/tokenizer ne sont pas montés : ils appartiennent au pipeline downstream.

## Sauvegarde

Sauvegarder régulièrement :

```text
data_lake/
.ivoiredata/
```

Le second dossier est indispensable en v0.8.0 car il contient fraîcheur, préférences et qualification CI Gold.

## Mise à jour sans perte

```bash
git status
git pull
docker compose build
docker compose --profile run up -d
docker compose exec api ivoiredata audit
```

Ne jamais faire `rm -rf data_lake .ivoiredata` pour résoudre un problème de code.

## Arrêt

```bash
docker compose down
```

Les données persistantes restent sur l’hôte.

## Diagnostic

```bash
docker compose logs --tail=200 api
docker compose logs --tail=200 scheduler
docker compose exec api ivoiredata audit
docker compose exec api ivoiredata quality-audit
```

## Gel CI Gold

Après qualification réussie :

```bash
docker compose exec api ivoiredata ci-gold
```

Si `approved=true` :

```bash
docker compose exec api ivoiredata ci-gold --write
```

Archiver ensuite `data_lake/reports/ci-gold/` avec le snapshot downstream correspondant.

Une release logicielle v0.8.0 ne doit pas être confondue avec un snapshot CI Gold final tant que `approved=true` n’est pas obtenu localement.
