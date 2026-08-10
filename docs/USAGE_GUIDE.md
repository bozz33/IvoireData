# Guide d’utilisation IvoireData v0.8.0

Ce guide couvre l’installation, la collecte manuelle/automatique, les contrôles dynamiques, les audits CI Gold, la qualification de stabilité, la sauvegarde et le handoff downstream.

> IvoireData reste le moteur de collecte/livraison. Nettoyage ML avancé, PII, déduplication, corpus, tokenizer et entraînement restent downstream.

## 1. Installation

Prérequis recommandés : Git, Docker Engine/Desktop, Docker Compose v2, accès Internet et espace disque local.

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

Ne jamais supprimer `data_lake/` ou `.ivoiredata/` lors d’une mise à jour.

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

- `configs/runtime_sources.json` : configuration historique/par défaut ;
- `configs/ci_gold_sources.json` : overlay versionné CI Gold ;
- `.ivoiredata/state/runtime_overrides.json` : choix utilisateur persistants ;
- `configs/ci_coverage.json` : matrice nationale de couverture.

## 3. Diagnostic de base

```bash
ivoiredata --help
ivoiredata sources --public
ivoiredata coverage
ivoiredata audit
ivoiredata coverage-audit
ivoiredata quality-audit
ivoiredata updates status
ivoiredata qualification status
ivoiredata ci-gold
```

Avec Docker :

```bash
docker compose exec api ivoiredata audit
docker compose exec api ivoiredata coverage-audit
docker compose exec api ivoiredata quality-audit
```

## 4. Synchronisation manuelle

Une source :

```bash
ivoiredata sync civ_faostat --force
```

Toutes les sources publiques actives :

```bash
ivoiredata sync --all-public --force
```

Docker :

```bash
docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync --all-public --force"
```

La synchronisation manuelle reste possible même si l’automatisation globale est coupée.

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

Sémantique :

```text
enabled=false                   -> DISABLED
enabled=true, auto_sync=false   -> MANUAL
enabled=true, auto_sync=true    -> AUTOMATIC
```

Les overrides sont persistés dans `.ivoiredata/state/runtime_overrides.json` et partagés entre les conteneurs.

## 6. Manifest v3

Chaque source reçoit un manifest avec quatre dimensions opérationnelles :

- `sync.status` ;
- `delivery.status` ;
- `freshness.status` ;
- `transport.security`.

Et une section nationale `metadata` contenant notamment :

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

- `FULL_STRUCTURED` : vraies données métier structurées ;
- `DOCUMENTS_ONLY` : pages/PDF/chunks textuels ;
- `SNAPSHOT_ONLY` : payload brut/binaire, ex. OSM PBF ;
- `METADATA_ONLY` : limitation volontaire ;
- `EMPTY` : aucune livraison exploitable.

`SUCCESS` ne suffit jamais à déclarer une source couverte.

## 8. Classification des documents

Les documents `public_web` reçoivent directement les métadonnées CI Gold dans le Parquet. Pour une source à domaine unique, le domaine de la source est canonique. Pour une source multidomaine, les titres/métadonnées sont classifiés avec des règles déterministes conservatrices.

Data.gouv.ci et World Bank WDI reçoivent aussi des champs `__ivoiredata_*` au niveau dataset/indicateur.

## 9. Audit normal

```bash
ivoiredata audit
```

Le résumé distingue maintenant :

```text
rows.structured
rows.documents
rows.total_parquet
transport.VERIFIED_TLS
transport.DEGRADED_TLS
```

## 10. Audit de couverture nationale

```bash
ivoiredata coverage-audit
```

La matrice est `configs/ci_coverage.json`.

Statuts : `COVERED`, `PARTIAL`, `CONTROLLED`, `UNAVAILABLE`, `UNRESOLVED`, `MISSING`.

Un P0 `MISSING`/`UNRESOLVED` bloque CI Gold.

## 11. Audit qualité

```bash
ivoiredata quality-audit
```

Il vérifie notamment :

- manifest ;
- métadonnées nationales ;
- droits ;
- `EMPTY`/`ERROR` P0 ;
- colonnes documentaires CI Gold.

Après migration depuis v0.7.x, un full sync forcé est requis afin de régénérer les manifests v3 et les Parquet documentaires enrichis.

## 12. Qualification 14 jours

Démarrer après un full sync propre :

```bash
ivoiredata qualification start
```

Consulter :

```bash
ivoiredata qualification status
```

Le scheduler enregistre automatiquement les cycles. Les sync manuels ne comptent pas.

Qualification réussie seulement avec :

- >=14 jours réels ;
- >=14 cycles scheduler ;
- 0 cycle automatique avec erreur ;
- 0 sync automatique en erreur.

## 13. CI Gold

```bash
ivoiredata ci-gold
```

Le score combine couverture, qualité/provenance, classification, fraîcheur, stabilité, droits et handoff.

Pour écrire le dossier de preuve :

```bash
ivoiredata ci-gold --write
```

Sortie : `data_lake/reports/ci-gold/`.

CI Gold final exige `approved=true`, pas seulement un score élevé.

## 14. API

Endpoints principaux :

```text
GET  /health
GET  /sources
GET  /status
GET  /coverage
GET  /coverage-audit
GET  /quality-audit
GET  /audit
GET  /ci-gold
POST /ci-gold/report
GET  /qualification
POST /qualification/start
GET  /settings/updates
PUT  /settings/updates
GET  /sources/{source_id}/settings
PUT  /sources/{source_id}/settings
POST /sync/{source_id}
GET  /search/documents
POST /query/source/{source_id}
```

## 15. Interroger les Parquet

```bash
ivoiredata query civ_worldbank_wdi \
  "SELECT * FROM worldbank_wdi LIMIT 20"
```

Les Parquet peuvent aussi être lus avec DuckDB, pandas et PyArrow.

## 16. Sauvegarde

Sauvegarder sur un second disque :

```text
data_lake/
.ivoiredata/
```

Le second dossier contient les checkpoints, overrides et la qualification CI Gold.

## 17. Incident upstream

Ordre conseillé :

1. `ivoiredata audit` ;
2. `ivoiredata quality-audit` ;
3. inspecter `manifest.json` ;
4. lire les logs Docker ;
5. vérifier l’upstream ;
6. corriger le connecteur si nécessaire ;
7. ajouter un test ;
8. resynchroniser uniquement la source.

Une erreur upstream ne doit pas supprimer la dernière livraison valide.

## 18. Droits

Les tiers A/B/C/D restent la politique de référence. Ne jamais contourner authentification, CAPTCHA, paywall, consentement ou restriction de licence pour améliorer artificiellement la couverture.

## 19. Handoff downstream

Le downstream consomme :

```text
data_lake/catalog.json
data_lake/domains/**/manifest.json
data_lake/domains/**/tables/*.parquet
data_lake/domains/**/documents/*
data_lake/domains/**/raw/*
data_lake/reports/ci-gold/*
```

Puis : droits → nettoyage → PII → qualité → dédup → corpus → tokenizer → packing/sharding → entraînement.

## 20. Migration v0.7.2 → v0.8.0

```bash
git pull
docker compose build
docker compose --profile run up -d

docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync --all-public --force"

docker compose exec api ivoiredata audit
docker compose exec api ivoiredata coverage-audit
docker compose exec api ivoiredata quality-audit
```

Lorsque ces contrôles sont propres :

```bash
docker compose exec api ivoiredata qualification start
```

Ne pas annoncer CI Gold final avant la fin réelle de la fenêtre de qualification.
