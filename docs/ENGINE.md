# IvoireData Engine v0.7.0

IvoireData utilise **dlt OSS** pour Extract/Normalize/Load et ajoute registre des sources, routage, politiques d’accès, fraîcheur, provenance, snapshots locaux, classement domaine/source, manifests auditables et catalogue global.

## Responsabilité officielle

```text
source officielle
→ acquisition
→ conservation brute lorsque appropriée
→ normalisation dlt
→ Parquet/documents/snapshot
→ classement domaine/source
→ audit livraison
→ manifest v2
→ catalog global
→ mise à jour automatique
```

Le nettoyage ML avancé, PII corpus, dédup fuzzy, mixture, tokenizer, packing et shards sont hors du moteur ; voir [`DOWNSTREAM_AUTOMATION.md`](DOWNSTREAM_AUTOMATION.md).

## `IvoireDataEngine`

Responsabilités :

- résoudre la source et appliquer la politique d’accès ;
- créer son arborescence locale ;
- router vers un connecteur ;
- lancer un pipeline dlt isolé par source ;
- charger en Parquet lorsque structurable ;
- conserver raw/documents ;
- enregistrer fraîcheur succès/erreur ;
- calculer la livraison réelle ;
- écrire `manifest.json` ;
- reconstruire `catalog.json` ;
- produire `audit()`.

## `delivery.py` — manifest v2

Le manifest sépare :

```text
sync.status

delivery.status
  FULL_STRUCTURED
  DOCUMENTS_ONLY
  SNAPSHOT_ONLY
  METADATA_ONLY
  EMPTY

freshness.status
  FRESH
  DUE
  STALE
  NEVER_SYNCED

transport.security
  VERIFIED_TLS
  DEGRADED_TLS
  HTTP
```

Le moteur garde le champ top-level `status` pour compatibilité avec les anciens consommateurs.

Les lignes Parquet sont calculées à partir des métadonnées `num_rows`, sans scanner les tables complètes.

## Connecteurs spécialisés

- `data_gouv_ci` ;
- `world_bank_wdi` ;
- `world_bank_projects` ;
- `ilostat_ref_area` — CSV pays, aucun filtre sur `obs_status` ;
- `faostat_country` — ZIP bulk + filtre Côte d’Ivoire ;
- `uis_country` — UIS Data API + `geoUnit=CIV` ;
- `geoboundaries` ;
- `osm_geofabrik`.

Connecteurs génériques : `http_file`, `bulk_catalog`, `public_web`.

Voir [`CONNECTORS.md`](CONNECTORS.md).

## Freshness

`.ivoiredata/state/freshness.json` garde `last_attempt`, `last_status`, `last_success` et les détails. `refresh_hours` décide si une source est due.

Lorsqu’un nouvel essai échoue mais qu’une ancienne donnée valide existe, le manifest v2 expose `STALE` sans supprimer la livraison précédente.

## Audit

```bash
ivoiredata audit
```

et :

```text
GET /audit
```

lisent les manifests et l’état de fraîcheur pour fournir la couverture réellement exploitable. Voir [`AUDIT.md`](AUDIT.md).

## Permissions

- `OPEN` / `OPEN_PUBLIC` : ingestion automatique possible ;
- `MIXED` : uniquement `metadata_only=true` ;
- `D_*` : jamais en auto-sync.

## CLI

```bash
ivoiredata sources --public
ivoiredata coverage
ivoiredata status --public
ivoiredata audit
ivoiredata inventory
ivoiredata source-path SOURCE_ID
ivoiredata sync SOURCE_ID
ivoiredata sync --all-public --force
ivoiredata scheduler --once
ivoiredata scheduler --interval 3600
ivoiredata query SOURCE_ID "SELECT ..."
```

## Invariants v0.7

1. un `success` ne prouve pas une livraison ;
2. une source vide doit apparaître `EMPTY` ;
3. une erreur ne doit pas effacer une ancienne version valide ;
4. TLS dégradé doit être visible ;
5. la donnée réelle reste locale ;
6. un connecteur n’est déclaré validé qu’après test live local + audit.
