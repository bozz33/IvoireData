# Exploitation locale d’IvoireData v0.6

Ce document décrit l’usage quotidien du moteur sur un PC ou serveur local.

## Vérifier l’installation

```bash
ivoiredata coverage
ivoiredata sources --public
ivoiredata status --public
ivoiredata inventory
```

- `coverage` : couverture du registre ;
- `status` : fraîcheur et dernier résultat par source ;
- `inventory` : état du `data_lake/catalog.json` avec domaines et sources réellement synchronisés.

## Synchroniser une source

```bash
ivoiredata sync civ_datagouv_catalog
ivoiredata sync civ_worldbank_wdi
ivoiredata sync civ_ilostat
```

Forcer une vérification :

```bash
ivoiredata sync civ_dgi --force
```

Après succès, vérifier :

```bash
ivoiredata source-path civ_dgi
ivoiredata inventory
```

La source doit avoir un dossier :

```text
data_lake/domains/<domain>/<source_id>/
├── raw/
├── tables/
├── documents/
└── manifest.json
```

## Synchroniser les sources dues

```bash
ivoiredata scheduler --once
```

Scheduler permanent :

```bash
ivoiredata scheduler --interval 3600
```

Le scheduler se réveille toutes les heures mais respecte `refresh_hours` propre à chaque source.

## Première alimentation complète

Pour essayer toutes les sources publiques configurées :

```bash
ivoiredata sync --all-public
```

Cette commande peut être longue et certains upstream peuvent échouer temporairement. Une erreur d'une source n'efface pas les données déjà livrées par les autres.

Pour les opérations quotidiennes, préférer le scheduler plutôt qu'un `--all-public` forcé.

## Requêtes locales

Chaque source est interrogée séparément :

```bash
ivoiredata query civ_worldbank_wdi "SELECT * FROM worldbank_wdi LIMIT 20"
```

Les fichiers `tables/` étant en Parquet, ils peuvent aussi être lus directement avec DuckDB, pandas, PyArrow ou le pipeline de l'équipe modèle.

## API locale

```bash
uvicorn ivoiredata.api:app --host 127.0.0.1 --port 8000
```

Endpoints utiles :

```text
GET  /health
GET  /sources
GET  /status
GET  /coverage
GET  /inventory
GET  /sources/{source_id}/path
POST /sync/{source_id}
GET  /search/documents
POST /query/source/{source_id}
```

## Windows

`scripts/install_windows_scheduler.ps1` crée une tâche planifiée locale. Le compte Windows utilisé doit avoir accès au dossier IvoireData, au dossier de données et à Internet.

## Diagnostic

Ordre recommandé :

1. `ivoiredata status --public` ;
2. `ivoiredata inventory` ;
3. `data_lake/domains/<domain>/<source>/manifest.json` ;
4. `.ivoiredata/state/freshness.json` ;
5. espace disque ;
6. URL upstream ;
7. `robots.txt` pour les crawlers ;
8. changement d'API/format côté producteur.

## Politique d'incident

- **source indisponible** : garder la dernière donnée locale et enregistrer l'erreur ;
- **payload invalide** : ne pas marquer la synchronisation réussie ;
- **schéma upstream modifié** : adapter connecteur + tests ;
- **licence/conditions modifiées** : désactiver `auto_sync` pendant réévaluation ;
- **disque plein** : arrêter les gros snapshots avant corruption, libérer/déplacer le data lake puis reprendre ;
- **fichier `.part`** : considérer le téléchargement comme incomplet ;
- **manifest erreur** : le `catalog.json` doit refléter l'état et ne pas transformer l'erreur en succès.

## Sauvegarde

Sauvegarder :

```text
data_lake/
.ivoiredata/
```

sur un second disque. GitHub contient seulement le code/config/docs.

## Handoff équipe modèle

IvoireData ne lance plus officiellement `corpus-build`/`tokenizer-train`. L'équipe modèle consomme :

```text
data_lake/catalog.json
+
data_lake/domains/.../manifest.json
+
raw/ tables/ documents/
```

Voir [`DATA_HANDOFF_CONTRACT.md`](DATA_HANDOFF_CONTRACT.md) et [`DOWNSTREAM_AUTOMATION.md`](DOWNSTREAM_AUTOMATION.md).
