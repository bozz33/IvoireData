# Guide d’utilisation IvoireData v0.8.1

Ce guide couvre installation, collecte manuelle/automatique, contrôles dynamiques, audits CI Gold, découvertes Data.gouv, PDF `NEEDS_OCR`, qualification, sauvegarde et handoff downstream.

> IvoireData reste le moteur de collecte/livraison. Nettoyage ML avancé, PII, déduplication, corpus, tokenizer et entraînement restent downstream.

## 1. Installation / mise à jour

```bash
git clone https://github.com/bozz33/IvoireData.git
cd IvoireData
docker compose build
docker compose --profile run up -d
```

Mise à jour :

```bash
git status
git pull
docker compose build
docker compose --profile run up -d
```

Ne jamais supprimer `data_lake/` ou `.ivoiredata/` pour une mise à jour de code.

## 2. Stockage

```text
data_lake/
├── catalog.json
├── reports/ci-gold/
└── domains/<domain>/<source_id>/
    ├── raw/
    ├── tables/
    ├── documents/
    └── manifest.json

.ivoiredata/state/
├── freshness.json
├── runtime_overrides.json
└── ci_gold_qualification.json
```

Registres/config :

```text
registry/sources.csv
registry/ci_gold_completeness.csv
configs/runtime_sources.json
configs/ci_gold_sources.json
configs/ci_coverage.json
```

## 3. Diagnostic de base

```bash
ivoiredata --help
ivoiredata sources --public
ivoiredata coverage
ivoiredata audit
ivoiredata coverage-audit
ivoiredata quality-audit
ivoiredata discoveries
ivoiredata updates status
ivoiredata qualification status
ivoiredata ci-gold
```

Docker :

```bash
docker compose exec api ivoiredata audit
docker compose exec api ivoiredata coverage-audit
docker compose exec api ivoiredata quality-audit
docker compose exec api ivoiredata discoveries
```

## 4. Synchronisation manuelle

```bash
ivoiredata sync civ_faostat --force
ivoiredata sync --all-public --force
```

Docker :

```bash
docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync --all-public --force"
```

Le manuel reste disponible même avec l’automatique global désactivé.

## 5. Mise à jour automatique

```bash
ivoiredata updates status
ivoiredata updates disable
ivoiredata updates enable
ivoiredata updates interval 1800
```

Modes source :

```bash
ivoiredata source status civ_faostat
ivoiredata source auto civ_faostat
ivoiredata source manual civ_faostat
ivoiredata source disable civ_faostat
ivoiredata source enable civ_faostat
ivoiredata source refresh civ_faostat 72
```

```text
enabled=false                  -> DISABLED
enabled=true auto_sync=false   -> MANUAL
enabled=true auto_sync=true    -> AUTOMATIC
```

Les overrides persistent dans `.ivoiredata/state/runtime_overrides.json`.

## 6. Manifest v3 et métadonnées CIV

Le manifest sépare sync, delivery, fraîcheur et transport, puis ajoute :

```text
country_code=CIV
country_name=Côte d'Ivoire
source_domain
primary_domain
secondary_domains_json
language
document_type
geographic_scope
provider
rights_tier
access_tier
classification_status
classification_confidence
```

## 7. Delivery status

```text
FULL_STRUCTURED
DOCUMENTS_ONLY
SNAPSHOT_ONLY
METADATA_ONLY
EMPTY
```

`SUCCESS` technique ne signifie jamais automatiquement `COVERED`.

## 8. Classification et sources multidomaines

Les sources spécialisées utilisent leur domaine canonique. Data.gouv.ci/WDI et contenus multidomaines reçoivent une classification déterministe à partir de métadonnées, titre et URL.

Les données restent stockées une seule fois ; les métadonnées permettent le reclassement logique sans duplication physique.

## 9. Découvertes Data.gouv

```bash
ivoiredata discoveries
ivoiredata discoveries --limit 200
```

La commande compare le catalogue Data.gouv.ci local aux mappings explicites du registre et retourne `MAPPED`/`UNMAPPED`.

Important :

```text
discover -> review domain/rights -> register/configure -> sync
```

