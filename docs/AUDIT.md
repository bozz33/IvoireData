# Audit IvoireData v0.7

La v0.7 sépare **l’exécution d’une synchronisation** de **la qualité réelle de la livraison**. Cette distinction évite qu’une source soit considérée couverte uniquement parce que la requête HTTP ou le pipeline dlt s’est terminé sans exception.

## Commande

```bash
ivoiredata audit
```

Par défaut, l’audit porte sur les sources publiques synchronisables. Utiliser :

```bash
ivoiredata audit --all
```

pour inclure les sources manuelles/contrôlées du registre.

API :

```text
GET /audit
```

## Dimensions

### `sync_status`

- `SUCCESS` : le dernier run s’est terminé sans exception ;
- `ERROR` : le dernier run a échoué ;
- `NEVER` : source non encore exécutée.

### `delivery_status`

- `FULL_STRUCTURED` : au moins une table Parquet métier contient des lignes ;
- `DOCUMENTS_ONLY` : pas de table structurée, mais documents/pages réellement archivés ;
- `SNAPSHOT_ONLY` : payload brut réel, sans représentation structurée ;
- `METADATA_ONLY` : source volontairement limitée aux métadonnées publiques ;
- `EMPTY` : aucune livraison exploitable constatée.

Le comptage des lignes Parquet utilise les métadonnées de fichier (`num_rows`) et non une lecture complète des datasets.

### `freshness_status`

- `FRESH` : dernière version valide encore dans sa fenêtre `refresh_hours` ;
- `DUE` : dernière version valide existe mais une nouvelle vérification est due ;
- `STALE` : le dernier essai a échoué alors qu’une ancienne version valide existe ;
- `NEVER_SYNCED` : aucune version valide enregistrée.

### `transport_security`

- `VERIFIED_TLS` : HTTPS avec validation certificat ;
- `DEGRADED_TLS` : HTTPS avec `verify_ssl=false` pour un upstream au certificat cassé/incomplet ;
- `HTTP` : source en HTTP non chiffré.

## Warnings

- `EMPTY_AFTER_SUCCESS` : le pipeline a réussi mais n’a livré aucun payload/table/document ;
- `SYNC_ERROR_WITH_STALE_DATA` : le dernier run a échoué mais des données antérieures restent disponibles ;
- `TLS_VERIFICATION_DISABLED` : TLS non vérifié pour cette source ;
- `METADATA_ONLY_SOURCE` : la politique limite volontairement la livraison aux métadonnées.

## Règle de couverture

Ne jamais déduire la couverture à partir de `sync_status` seul.

Exemple :

```text
civ_faostat
sync_status      SUCCESS
delivery_status  EMPTY
```

signifie : le code a terminé, mais aucune donnée utile n’a été livrée.

À l’inverse :

```text
civ_treasury_debt
sync_status      ERROR
delivery_status  DOCUMENTS_ONLY
freshness_status STALE
```

signifie : l’upstream échoue actuellement mais la dernière livraison valide reste disponible localement.

## Manifest v2

Chaque `manifest.json` conserve les anciens champs principaux pour compatibilité et ajoute :

```json
{
  "status": "success",
  "delivery_status": "FULL_STRUCTURED",
  "freshness_status": "FRESH",
  "transport_security": "VERIFIED_TLS",
  "sync": {},
  "delivery": {
    "rows": 102296,
    "table_files": 3,
    "raw_files": 10,
    "document_files": 0
  },
  "freshness": {},
  "transport": {},
  "rights": {},
  "warnings": []
}
```

## Après une mise à jour du moteur

Les anciens manifests v0.6 ne contiennent pas encore toutes ces métriques. Le moyen recommandé de les migrer est de resynchroniser les sources :

```bash
ivoiredata sync --all-public --force
ivoiredata audit
```

Les données ne sont pas envoyées vers GitHub : l’audit porte uniquement sur le data lake local.
