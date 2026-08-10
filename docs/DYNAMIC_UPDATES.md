# Mises à jour dynamiques — IvoireData v0.7.2

IvoireData prend en charge deux modes indépendants :

- synchronisation manuelle, toujours disponible pour une source active ;
- synchronisation automatique via scheduler, désactivable globalement et source par source.

Les réglages modifiables ne sont pas écrits dans `configs/runtime_sources.json`. Ce fichier reste la configuration par défaut livrée avec le code. Les changements locaux sont persistés dans :

```text
.ivoiredata/state/runtime_overrides.json
```

Ce chemin appartient au volume `.ivoiredata/` déjà partagé par les services Docker `api`, `scheduler` et `sync-once`. Un changement effectué dans un conteneur est donc vu par les autres et survit aux rebuilds/restarts.

## États d'une source

```text
enabled=false                         -> DISABLED
enabled=true + auto_sync=false       -> MANUAL
enabled=true + auto_sync=true        -> AUTOMATIC
```

Une source `DISABLED` est exclue des listes opérationnelles, du scheduler et des full sync. Un appel direct `sync <source_id>` est refusé tant qu'elle n'est pas réactivée.

## Voir l'état global

```bash
ivoiredata updates status
```

La sortie indique notamment :

- `automatic_enabled` ;
- `scheduler_interval_seconds` ;
- sources publiques automatiques ;
- sources publiques manuelles ;
- sources contrôlées ;
- sources désactivées ;
- chemin du fichier d'overrides.

Avec Docker :

```bash
docker compose exec api ivoiredata updates status
```

## Désactiver/réactiver toutes les mises à jour automatiques

```bash
ivoiredata updates disable
ivoiredata updates enable
```

Lorsque l'automatique est désactivé, le scheduler ne lance aucune synchronisation. Les commandes manuelles restent disponibles :

```bash
ivoiredata sync civ_faostat --force
ivoiredata sync --all-public --force
```

## Modifier l'intervalle de réveil du scheduler

Minimum : 300 secondes.

```bash
ivoiredata updates interval 1800
```

Le scheduler relit la configuration persistante à chaque cycle. Avec Docker, la commande normale est :

```bash
docker compose --profile run up -d scheduler
```

Le compose ne force plus `--interval 3600`. Si `IVOIREDATA_SCHEDULER_INTERVAL` est explicitement défini dans l'environnement, cette variable reste un override d'exploitation prioritaire.

## Contrôler une source

Voir son état :

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

Désactiver :

```bash
ivoiredata source disable civ_faostat
```

Réactiver sans modifier son dernier choix `auto_sync` :

```bash
ivoiredata source enable civ_faostat
```

Modifier sa fenêtre de fraîcheur :

```bash
ivoiredata source refresh civ_faostat 72
```

## API

État global :

```text
GET /settings/updates
```

Modifier :

```text
PUT /settings/updates
```

Exemple JSON :

```json
{
  "automatic_enabled": false,
  "scheduler_interval_seconds": 1800
}
```

État d'une source :

```text
GET /sources/{source_id}/settings
```

Modifier :

```text
PUT /sources/{source_id}/settings
```

Exemples :

```json
{
  "update_mode": "MANUAL",
  "refresh_hours": 72
}
```

ou :

```json
{
  "update_mode": "DISABLED"
}
```

Valeurs de `update_mode` : `AUTOMATIC`, `MANUAL`, `DISABLED`.

## Docker et persistance

Les paramètres dynamiques sont dans `.ivoiredata/`, donc les opérations suivantes ne les effacent pas :

```bash
docker compose down
docker compose build
docker compose --profile run up -d
```

Pour revenir aux paramètres par défaut du dépôt, sauvegarder puis supprimer uniquement :

```text
.ivoiredata/state/runtime_overrides.json
```

Ne pas supprimer `freshness.json` ou le data lake pour réinitialiser les modes de mise à jour.

## Règles importantes

1. `updates disable` ne bloque jamais une synchronisation manuelle explicitement demandée.
2. `source manual` empêche uniquement le scheduler de sélectionner cette source.
3. `source disable` bloque aussi un `sync <source_id>` direct jusqu'à réactivation.
4. `sync --all-public --force` respecte `enabled=false`.
5. les sources contrôlées par les droits restent hors ingestion automatique, indépendamment du mode demandé.
