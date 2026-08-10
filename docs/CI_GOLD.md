# IvoireData — spécification CI Gold

## 1. Périmètre

CI Gold concerne **uniquement la Côte d’Ivoire**. Aucune extension pays/région n’est autorisée tant que les gates CI Gold ne sont pas satisfaits.

CI Gold ne signifie pas « toutes les informations existant dans le pays ». Il signifie que chaque grande famille nationale prioritaire a été **identifiée, qualifiée et mesurée** avec un statut explicite :

- `COVERED` ;
- `PARTIAL` ;
- `CONTROLLED` ;
- `UNAVAILABLE` ;
- `UNRESOLVED` ;
- `MISSING`.

La matrice machine-readable est `configs/ci_coverage.json`.

## 2. Contrat de métadonnées

À partir de v0.8.0, le manifest est en schema v3. Chaque source possède une section `metadata` avec au minimum :

```text
country_code
country_name
source_id
provider
source_domain
primary_domain
secondary_domains_json
language
geographic_scope
document_type
rights_tier
access_tier
classification_status
classification_confidence
```

Pour IvoireData CI :

```text
country_code = CIV
country_name = Côte d'Ivoire
```

Les documents `public_web` portent ces champs directement dans leurs lignes Parquet. Les sources multidomaines Data.gouv.ci et WDI reçoivent une classification déterministe au niveau dataset/indicateur.

## 3. Classification

Ordre de décision :

1. domaine explicite de la source ;
2. configuration source ;
3. métadonnées upstream ;
4. titre/description/URL ;
5. règles lexicales déterministes ;
6. `multidomain`/`PARTIAL` en cas d’incertitude.

Aucun LLM n’est nécessaire au chemin normal. Il vaut mieux conserver `UNKNOWN`/`PARTIAL` que fabriquer une classification.

## 4. Types documentaires

Types initiaux :

```text
LAW
DECREE
ORDINANCE
REGULATION
REPORT
STATISTICAL_REPORT
DATASET
BUDGET
STRATEGY
PLAN
GUIDE
PROCEDURE
FORM
PRESS_RELEASE
DIRECTORY
MAP
RESEARCH
OTHER
```

## 5. Sources institutionnelles CI Gold

La v0.8.0 ajoute notamment :

- `civ_sgg_official_texts` — SGG, textes officiels / Journal officiel ;
- `civ_dgbf_budget` — DGBF, budget et lois de finances ;
- `civ_mesrs` — enseignement supérieur / recherche ;
- `civ_cei` — élections et résultats ;
- `civ_ageroute` — routes ;
- `civ_anare` — électricité ;
- `civ_culture` — culture/patrimoine ;
- `civ_tourism` — tourisme ;
- `civ_communication` — communication/médias ;
- `civ_sports` — sport ;
- `civ_gouv_portal` — portail du Gouvernement.

Ces sources sont définies dans le registre et configurées dans `configs/ci_gold_sources.json`.

## 6. Audit de couverture

```bash
ivoiredata coverage-audit
```

L’audit compare :

- domaines attendus ;
- sources attendues ;
- présence au registre ;
- activation ;
- politique de droits ;
- livraison réellement non vide.

Un domaine P0 `MISSING` ou `UNRESOLVED` bloque CI Gold.

## 7. Audit qualité

```bash
ivoiredata quality-audit
```

Contrôles principaux :

- manifest présent ;
- métadonnées nationales présentes ;
- droits présents ;
- aucune livraison `EMPTY` critique ;
- aucun `SYNC_ERROR` critique ;
- colonnes documentaires CI Gold présentes sur les sources Web après migration.

Les anciennes tables v0.7.x restent lisibles, mais l’audit les signale jusqu’à resynchronisation v0.8.0.

## 8. Qualification de stabilité

La qualification est persistée dans :

```text
.ivoiredata/state/ci_gold_qualification.json
```

Démarrage :

