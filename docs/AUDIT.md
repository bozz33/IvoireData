# Audit IvoireData v0.8.0

IvoireData distingue désormais **l’exécution**, **la livraison**, **la fraîcheur**, **le transport**, **la couverture nationale** et **la qualité CI Gold**.

## Audit de livraison

```bash
ivoiredata audit
```

API : `GET /audit`.

Dimensions :

- `sync_status` : `SUCCESS`, `ERROR`, `NEVER` ;
- `delivery_status` : `FULL_STRUCTURED`, `DOCUMENTS_ONLY`, `SNAPSHOT_ONLY`, `METADATA_ONLY`, `EMPTY` ;
- `freshness_status` : `FRESH`, `DUE`, `STALE`, `NEVER_SYNCED` ;
- `transport_security` : `VERIFIED_TLS`, `DEGRADED_TLS`, `HTTP`.

Le résumé sépare :

```text
rows.structured
rows.documents
rows.total_parquet
transport.*
```

Les lignes Parquet sont comptées via leurs métadonnées, sans scanner tout le contenu.

## Manifest v3

Exemple simplifié :

```json
{
  "schema_version": 3,
  "source_id": "civ_faostat",
  "country_code": "CIV",
  "country_name": "Côte d'Ivoire",
  "domain": "agriculture",
  "status": "success",
  "delivery_status": "FULL_STRUCTURED",
  "freshness_status": "FRESH",
  "transport_security": "VERIFIED_TLS",
  "metadata": {
    "country_code": "CIV",
    "source_domain": "agriculture",
    "primary_domain": "agriculture",
    "language": "fr",
    "geographic_scope": "NATIONAL",
    "rights_tier": "B_SOURCE_TERMS",
    "classification_status": "CONFIGURED"
  },
  "sync": {},
  "delivery": {},
  "freshness": {},
  "transport": {},
  "rights": {},
  "warnings": []
}
```

## Warnings

- `EMPTY_AFTER_SUCCESS` ;
- `SYNC_ERROR_WITH_STALE_DATA` ;
- `TLS_VERIFICATION_DISABLED` ;
- `METADATA_ONLY_SOURCE`.

## Audit de couverture

```bash
ivoiredata coverage-audit
```

Compare la matrice `configs/ci_coverage.json` aux sources enregistrées et aux livraisons locales.

Statuts : `COVERED`, `PARTIAL`, `CONTROLLED`, `UNAVAILABLE`, `UNRESOLVED`, `MISSING`.

Un P0 manquant/non résolu bloque CI Gold.

## Audit qualité

```bash
ivoiredata quality-audit
```

Contrôle notamment :

- manifest présent ;
- metadata CIV complète ;
- droits présents ;
- aucune livraison P0 vide ;
- aucune erreur P0 ;
- colonnes documentaires CI Gold présentes dans les Parquet Web.

## Audit CI Gold

```bash
ivoiredata ci-gold
```

Le résultat expose :

```text
score
approved
components
gates
coverage
quality
qualification
audit_summary
```

Pour écrire les preuves :

```bash
ivoiredata ci-gold --write
```

## Migration depuis v0.7.x

Les manifests v2 et anciens Parquet documentaires sont lisibles mais ne satisfont pas nécessairement les nouvelles métadonnées. Après mise à jour :

```bash
ivoiredata sync --all-public --force
ivoiredata audit
ivoiredata quality-audit
```

Le full sync régénère les manifests v3 et enrichit les lignes documentaires.

## Règle de couverture

Ne jamais annoncer une couverture à partir de `SUCCESS` seul. La couverture nationale doit provenir de `coverage-audit`, et CI Gold final exige `approved=true` après la qualification réelle de stabilité.
