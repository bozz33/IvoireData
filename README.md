# IvoireData 🇨🇮

**v0.3.0 — corpus multisectoriel matérialisable pour une IA adaptée à la Côte d’Ivoire**

La phase active couvre les données ivoiriennes **hors langues** : administration, fiscalité, droit, économie, agriculture, santé, éducation, télécoms, mines/pétrole/énergie, environnement, transport, foncier/logement, eau, météo, géographie et développement.

## État réel

- Le portail officiel `data.gouv.ci` expose **202 jeux de données observés au 2026-08-09**.
- Jusqu'à v0.2, IvoireData conservait surtout les **liens/métadonnées**, les règles d'ingestion et un petit corpus de faits : les 202 tables n'étaient pas commitées dans Git.
- v0.3 ajoute la **matérialisation réelle** du catalogue Data Fair : export CSV brut, métadonnées, JSONL enrichi de provenance, Parquet et manifests.
- Les autres sources publiques (DGI, Justice, CNPS, ARTCI, ministères, etc.) restent intégrables dans `CIV-Public-RAG` sans contourner d'authentification ni d'accès restreint.
- Langues et speech restent volontairement différés.

## Matérialiser data.gouv.ci

```bash
python -m pip install -e '.[materialize,dev]'
python scripts/materialize_data_gouv_ci.py --output data_lake
```

Test rapide sur 5 datasets :

```bash
python scripts/materialize_data_gouv_ci.py --output data_lake --limit 5
```

Un dataset précis :

```bash
python scripts/materialize_data_gouv_ci.py \
  --output data_lake \
  --dataset recensement-de-la-population-ivoirienne
```

Le résultat est produit dans :

```text
data_lake/
├── catalog/data_gouv_ci.json
├── metadata/data_gouv_ci/*.json
├── raw/data_gouv_ci/*/full.csv
├── processed/data_gouv_ci/jsonl/*.jsonl
├── processed/data_gouv_ci/parquet/*.parquet
└── manifests/
```

Voir `docs/DATAGOUV_ACCESS.md` pour les endpoints et le détail du pipeline.

## GitHub Actions

Le workflow **Materialize data.gouv.ci** est lançable manuellement depuis l'onglet **Actions**. Il télécharge les jeux accessibles, les normalise et publie `data_lake/` comme artifact GitHub temporaire. Cela donne un accès direct au corpus sans stocker des centaines de fichiers volumineux dans l'historique Git.

## Autres pipelines

Validation :

```bash
python scripts/validate_registry.py
python scripts/validate_seed_facts.py
pytest -q
```

Ingestion d'une page ou d'un PDF public :

```bash
python scripts/ingest_public_web.py --source-id civ_dgi https://www.dgi.gouv.ci/
```

Découverte/crawl public borné :

```bash
python scripts/discover_public_documents.py --source-id civ_dgi --max-pages 50 --include-pages https://www.dgi.gouv.ci/
```

## Collections

- `CIV-Open` : datasets ouverts et redistribuables.
- `CIV-Public-RAG` : documents/pages publics avec provenance, bruts conservés hors Git si nécessaire.
- `CIV-Facts` : faits structurés, datés et sourcés.
- `CIV-Microdata` : microdonnées à accès contrôlé.
- `CIV-Eval` : benchmark futur, isolé de l'entraînement.

## Stockage

Git contient le code, les registres, manifests et petits jeux Gold. Les gros snapshots vivent dans `data_lake/`, GitHub Actions artifacts ou, à terme, S3/MinIO. Chaque export garde une URL source, une date de récupération et un SHA-256 pour être reproductible.

## Licences

Le code original du projet est Apache-2.0. Les sources externes conservent leurs propres droits et obligations. Voir `docs/RIGHTS_AND_ACCESS.md` et `docs/QUALITY_ASSURANCE.md`.
