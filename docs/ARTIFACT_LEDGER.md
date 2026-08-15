# Artifact Ledger — v0.8.4-A

IvoireData s'arrête à la livraison de données. Le `Artifact Ledger` est la couche de preuve physique entre l'état upstream et les fichiers réellement présents sur disque.

## Objectif

Un artefact ne doit jamais être présenté comme téléchargé simplement parce qu'un cache ou une signature indique qu'il l'a été par le passé. Le moteur distingue désormais l'état logique upstream de l'état physique local.

Base par défaut :

```text
.ivoiredata/state/artifact_ledger.sqlite3
```

Variable d'environnement optionnelle :

```text
IVOIREDATA_ARTIFACT_LEDGER=/chemin/artifact_ledger.sqlite3
```

## États physiques

- `DISCOVERED` : connu, sans preuve de matérialisation locale.
- `FETCHED` : fichier local présent après un téléchargement connu.
- `VERIFIED` : présence, taille et SHA-256 vérifiés localement.
- `UNCHANGED` : upstream inchangé et fichier local toujours présent.
- `LOCAL_MISSING` : l'état upstream prétend qu'une donnée existe, mais aucun fichier local exploitable n'est présent.
- `CORRUPTED` : taille ou SHA-256 incohérent.
- `FAILED` : récupération upstream en erreur.
- `REMOVED` / `DELETED` : tombstone upstream ; l'historique n'est pas effacé silencieusement.
- `EMPTY_VALID` : réservé aux ressources officiellement vides et explicitement qualifiées comme telles.

## Migration des états existants

`ivoiredata artifacts audit` importe les lignes de `.ivoiredata/state/upstreams.json` dans le ledger. Une ligne marquée `downloaded=true` mais sans `local_path` présent devient `LOCAL_MISSING`, jamais `FETCHED`.

Cette migration est idempotente : elle peut être relancée autant de fois que nécessaire.

## Run Ledger

Chaque appel à `IvoireDataEngine.sync()` crée désormais un `run_id` dans la même base SQLite. Il mémorise :

- source et connecteur ;
- `force=true/false` ;
- heure de début / fin ;
- statut final ;
- erreur éventuelle ;
- artefacts observés ;
- octets observés.

Le wrapper ne change pas la logique métier des connecteurs ni le fonctionnement CI Gold ; il observe l'état upstream produit par le sync.

## Commandes

Audit léger et import des états historiques :

```bash
ivoiredata artifacts audit
ivoiredata artifacts audit --source-id civ_datagouv_catalog
```

Vérification physique profonde (lecture des fichiers + SHA-256) :

```bash
ivoiredata artifacts verify
ivoiredata artifacts verify --source-id civ_datagouv_catalog
ivoiredata artifacts verify --source-id civ_datagouv_catalog --limit 20
```

La commande retourne un code non nul si elle détecte `LOCAL_MISSING` ou `CORRUPTED`.

Plan de réparation sans réseau :

```bash
ivoiredata artifacts repair
ivoiredata artifacts repair --source-id civ_datagouv_catalog
```

Exécution ciblée :

```bash
ivoiredata artifacts repair --source-id civ_datagouv_catalog --execute
```

Une réparation exécute uniquement les sources concernées avec `force=true`. Les connecteurs incrémentaux restent responsables de ne pas retélécharger les artefacts réellement inchangés. Par sécurité, une réparation globale est plafonnée à 20 sources, modifiable explicitement par `--max-sources`.

## API

`GET /artifacts` expose l'audit physique. `GET /health` retourne aussi le chemin du ledger.

## Garanties du lot v0.8.4-A

1. aucune ligne `FETCHED` n'est créée si `local_path` n'est pas un fichier existant ;
2. `verify` recalcule le SHA-256 et détecte les corruptions ;
3. les fichiers disparus deviennent `LOCAL_MISSING` ;
4. les réparations sont planifiables avant toute requête réseau ;
5. les syncs sont historisés par `run_id` ;
6. SQLite utilise WAL, `busy_timeout` et une version de schéma explicite ;
7. le scheduler n'est pas réactivé par cette version.

## Hors périmètre de ce lot

- streaming Data.gouv CI pour les très gros datasets ;
- client HTTP commun et budgets réseau globaux ;
- content-addressed storage ;
- harvesters npm/Cargo/NuGet/Go/Maven supplémentaires ;
- nettoyage, corpus, tokenizer et entraînement.

Ces éléments sont traités dans les lots suivants sans déplacer la frontière de responsabilité d'IvoireData : la sortie reste une livraison de données brutes/structurées traçables.
