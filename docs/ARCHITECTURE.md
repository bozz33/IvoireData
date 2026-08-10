# Architecture IvoireData v0.8.0 — CI Gold

IvoireData reste un moteur **local-first / local data plane** : Git contient code/config/docs ; la machine contient les payloads, manifests, overrides et preuves CI Gold.

## Vue générale

```text
registry/sources.csv
configs/runtime_sources.json
configs/ci_gold_sources.json
configs/ci_coverage.json
          │
          ▼
    SourceRegistry
          ▲
          │ merge
.ivoiredata/state/runtime_overrides.json
          │
          ▼
      Engine
          │
  Connector Router
          │
 API / Web / PDF / CSV / Bulk / Geo
          │
          ▼
         dlt
          │
 pipeline PAR SOURCE
          │
          ▼
data_lake/domains/<domain>/<source_id>/
├── raw/
├── tables/
├── documents/
└── manifest.json  (schema v3)
          │
          ├──► catalog.json
          ├──► audit
          ├──► coverage-audit
          ├──► quality-audit
          └──► ci-gold
```

## Control plane

- `registry/sources.csv` : identité, domaine source, provider, URL, droits, priorité ;
- `configs/runtime_sources.json` : paramètres de base historiques ;
- `configs/ci_gold_sources.json` : overlay versionné pour les sources CI Gold ;
- `.ivoiredata/state/runtime_overrides.json` : AUTO/MANUAL/DISABLED et fréquences utilisateur ;
- `configs/ci_coverage.json` : taxonomie/matrice de couverture nationale ;
- `.ivoiredata/state/freshness.json` : fraîcheur ;
- `.ivoiredata/state/ci_gold_qualification.json` : fenêtre de stabilité réelle.

Ordre de fusion :

```text
runtime_sources.json
→ ci_gold_sources.json
→ runtime_overrides.json
```

Les choix locaux gardent donc la priorité et survivent aux rebuilds.

## Data plane

Chaque source garde un emplacement canonique unique :

```text
data_lake/domains/<source_domain>/<source_id>/
```

Un contenu multidomaine n’est pas dupliqué physiquement. Ses lignes portent `primary_domain` et `secondary_domains_json` pour permettre les index croisés downstream.

## Métadonnées nationales

Le manifest v3 ajoute une section `metadata` avec :

```text
country_code=CIV
country_name=Côte d'Ivoire
source_domain
primary_domain
secondary_domains_json
language
document_type
geographic_scope
rights/access
classification_status/confidence
```

Les documents Web portent ces métadonnées dans leurs Parquet. Data.gouv.ci et WDI ajoutent des champs `__ivoiredata_*` au niveau dataset/indicateur.

## Classification

La classification privilégie : domaine source → config → metadata upstream → titre/URL → règles lexicales déterministes. En cas d’incertitude, le système conserve `multidomain/PARTIAL` au lieu d’inventer.

## Fraîcheur et scheduler

```text
scheduler
  ↓
automatic_enabled ?
  ├─ non → aucun sync automatique
  └─ oui
       ↓
source enabled + auto_sync + due ?
       ↓
connector
       ↓
manifest/catalog/freshness
       ↓
QualificationStore.record_cycle()
```

Les sync manuels ne sont pas comptés dans la qualification CI Gold.

## CI Gold

```text
audit                état de livraison
coverage-audit       couverture nationale
quality-audit        qualité/métadonnées/droits
qualification        stabilité 14 jours
        │
        ▼
      ci-gold
        │
        ▼
approved=true uniquement si tous les gates passent
```

Les preuves sont générées sous `data_lake/reports/ci-gold/`.

## Sécurité et droits

- pas de contournement d’authentification/CAPTCHA/paywall ;
- les sources D restent hors ingestion automatique ;
- `metadata_only` ne permet que le contenu public autorisé ;
- le TLS dégradé reste explicitement signalé ;
- les droits suivent les données jusqu’au handoff.

## Frontière downstream

IvoireData livre le data lake CI. Le downstream prend ensuite un snapshot/freeze et réalise : droits → nettoyage → PII → qualité → dédup → corpus → tokenizer → shards → entraînement.

IvoireData ne doit pas absorber ces responsabilités.