IvoireData ne synchronise jamais automatiquement une nouvelle découverte non revue.

## 10. PDF scannés / NEEDS_OCR

Un PDF avec trop peu de texte extractible est conservé mais marqué :

```text
extraction_status=NEEDS_OCR
```

Un sidecar est écrit :

```text
<snapshot>.needs_ocr.json
```

L’OCR automatique est volontairement désactivée. Le traitement OCR peut être effectué ultérieurement uniquement sur les documents prioritaires.

## 11. Audit normal

```bash
ivoiredata audit
```

Résumé :

```text
rows.structured
rows.documents
rows.total_parquet
transport.*
```

## 12. Coverage audit

```bash
ivoiredata coverage-audit
```

Matrice v2 : plus de 50 familles nationales. Statuts :

```text
COVERED PARTIAL CONTROLLED UNAVAILABLE UNRESOLVED MISSING
```

Un P0 `MISSING/UNRESOLVED` bloque CI Gold.

## 13. Quality audit

```bash
ivoiredata quality-audit
```

Contrôles principaux :

- manifest absent ;
- manifest schema <3 ;
- métadonnées CIV incomplètes ;
- droits absents ;
- `EMPTY`/`ERROR` ;
- colonnes documentaires manquantes ;
- fichiers zéro octet ;
- documents `NEEDS_OCR`.

`NEEDS_OCR` est un warning de traitement, pas une autorisation à OCRer automatiquement.

## 14. Qualification réelle

Après full sync propre :

```bash
ivoiredata qualification start
```

État :

```bash
ivoiredata qualification status
```

Pour réussir :

- >=14 jours réels ;
- >=14 cycles scheduler ;
- au moins une vraie sync automatique ;
- toutes les sources automatiques actives exercées ;
- zéro erreur automatique.

Les sync manuels ne comptent pas.

## 15. CI Gold

```bash
ivoiredata ci-gold
ivoiredata ci-gold --write
```

Le second écrit :

```text
data_lake/reports/ci-gold/
```

CI Gold final exige `approved=true` et tous les gates vrais.

## 16. API

```text
GET  /health
GET  /sources
GET  /status
GET  /coverage
GET  /coverage-audit
GET  /quality-audit
GET  /discoveries
GET  /audit
GET  /ci-gold
POST /ci-gold/report
GET  /qualification
POST /qualification/start
GET/PUT /settings/updates
GET/PUT /sources/{source_id}/settings
POST /sync/{source_id}
GET /search/documents
POST /query/source/{source_id}
```

## 17. Requête locale

```bash
ivoiredata query civ_worldbank_wdi \
  "SELECT * FROM worldbank_wdi LIMIT 20"
```

Query/search utilisent la même configuration effective (overlays CI Gold + overrides persistants) que le moteur.

## 18. Sauvegarde

Sauvegarder sur un second disque :

```text
data_lake/
.ivoiredata/
```

## 19. Incident upstream

1. `ivoiredata audit` ;
2. `ivoiredata quality-audit` ;
3. manifest ;
4. logs Docker ;
5. upstream ;
6. corriger + test ;
7. resync uniquement la source.

Ne pas supprimer l’ancienne livraison valide lors d’une panne upstream.

## 20. Droits

Les tiers A/B/C/D restent la référence. Aucun contournement d’authentification, CAPTCHA, paywall, consentement ou restriction de licence.

## 21. Handoff downstream

Le downstream consomme catalog, manifests, tables, documents, raw et rapports CI Gold, puis réalise : droits → nettoyage → PII → qualité → dédup → corpus → tokenizer → shards → entraînement.

## 22. Migration vers v0.8.1

```bash
git pull
docker compose build
docker compose --profile run up -d

docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync --all-public --force"

docker compose exec api ivoiredata audit
docker compose exec api ivoiredata coverage-audit
docker compose exec api ivoiredata quality-audit
docker compose exec api ivoiredata discoveries
```

Quand les audits sont propres :

```bash
docker compose exec api ivoiredata qualification start
```

Ne pas annoncer CI Gold final avant la vraie fin de qualification.
