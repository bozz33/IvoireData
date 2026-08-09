# Exploitation locale d’IvoireData v0.7

## Vérifier l’installation

```bash
ivoiredata coverage
ivoiredata sources --public
ivoiredata status --public
ivoiredata audit
ivoiredata inventory
```

- `coverage` : ce que le registre/config prévoit ;
- `status` : dernier résultat + métriques du manifest ;
- `audit` : couverture réellement livrée ;
- `inventory` : catalogue local complet.

## Synchroniser une source

```bash
ivoiredata sync civ_datagouv_catalog
ivoiredata sync civ_worldbank_wdi
ivoiredata sync civ_ilostat --force
ivoiredata sync civ_faostat --force
ivoiredata sync civ_uis --force
```

Après un sync :

```bash
ivoiredata audit
ivoiredata source-path civ_faostat
```

Ne jamais conclure à partir de `status=success` seul. Vérifier `delivery_status`, `rows`, fichiers raw et warnings.

## Synchronisation complète

```bash
ivoiredata sync --all-public --force
ivoiredata audit
```

Pour l’exploitation continue :

```bash
ivoiredata scheduler --interval 3600
```

Le scheduler respecte `refresh_hours` de chaque source.

## Dossiers

```text
data_lake/domains/<domain>/<source_id>/
├── raw/
├── tables/
├── documents/
└── manifest.json
```

Le manifest v2 contient `sync`, `delivery`, `freshness`, `transport`, `rights` et `warnings`.

## API

```bash
uvicorn ivoiredata.api:app --host 127.0.0.1 --port 8000
```

Endpoints :

```text
GET  /health
GET  /sources
GET  /status
GET  /coverage
GET  /audit
GET  /inventory
GET  /sources/{source_id}/path
POST /sync/{source_id}
GET  /search/documents
POST /query/source/{source_id}
```

## Incidents

### Upstream indisponible

Le dernier essai passe en `ERROR`. Si une ancienne livraison valide existe :

```text
sync_status      ERROR
delivery_status  <niveau existant>
freshness_status STALE
warning          SYNC_ERROR_WITH_STALE_DATA
```

Ne pas supprimer la dernière donnée valide.

### TLS cassé côté upstream

`verify_ssl=false` est un fallback explicite pour quelques sites institutionnels mal configurés. L’audit doit afficher :

```text
transport_security DEGRADED_TLS
warning            TLS_VERIFICATION_DISABLED
```

### Success mais aucune donnée

```text
sync_status      SUCCESS
delivery_status  EMPTY
warning          EMPTY_AFTER_SUCCESS
```

C’est un incident de couverture, pas une source considérée terminée.

### Changement API/format

1. isoler la source ;
2. conserver le raw déjà valide ;
3. corriger le connecteur ;
4. ajouter un test hors réseau ;
5. resynchroniser la source ;
6. vérifier `ivoiredata audit`.

## Requêtes locales

```bash
ivoiredata query civ_worldbank_wdi "SELECT * FROM worldbank_wdi LIMIT 20"
```

Les tables peuvent aussi être lues directement avec DuckDB, pandas ou PyArrow.

## Migration v0.6 → v0.7

Les manifests v0.6 ne contiennent pas les nouveaux champs. Après `git pull` et rebuild/install :

```bash
ivoiredata sync --all-public --force
ivoiredata audit
```

Cela réécrit les manifests v2 à partir des fichiers réellement présents.

## Sauvegarde

Sauvegarder sur un second disque :

```text
data_lake/
.ivoiredata/
```

GitHub garde uniquement le code/config/docs.

## Handoff équipe modèle

Le downstream consomme `catalog.json`, les `manifest.json`, `raw/`, `tables/` et `documents/`. Voir [`DATA_HANDOFF_CONTRACT.md`](DATA_HANDOFF_CONTRACT.md) et [`DOWNSTREAM_AUTOMATION.md`](DOWNSTREAM_AUTOMATION.md).
