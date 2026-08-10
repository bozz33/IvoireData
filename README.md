# IvoireData 🇨🇮

**v0.8.1 — moteur local CI Gold de collecte, classification, audit et qualification des données publiques de Côte d’Ivoire**

IvoireData collecte et organise des sources publiques ivoiriennes, conserve provenance/droits, classe les contenus par domaine, ajoute `country_code=CIV`, gère les mises à jour manuelles/automatiques et mesure la couverture nationale sans confondre « source connue » et « donnée réellement livrée ».

## Objectif CI Gold

CI Gold ne signifie pas « toutes les informations qui existent en Côte d’Ivoire ». Il signifie que toutes les grandes familles nationales prioritaires ont été identifiées et évaluées avec un statut explicite :

```text
COVERED | PARTIAL | CONTROLLED | UNAVAILABLE | UNRESOLVED | MISSING
```

La matrice v2 couvre désormais plus de 50 familles : institutions, droit, finances publiques, élections, administration territoriale, population, migration, emploi, pauvreté, genre, jeunesse, protection sociale, économie, industrie, investissement, agriculture, santé, éducation, numérique, télécoms, cybersécurité publique, innovation, environnement, énergie, infrastructures, culture, sport, histoire, langues, etc.

## Registres

```text
registry/sources.csv                  socle historique
registry/ci_gold_completeness.csv     compléments institutionnels CI Gold
```

La v0.8.1 ajoute notamment : Femme/Famille/Enfant, Jeunesse, Commerce/Industrie, CEPICI, ministère du Numérique, Intérieur/Décentralisation, ONEF, Fonction publique, HABG, Défense, Assemblée nationale, Sénat, Conseil constitutionnel, Cour des comptes, CESEC, Présidence, Diplomatie, Solidarité/Pauvreté et MIRAH.

## Métadonnées nationales

Manifest/catalog schema v3 et documents portent notamment :

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

Les documents restent stockés une seule fois dans leur source canonique ; les métadonnées permettent les vues multidomaines sans duplication physique.

## Commandes principales

```bash
ivoiredata audit
ivoiredata coverage-audit
ivoiredata quality-audit
ivoiredata discoveries
ivoiredata qualification status
ivoiredata ci-gold
ivoiredata ci-gold --write
```

`discoveries` compare le catalogue Data.gouv.ci local aux mappings explicites du registre. Les nouveaux datasets sont seulement signalés : **aucune ingestion automatique sans revue domaine/droits**.

## PDF scannés

Les PDF avec trop peu de texte extractible sont conservés mais marqués :

```text
NEEDS_OCR
```

Un sidecar `*.needs_ocr.json` est créé. IvoireData **ne lance pas d’OCR automatiquement** ; l’audit qualité expose le nombre de documents à traiter.

## CI Gold gates

`ivoiredata ci-gold` exige notamment :

- score >=95 ;
- aucun P0 `MISSING/UNRESOLVED` ;
- aucun `EMPTY/ERROR` critique actif ;
- droits présents ;
- manifests v3 complets ;
- métadonnées documentaires complètes ;
- catalogue présent ;
- **14 jours réels de qualification automatique** ;
- au moins 14 cycles ;
- zéro erreur automatique ;
- toutes les sources automatiques actives réellement exercées au moins une fois.

Le logiciel v0.8.1 est une fondation/candidate CI Gold. Le data lake n’est « CI Gold final » que lorsque le run local retourne `approved=true`.

## Mises à jour dynamiques

```bash
ivoiredata updates status
ivoiredata updates disable
ivoiredata updates enable
ivoiredata updates interval 1800

ivoiredata source auto civ_faostat
ivoiredata source manual civ_faostat
ivoiredata source disable civ_faostat
ivoiredata source enable civ_faostat
ivoiredata source refresh civ_faostat 72
```

Les overrides persistent dans `.ivoiredata/state/runtime_overrides.json` et sont partagés entre API, scheduler et sync-once.

## Stockage

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

## Migration / validation locale

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

Quand le full sync et les audits sont propres :

```bash
docker compose exec api ivoiredata qualification start
```

Après la vraie fenêtre de qualification :

```bash
docker compose exec api ivoiredata qualification status
docker compose exec api ivoiredata ci-gold
docker compose exec api ivoiredata ci-gold --write
```

## API

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

## Frontière downstream

IvoireData s’arrête au data lake qualifié et aux preuves. Le downstream reste responsable de : validation finale des droits → nettoyage → PII → qualité → déduplication → corpus → tokenizer → shards → entraînement.

## Documentation

- [`docs/CI_GOLD.md`](docs/CI_GOLD.md)
- [`docs/CI_COVERAGE_MATRIX.md`](docs/CI_COVERAGE_MATRIX.md)
- [`docs/USAGE_GUIDE.md`](docs/USAGE_GUIDE.md)
- [`docs/AUDIT.md`](docs/AUDIT.md)
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
- [`docs/DATA_HANDOFF_CONTRACT.md`](docs/DATA_HANDOFF_CONTRACT.md)
- [`docs/RIGHTS_AND_ACCESS.md`](docs/RIGHTS_AND_ACCESS.md)

## Principes

1. Côte d’Ivoire uniquement jusqu’à CI Gold final.
2. Aucun contournement auth/CAPTCHA/paywall/licence.
3. `SUCCESS` technique ne signifie pas couverture.
4. Les sources contrôlées peuvent être correctement évaluées sans ingérer leurs payloads.
5. Les nouvelles découvertes ne sont jamais activées sans revue.
6. Une erreur upstream ne supprime pas l’ancienne livraison valide.
7. Le data lake réel reste hors Git.
