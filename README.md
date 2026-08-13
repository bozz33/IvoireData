# IvoireData 🇨🇮

**v0.8.3 — moteur local CI Gold de collecte officielle, incrémentale, classification, audit et qualification des données publiques de Côte d’Ivoire**

IvoireData collecte et organise des sources publiques ivoiriennes, conserve provenance/droits, classe les contenus par domaine, ajoute `country_code=CIV`, gère les mises à jour manuelles/automatiques et mesure la couverture nationale sans confondre « source connue » et « donnée réellement livrée ».

## v0.8.3 : synchronisation incrémentale robuste

La vérification d'une source ne signifie plus que son contenu est retéléchargé. Le moteur utilise, dans cet ordre :

1. version officielle upstream (`DateUpdate`, `last.update`, `lastupdated`, etc.) ;
2. `ETag` / `Last-Modified` et HTTP `304 Not Modified` ;
3. SHA-256 lorsque le serveur n'expose aucun validateur.

L'état réseau est persistant dans :

```text
.ivoiredata/state/upstreams.json
```

Les états JSON sont écrits atomiquement. Un état corrompu est mis en quarantaine sous `*.corrupt-<timestamp>` au lieu d'empêcher le moteur de démarrer.

`--force` signifie désormais **vérifier maintenant**. Il ne force jamais volontairement le téléchargement d'une version identique déjà matérialisée.

Lorsqu'une grosse source structurée termine avec un backlog ou un échec partiel, le scheduler la retente sur une cadence courte (6 h par défaut, configurable avec `partial_retry_hours`) **sans `force`**. Les signatures officielles et le cache local empêchent donc de retransférer les artefacts déjà acquis ; seuls les éléments manquants, nouveaux ou modifiés sont repris. Un tel cycle est marqué `partial` pour la qualification CI Gold tant que le backlog n'est pas résorbé.

```bash
ivoiredata upstreams
ivoiredata upstreams civ_datagouv_catalog
ivoiredata upstreams civ_ilostat
ivoiredata upstreams civ_faostat
```

Détails : [`docs/UPSTREAM_INCREMENTAL.md`](docs/UPSTREAM_INCREMENTAL.md).

## Sources structurées principales

- **Data.gouv.ci / Data Fair** : catalogue public anonyme officiel ; `/full` en priorité puis fallback officiel `/lines`, avec `page>=1` et suivi du curseur `next` jusqu'à son absence.
- **ILOSTAT** : TOC officiel des indicateurs + API CSV par indicateur avec `id=<indicator>&ref_area=CIV`; `last.update` décide quels indicateurs doivent être redemandés. Le chemin RDS reste volontairement désactivé.
- **FAOSTAT** : catalogue bulk officiel `datasets_E.json`; tous les domaines courants sont découverts, les archives `Discontinued` sont exclues par défaut, et `DateUpdate/FileRows/FileSize` évitent le retéléchargement des ZIP inchangés.
- **World Bank WDI** : API V2 officielle ; `lastupdated` de la source WDI sert de signature globale.
- **World Bank Projects** : API Projects officielle ; HTTP validators + hash canonique.
- **UNESCO UIS** : API publique officielle ; HTTP validators par artefact + SHA fallback.
- **geoBoundaries** : API/GeoJSON ; validators + SHA.
- **OpenStreetMap / Geofabrik** : snapshot PBF ; checksum `.md5` officiel puis validators HTTP en fallback.
- **Portails institutionnels** : ETag/Last-Modified quand disponibles ; SHA-256 sinon. Un `304` conserve également les liens déjà découverts pour poursuivre le crawl.

## Objectif CI Gold

CI Gold ne signifie pas « toutes les informations qui existent en Côte d’Ivoire ». Il signifie que toutes les grandes familles nationales prioritaires ont été identifiées, évaluées et collectées autant que les sources publiques/droits le permettent :

```text
COVERED | PARTIAL | CONTROLLED | UNAVAILABLE | UNRESOLVED | MISSING
```

La matrice v2 couvre plus de 50 familles : institutions, droit, finances publiques, élections, administration territoriale, population, migration, emploi, pauvreté, genre, jeunesse, protection sociale, économie, industrie, investissement, agriculture, santé, éducation, numérique, télécoms, cybersécurité publique, innovation, environnement, énergie, infrastructures, culture, sport, histoire, langues, etc.

CI Gold bloque maintenant également si une grande source structurée annonce un **échec partiel** ou un **backlog de données non encore transférées**.

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

## Commandes principales

```bash
ivoiredata --version
ivoiredata audit
ivoiredata upstreams
ivoiredata coverage-audit
ivoiredata quality-audit
ivoiredata discoveries
ivoiredata qualification status
ivoiredata ci-gold
ivoiredata ci-gold --write
```

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
├── upstreams.json
└── ci_gold_qualification.json
```

## Mise à niveau v0.8.3

Pendant la migration, arrêter le scheduler, sauvegarder `.ivoiredata/` et le data lake, reconstruire l'image, puis migrer **les grandes sources structurées une par une**. Il n'est pas nécessaire de relancer immédiatement un `--all-public --force`.

```bash
git pull
docker compose build
docker compose --profile run up -d

docker compose exec api ivoiredata --version
docker compose exec api ivoiredata upstreams
```

Procédure complète et tests anti-retéléchargement : [`docs/UPSTREAM_INCREMENTAL.md`](docs/UPSTREAM_INCREMENTAL.md).

## API

```text
GET  /health
GET  /sources
GET  /status
GET  /coverage
GET  /coverage-audit
GET  /quality-audit
GET  /upstreams
GET  /upstreams/{source_id}
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
