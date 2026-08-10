# IvoireData 🇨🇮

**v0.8.0 — moteur local CI Gold de collecte, classification, mise à jour, audit et livraison de données de Côte d’Ivoire**

IvoireData collecte les sources publiques utiles à la Côte d’Ivoire, conserve leur provenance et leurs droits, classe les données par domaine, ajoute des métadonnées nationales `country_code=CIV`, détecte les mises à jour et livre les données localement. GitHub contient le code, la configuration et la documentation ; le data lake réel reste sur la machine.

## CI Gold

La v0.8.0 introduit le programme **CI Gold**. L’objectif n’est pas de prétendre posséder « toute information existant en Côte d’Ivoire », mais de rendre **chaque grande famille nationale prioritaire explicitement mesurée** : `COVERED`, `PARTIAL`, `CONTROLLED`, `UNAVAILABLE`, `UNRESOLVED` ou `MISSING`.

Commandes principales :

```bash
ivoiredata coverage-audit
ivoiredata quality-audit
ivoiredata qualification status
ivoiredata ci-gold
ivoiredata ci-gold --write
```

`ci-gold --write` produit le dossier de preuve local :

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

CI Gold ne peut être approuvé que lorsque tous les gates sont vrais, notamment : score ≥95, aucun P0 manquant/non résolu, aucun `EMPTY`/`ERROR` actif, droits complets, métadonnées documentaires complètes et **14 jours réels de qualification automatique sans erreur**.

## Métadonnées nationales v3

Le manifest v3 et les documents collectés portent notamment :

```text
country_code = CIV
country_name = Côte d'Ivoire
source_domain
primary_domain
secondary_domains
language
document_type
geographic_scope
provider
rights_tier
access_tier
classification_status
classification_confidence
```

Les sources spécialisées restent classées par leur domaine. Pour les sources `multidomain`, IvoireData applique des règles déterministes sur les métadonnées/titres afin de produire un domaine principal et des domaines secondaires sans nécessiter de LLM.

## Sources CI Gold ajoutées

La couverture institutionnelle est renforcée avec :

- Secrétariat Général du Gouvernement — textes officiels / Journal officiel ;
- DGBF — budget, lois de finances, Budget citoyen ;
- MESRS — enseignement supérieur et recherche ;
- CEI — élections, résultats, textes et statistiques ;
- AGEROUTE — réseau routier et banque de données ;
- ANARE-CI — régulation et données électriques ;
- Ministère de la Culture ;
- Ministère du Tourisme ;
- Ministère de la Communication ;
- Ministère des Sports ;
- portail officiel du Gouvernement.

Ces sources sont configurées dans `configs/ci_gold_sources.json`. Les sources privées, contrôlées, soumises à authentification ou à licence incompatible ne sont jamais contournées.

## Stockage local

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

.ivoiredata/state/
├── freshness.json
├── runtime_overrides.json
└── ci_gold_qualification.json
```

## Mise à jour manuelle / automatique

L’automatisation reste entièrement désactivable :

```bash
ivoiredata updates status
ivoiredata updates disable
ivoiredata updates enable
ivoiredata updates interval 1800
```

Par source :

```bash
ivoiredata source status civ_faostat
ivoiredata source auto civ_faostat
ivoiredata source manual civ_faostat
ivoiredata source disable civ_faostat
ivoiredata source enable civ_faostat
ivoiredata source refresh civ_faostat 72
```

Les choix utilisateur sont persistés dans `.ivoiredata/state/runtime_overrides.json` et partagés entre `api`, `scheduler` et `sync-once`.

## Démarrage Docker

```bash
git pull
docker compose build
docker compose --profile run up -d
```

Validation :

```bash
docker compose exec api ivoiredata audit
docker compose exec api ivoiredata coverage-audit
docker compose exec api ivoiredata quality-audit
```

Après passage à v0.8.0, effectuer une synchronisation complète forcée afin de régénérer les manifests v3 et enrichir les anciens Parquet documentaires :

```bash
docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync --all-public --force"
```

Puis démarrer la qualification :

```bash
docker compose exec api ivoiredata qualification start
```

## API locale

Endpoints importants :

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
```

## Frontière de responsabilité

```text
sources publiques CIV
        ↓
IvoireData
collecte / provenance / classification / audit / fraîcheur
        ↓
data_lake CI Gold
        ↓
pipeline downstream
rights validation / nettoyage / PII / dédup / corpus / tokenizer / training
```

IvoireData ne réalise pas le nettoyage ML avancé, la déduplication du corpus, le tokenizer ni l’entraînement.

## Documentation

- [`docs/CI_GOLD.md`](docs/CI_GOLD.md) — spécification et gates CI Gold ;
- [`docs/CI_COVERAGE_MATRIX.md`](docs/CI_COVERAGE_MATRIX.md) — couverture nationale ;
- [`docs/USAGE_GUIDE.md`](docs/USAGE_GUIDE.md) — exploitation ;
- [`docs/AUDIT.md`](docs/AUDIT.md) — audit et manifests ;
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — architecture ;
- [`docs/DATA_HANDOFF_CONTRACT.md`](docs/DATA_HANDOFF_CONTRACT.md) — contrat downstream ;
- [`docs/RIGHTS_AND_ACCESS.md`](docs/RIGHTS_AND_ACCESS.md) — droits.

## Principes

1. Côte d’Ivoire uniquement jusqu’à validation CI Gold.
2. Aucun contournement d’authentification, CAPTCHA, paywall ou contrôle d’accès.
3. Une source `SUCCESS` n’est pas automatiquement considérée comme couverte.
4. Une source `EMPTY`, non résolue ou contrôlée ne doit pas être présentée comme une livraison complète.
5. Les droits et la provenance restent attachés aux données jusqu’au handoff.
6. Une erreur upstream ne supprime pas la dernière livraison valide.
7. Le data lake réel et les réglages locaux ne sont pas stockés dans Git.
