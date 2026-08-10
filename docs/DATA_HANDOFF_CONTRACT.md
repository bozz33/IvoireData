# Contrat de sortie IvoireData → pipeline downstream — CI Gold v0.8.0

Ce document définit la frontière entre le data lake vivant IvoireData et le pipeline de préparation du corpus/modèle.

## Responsabilité IvoireData

IvoireData livre des données :

- récupérées depuis des sources déclarées ;
- stockées localement ;
- classées par domaine/source ;
- accompagnées de provenance, droits et statut d’accès ;
- enrichies avec les métadonnées nationales CI Gold ;
- versionnables par hash/checksum lorsque disponible ;
- mises à jour manuellement ou automatiquement ;
- auditées par livraison, couverture, qualité et fraîcheur ;
- sans contourner authentification, CAPTCHA, paywall ou contrôle d’accès.

IvoireData ne décide pas du corpus final et ne réalise pas le nettoyage ML avancé, la PII, la déduplication, le tokenizer ou l’entraînement.

## Arborescence de livraison

```text
data_lake/
├── catalog.json
├── reports/ci-gold/
└── domains/
    └── <domain>/
        └── <source_id>/
            ├── raw/
            ├── tables/
            ├── documents/
            └── manifest.json
```

### `raw/`

Payload source original lorsque sa conservation locale est autorisée : JSON, CSV, XLS/XLSX, PDF, ZIP, PBF, etc. Les sidecars peuvent contenir URL, date, MIME, taille et SHA-256.

### `tables/`

Parquet produits par le pipeline dlt par source. Pour les documents Web, les lignes peuvent être des chunks documentaires ; `delivery_status` permet de distinguer ces lignes des données métier structurées.

### `documents/`

Snapshots/pages/PDF récupérés par les connecteurs documentaires.

### `manifest.json` — schema v3

Le manifest est la carte d’identité opérationnelle de la source. Exemple simplifié :

```json
{
  "schema_version": 3,
  "source_id": "civ_faostat",
  "country_code": "CIV",
  "country_name": "Côte d'Ivoire",
  "domain": "agriculture",
  "provider": "FAO",
  "source_url": "...",
  "rights_tier": "B_SOURCE_TERMS",
  "access_tier": "OPEN",
  "connector": "faostat_country",
  "delivery_status": "FULL_STRUCTURED",
  "freshness_status": "FRESH",
  "transport_security": "VERIFIED_TLS",
  "metadata": {
    "source_domain": "agriculture",
    "primary_domain": "agriculture",
    "secondary_domains_json": "[]",
    "language": "fr",
    "geographic_scope": "NATIONAL",
    "classification_status": "CONFIGURED",
    "classification_confidence": 1.0
  },
  "delivery": {},
  "freshness": {},
  "transport": {},
  "rights": {},
  "warnings": []
}
```

### `catalog.json` — schema v3

Le catalogue contient l’index global du data lake, `country_code=CIV`, les domaines, les sources et un `domain_index`. Le downstream doit utiliser ce catalogue comme point d’entrée plutôt que dépendre du code interne d’IvoireData.

## Métadonnées minimales downstream

Pour chaque unité exploitable, directement dans la table/document ou via son manifest, le downstream doit pouvoir retrouver :

```text
country_code
country_name
source_id
provider
source_url
source_domain
primary_domain
secondary_domains
language
document_type
geographic_scope
rights_tier
access_tier
retrieved_at / dates de sync
content hash / checksum lorsque disponible
chemin local
```

Les champs manquants doivent être traités comme `UNKNOWN`/non classifiés, jamais inventés.

## Sources multidomaines

Les fichiers restent stockés à un emplacement canonique unique. Les métadonnées `primary_domain` et `secondary_domains` permettent la recherche/catégorisation sans duplication physique.

Data.gouv.ci et WDI peuvent porter des champs `__ivoiredata_*` au niveau dataset/indicateur pour conserver cette classification fine.

## Preuves CI Gold

Lorsque demandées :

```bash
ivoiredata ci-gold --write
```

IvoireData produit :

```text
data_lake/reports/ci-gold/
├── audit.json
├── coverage.json
├── quality.json
├── qualification.json
├── sources.json
├── ci-gold-report.json
└── ci-gold-report.md
```

Le pipeline downstream peut archiver ces fichiers avec le snapshot utilisé pour un entraînement.

## Data lake vivant vs snapshot figé

```text
IvoireData vivant
      │
      │ sync continu
      ▼
Snapshot choisi
      │
      ├── catalog.json
      ├── manifests
      ├── payloads/tables/documents
      └── preuves CI Gold
      │
      ▼
validation des droits
→ nettoyage
→ PII
→ qualité
→ déduplication
→ corpus versionné
→ tokenizer
→ tokenisation
→ packing/sharding
→ entraînement
```

Le downstream doit enregistrer une copie/hash du catalogue, des manifests et des preuves utilisées afin qu’un entraînement soit reproductible.

## CI Gold Candidate vs CI Gold final

Une version logicielle v0.8.0 peut être opérationnelle alors que le data lake reste `CI Gold Candidate`. Le passage à CI Gold final exige notamment :

- full sync réel après migration ;
- aucune source active critique `EMPTY`/`ERROR` ;
- audits couverture/qualité satisfaits ;
- métadonnées documentaires migrées ;
- qualification automatique réelle de 14 jours ;
- `ivoiredata ci-gold` → `approved=true`.

Le downstream ne doit pas transformer le label logiciel `v0.8.0` en affirmation de qualité du snapshot.

## Droits

Le downstream doit respecter `rights_tier` et `access_tier` lors de la construction du corpus. Une source `CONTROLLED` ou D n’est pas automatiquement éligible à l’entraînement parce que ses métadonnées existent dans le catalogue.

## Règle essentielle

**Le data lake n’est pas le corpus.** IvoireData livre et qualifie les sources ; le corpus est une sélection/transformée figée produite par le pipeline downstream à partir d’un état documenté du data lake.
