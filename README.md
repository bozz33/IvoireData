# IvoireData 🇨🇮

**v0.4.2 — moteur local de données + usine IvoireCorpus, construit sur dlt OSS**

IvoireData collecte, met à jour, normalise, contrôle et prépare les données utiles à une IA ivoirienne. Le code est dans GitHub ; **toutes les données réelles restent sur le PC**.

Aucun S3, R2 ou MinIO n'est utilisé. Une base PostgreSQL n'est pas requise pour la V1 : les gros volumes restent en fichiers locaux, tandis que dlt/DuckDB fournissent la couche de requête.

## Architecture locale

```text
APIs / sites / PDF / CSV / XLSX / Parquet
                    │
               Connecteurs
                    │
                   dlt
                    │
       ┌────────────┴────────────┐
       │                         │
 data_lake/ local          .ivoiredata/
 données normalisées       état/checkpoints
       │
       ├──────── Query SQL / Search
       │
       └──────── Corpus Builder
                       │
                  corpora/
                       │
                   tokenizer/
```

## Fonctionnalités

- dlt comme moteur Extract/Normalize/Load ;
- `data.gouv.ci` Data Fair : catalogue et jeux de données dynamiques ;
- World Bank WDI Côte d'Ivoire via API v2 ;
- geoBoundaries CIV : métadonnées + GeoJSON ;
- crawler borné des sites publics, limité au même domaine et respectant `robots.txt` ;
- pages/PDF publics avec SHA-256 et chunks ;
- CSV/JSON/JSONL/XLS/XLSX/Parquet ;
- registre multisectoriel ;
- détection automatique du connecteur ;
- règles `refresh_hours` et scheduler **local** ;
- détection de changements et checkpoints ;
- requêtes SQL locales ;
- ranking et cross-check des sources ;
- recherche documentaire ;
- nettoyage, qualité et déduplication ;
- IvoireCorpus versionné et immuable ;
- tokenizer BPE optionnel ;
- FastAPI + CLI ;
- Docker local sans service de stockage externe ;
- CI GitHub uniquement pour valider le code.

## Installation

```bash
python -m pip install -e '.[dev]'
ivoiredata sources --public
ivoiredata coverage
ivoiredata sync civ_datagouv_catalog
ivoiredata status --public
```

## Mise à jour continue

Une vérification immédiate :

```bash
ivoiredata scheduler --once
```

Un scheduler local permanent :

```bash
ivoiredata scheduler --interval 3600
```

Le scheduler vérifie les sources toutes les heures mais respecte la fréquence propre à chaque source dans `configs/runtime_sources.json`. Une source inchangée n'est pas retraitée inutilement.

Les sources institutionnelles publiques majeures sont maintenant configurées en synchronisation automatique. Les sources à accès MIXED/contrôlé restent manuelles.

## API locale

```bash
uvicorn ivoiredata.api:app --host 127.0.0.1 --port 8000
```

Endpoints principaux :

- `GET /health`
- `GET /sources`
- `GET /status`
- `GET /coverage`
- `POST /sync/{source_id}`
- `GET /search/documents`
- `POST /query/sql`

## Corpus

```bash
ivoiredata corpus-build civ-0.1 TABLE1 TABLE2 --output corpora
```

Les corpus sont figés. Une mise à jour des sources produit ensuite une nouvelle version de corpus.

Tokenizer optionnel :

```bash
python -m pip install -e '.[training]'
ivoiredata tokenizer-train corpora/civ-0.1 --vocab-size 32000
```

Voir `docs/ENGINE.md`, `docs/DEPLOYMENT.md`, `docs/STORAGE.md` et `docs/CORPUS.md`.
