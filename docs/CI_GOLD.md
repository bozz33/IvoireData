# IvoireData — spécification CI Gold v0.8.1

## Périmètre

CI Gold concerne **uniquement la Côte d’Ivoire**. Il ne signifie pas « toutes les informations existant dans le pays », mais que toutes les grandes familles nationales prioritaires sont identifiées, évaluées et suivies par un statut explicite :

```text
COVERED | PARTIAL | CONTROLLED | UNAVAILABLE | UNRESOLVED | MISSING
```

La source de vérité machine-readable est `configs/ci_coverage.json` (matrice v2).

## Registres et configuration

```text
registry/sources.csv
registry/ci_gold_completeness.csv
configs/runtime_sources.json
configs/ci_gold_sources.json
.ivoiredata/state/runtime_overrides.json
```

Ordre de priorité : base → overlays CI Gold versionnés → overrides utilisateur persistants.

## Métadonnées nationales

Manifest/catalog schema v3. Les documents et sources portent notamment :

```text
country_code=CIV
country_name=Côte d'Ivoire
source_id
provider
source_domain
primary_domain
secondary_domains_json
language
document_type
geographic_scope
rights_tier
access_tier
classification_status
classification_confidence
retrieved_at
```

Les sources spécialisées gardent leur domaine canonique. Les sources multidomaines sont classées avec des règles déterministes et conservatrices ; aucune classification LLM n’est nécessaire au chemin normal.

## Couverture nationale v2

La matrice v2 dépasse 50 familles et inclut notamment : institutions de la République, finances publiques, droit/justice, élections, décentralisation, fonction publique, anti-corruption, population, migration, emploi, pauvreté, genre, jeunesse, handicap, économie, industrie, investissement, agriculture, santé, éducation, enseignement supérieur, recherche, télécoms, numérique, cybersécurité publique, innovation, médias, mines, hydrocarbures, électricité, environnement, biodiversité, géographie, foncier, eau, transport, routes, protection civile, défense, diplomatie, tourisme, culture, sport, histoire, langues, nouchi/français ivoirien et gastronomie.

Les corpus linguistiques/culturels soumis à droits peuvent rester `CONTROLLED`. Cela vaut mieux qu’une ingestion illégitime.

Un domaine **P0** doit être réellement `COVERED`. `PARTIAL`, `MISSING` et `UNRESOLVED` sont des blockers CI Gold.

## Sources institutionnelles complétées

En plus du socle v0.8.0, v0.8.1 ajoute notamment : Femme/Famille/Enfant, Jeunesse, Commerce/Industrie, CEPICI, ministère du Numérique, Intérieur/Décentralisation, ONEF, Fonction publique, HABG, Défense, Assemblée nationale, Sénat, Conseil constitutionnel, Cour des comptes, CESEC, Présidence, Diplomatie, Solidarité/Pauvreté et MIRAH.

## Découverte de nouvelles ressources

```bash
ivoiredata discoveries
```

compare le catalogue Data.gouv.ci réellement synchronisé avec les mappings explicites du registre.

Workflow obligatoire :

```text
discover
→ review domain / rights
→ register / configure
→ sync
```

Une découverte n’est **jamais auto-ingérée**.

## PDF scannés

Le connecteur document distingue les PDF textuels des PDF probablement scannés/text-poor. Pour ces derniers :

```text
extraction_status=NEEDS_OCR
```

Le PDF brut reste conservé et un sidecar `*.needs_ocr.json` est créé. `automatic_ocr=false`. `NEEDS_OCR` est une **ADVISORY** : il reste visible dans les preuves, mais ne pénalise pas le score de qualité comme une panne de collecte.

## Audits

```bash
ivoiredata audit
ivoiredata coverage-audit
ivoiredata quality-audit
ivoiredata discoveries
ivoiredata ci-gold
```

`quality-audit` vérifie notamment : manifest, schema v3, métadonnées CIV, droits, `EMPTY/ERROR`, colonnes documentaires, fichiers zéro octet et documents `NEEDS_OCR`.

## Qualification de stabilité

La séquence correcte est : full sync propre → audits propres → qualification.

```bash
ivoiredata qualification start
ivoiredata qualification status
```

Au démarrage, IvoireData enregistre un **baseline** des sources AUTO déjà `SUCCESS`, non `EMPTY` et `FRESH` grâce au full sync de préflight. Ce baseline ne compte ni comme cycle scheduler ni comme tentative automatique ; il sert uniquement à prouver que les sources à longue fréquence (par exemple 720 h) étaient saines au début de la fenêtre.

Ensuite seuls les cycles automatiques comptent vers la stabilité. Les sync manuels ne peuvent pas fabriquer les 14 jours/cycles.

Conditions minimales :

- >=14 jours calendaires réels ;
- >=14 cycles scheduler ;
- au moins une vraie synchronisation automatique réussie pendant la fenêtre ;
- toutes les sources AUTO couvertes par le baseline propre ou réellement tentées automatiquement ;
- zéro cycle automatique avec erreur ;
- zéro sync automatique en erreur.

## Score

| Composante | Poids |
|---|---:|
| Couverture | 25 % |
| Qualité / provenance | 20 % |
| Classification | 15 % |
| Fraîcheur | 15 % |
| Stabilité | 10 % |
| Droits | 10 % |
| Handoff | 5 % |

Le score est un outil interne. Les gates obligatoires ne peuvent pas être contournés par un bon score. La stabilité ne représente que 10 % : attendre 14 jours ne répare pas à lui seul des manifests legacy ou un schéma documentaire incomplet.

## Gates obligatoires

```text
score_at_least_95
no_p0_coverage_blocker
no_critical_quality_issue
no_active_empty
no_active_sync_error
rights_complete
document_metadata_complete
manifest_v3_complete
qualification_14_days
automatic_sources_exercised
catalog_present
all_manifests_present
```

## Rapport de preuve

```bash
ivoiredata ci-gold --write
```

Produit `data_lake/reports/ci-gold/` avec audit, couverture, qualité, qualification, sources et rapport final.

## Migration vers v0.8.1

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

Ne démarrer/réinitialiser la qualification qu’après correction des problèmes de full sync et des migrations de métadonnées.

## Definition of Done

La Côte d’Ivoire peut être appelée **CI Gold final** seulement lorsque :

1. le code/CI est vert ;
2. le data lake local a été entièrement migré v0.8.1 ;
3. aucun P0 actif n’est `PARTIAL/EMPTY/ERROR/MISSING/UNRESOLVED` ;
4. les droits et manifests v3 sont complets ;
5. les métadonnées documentaires sont complètes ;
6. la qualification réelle est validée ;
7. `ivoiredata ci-gold` retourne `approved=true` ;
8. `ivoiredata ci-gold --write` produit les preuves ;
9. le snapshot/handoff downstream est reproductible.

Avant cela, le statut correct est **CI Gold Candidate**.