```bash
ivoiredata qualification start
```

État :

```bash
ivoiredata qualification status
```

Seuls les cycles automatiques du scheduler sont enregistrés. Les sync manuels ne peuvent pas artificiellement valider la stabilité.

Gate de stabilité :

- au moins 14 jours calendaires réels ;
- au moins 14 cycles scheduler ;
- au moins une synchronisation automatique réelle réussie ;
- **chaque source publique active en mode AUTOMATIC doit avoir été réellement exercée au moins une fois pendant la fenêtre** ;
- zéro cycle automatique avec erreur ;
- zéro erreur de synchronisation enregistrée pendant la fenêtre.

Les réveils du scheduler sans source due sont enregistrés comme cycles, mais ne suffisent jamais à eux seuls à qualifier le système.

## 9. Score CI Gold

`ivoiredata ci-gold` calcule un score interne :

| Composante | Poids |
|---|---:|
| Couverture | 25 % |
| Qualité / provenance | 20 % |
| Classification | 15 % |
| Fraîcheur | 15 % |
| Stabilité | 10 % |
| Droits | 10 % |
| Handoff | 5 % |

Le score n’est pas une vérité statistique ; il sert à piloter la qualification.

## 10. Gates obligatoires

CI Gold est `approved=true` uniquement si **tous** les gates sont vrais :

```text
score_at_least_95
no_p0_coverage_blocker
no_critical_quality_issue
no_active_empty
no_active_sync_error
rights_complete
document_metadata_complete
qualification_14_days
automatic_sources_exercised
catalog_present
all_manifests_present
```

Un bon score ne permet jamais de contourner un gate obligatoire.

## 11. Rapport de preuve

```bash
ivoiredata ci-gold --write
```

Produit :

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

Ces fichiers décrivent l’état du data lake ; ils ne remplacent pas les données elles-mêmes.

## 12. Procédure de migration v0.7.2 → v0.8.0

```bash
git pull
docker compose build
docker compose --profile run up -d
```

Puis régénérer les données/manifests :

```bash
docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync --all-public --force"
```

Contrôles :

```bash
docker compose exec api ivoiredata audit
docker compose exec api ivoiredata coverage-audit
docker compose exec api ivoiredata quality-audit
```

Une fois les anomalies initiales corrigées :

```bash
docker compose exec api ivoiredata qualification start
```

Le scheduler doit rester actif pendant toute la fenêtre de qualification.

## 13. Droits

Les tiers restent :

- A : réutilisable/redistribuable ;
- B : conditions spécifiques à la source ;
- C : contenu public collectable localement avec contraintes de réutilisation ;
- D : restreint/autorisation requise.

Aucun accès contrôlé ne doit être contourné. Une catégorie peut être `CONTROLLED` et néanmoins être considérée comme correctement **évaluée** pour la matrice, sans que les payloads interdits soient ingérés.

## 14. Langues / nouchi / œuvres culturelles

La couverture linguistique et culturelle doit rester juridiquement propre. CI Gold n’autorise pas :

- aspiration massive de livres protégés ;
- paroles de chansons ;
- corpus privés ;
- données nominatives ;
- contournement de restrictions.

Ces familles peuvent rester `CONTROLLED` jusqu’à obtention de corpus ouverts, licenciés ou créés pour le projet.

## 15. Définition de Done

La phase CI est gelable lorsque :

1. `ivoiredata ci-gold` renvoie `approved=true` ;
2. `ivoiredata ci-gold --write` produit les preuves ;
3. la CI GitHub est verte ;
4. le full sync local ne contient aucun `EMPTY`/`ERROR` actif critique ;
5. la fenêtre de 14 jours est validée et toutes les sources automatiques ont été exercées ;
6. le snapshot/handoff downstream est reproductible.

Tant que l’un de ces points est faux, la Côte d’Ivoire reste en **CI Gold Candidate** et non CI Gold final.
