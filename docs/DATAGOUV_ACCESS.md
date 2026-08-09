# Accès et matérialisation de data.gouv.ci

## Important : catalogue ≠ données matérialisées

Le portail `https://data.gouv.ci/datasets` expose actuellement 202 jeux de données. Jusqu'à IvoireData v0.2, le dépôt conservait surtout les liens, métadonnées, politiques d'ingestion et quelques faits de validation. Les 202 tables n'étaient donc **pas** incluses dans Git.

À partir de v0.3, `scripts/materialize_data_gouv_ci.py` exploite directement l'API Data Fair publique du portail et produit un snapshot local réellement exploitable.

## API

Base Data Fair utilisée :

```text
https://data.gouv.ci/data-fair/api/v1
```

Patrons d'accès :

```text
GET /datasets
GET /datasets/{dataset_id}
GET /datasets/{dataset_id}/full
GET /datasets/{dataset_id}/lines
```

`/full` est utilisé comme export CSV complet/enrichi. Le script conserve l'URL exacte, le SHA-256 et la date de récupération.

## Matérialiser tout le portail

```bash
python -m pip install -e '.[materialize]'
python scripts/materialize_data_gouv_ci.py --output data_lake
```

Pour tester seulement 5 datasets :

```bash
python scripts/materialize_data_gouv_ci.py --output data_lake --limit 5
```

Pour un dataset précis :

```bash
python scripts/materialize_data_gouv_ci.py \
  --output data_lake \
  --dataset recensement-de-la-population-ivoirienne
```

## Résultat

```text
data_lake/
├── catalog/data_gouv_ci.json
├── metadata/data_gouv_ci/<dataset>.json
├── raw/data_gouv_ci/<dataset>/full.csv
├── processed/data_gouv_ci/jsonl/<dataset>.jsonl
├── processed/data_gouv_ci/parquet/<dataset>.parquet
└── manifests/
    ├── data_gouv_ci.jsonl
    ├── data_gouv_ci_errors.jsonl
    └── data_gouv_ci_summary.json
```

Les lignes JSONL/Parquet reçoivent des champs de provenance `__ivoiredata_*` : identifiant du dataset, titre, producteur, licence, URL source, date de récupération et index de ligne.

## GitHub Actions

Le workflow **Materialize data.gouv.ci** peut être lancé manuellement dans l'onglet Actions. Il exécute le même pipeline sur un runner GitHub et publie le snapshot comme artifact téléchargeable. Cela permet à un membre de l'équipe de récupérer le corpus sans configurer le pipeline localement.

## Pourquoi les données massives ne sont pas commitées dans Git

Git versionne le code, les schémas, manifests et petits jeux Gold. Les exports massifs sont générés par le pipeline puis stockés localement, dans un artifact GitHub temporaire ou, à terme, dans S3/MinIO. Cela évite un dépôt Git gigantesque tout en gardant chaque snapshot reproductible et vérifiable.

## Limites

- certains jeux Data Fair peuvent être des vues/jeux virtuels ou avoir un export indisponible ; ils sont enregistrés dans `data_gouv_ci_errors.jsonl` au lieu d'arrêter toute la collecte ;
- les données issues d'une base tierce (ex. OpenStreetMap) gardent la provenance amont et doivent respecter les obligations de cette base ;
- une source publique n'est pas supposée exacte : les conflits de valeurs restent suivis dans `data/quality/conflicts.jsonl`.
