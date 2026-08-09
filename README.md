# IvoireData 🇨🇮

**Version 0.2.0 — multisector public-data foundation for Côte d’Ivoire (languages deferred)**

IvoireData is a reproducible data foundation for AI/RAG/evaluation systems adapted to Côte d’Ivoire. Version 0.2 activates public-source ingestion across administration, law, taxation, economy, agriculture, health, education, telecom, energy, environment, transport, land/housing, water, geospatial and development data.

## Current state

- Master registry: **98 verified source/collection entries**.
- Active non-language registry: **92 entries**.
- Official Côte d’Ivoire open-data portal: **202 datasets** discoverable; Licence Ouverte permits reuse with attribution.
- Public-but-unclear-rights sources are now ingested **locally** for factual extraction and RAG; raw documents are not automatically mirrored to the public repo.
- Seed corpus: **25 short structured facts** with provenance.
- Language/speech sources are preserved in the master registry but deferred from the active pipeline.

## Collections

- `CIV-Open`: redistributable/open datasets.
- `CIV-Public-RAG`: public official sources ingested locally with provenance.
- `CIV-Facts`: normalized short factual records derived from public authoritative sources.
- `CIV-Microdata`: gated/research datasets kept separate.
- `CIV-Eval`: held-out evaluation datasets (future; never train on them).

## Start

```bash
python scripts/validate_registry.py
python scripts/validate_seed_facts.py
python scripts/build_public_queue.py
python scripts/build_summary.py
```

To ingest one public page locally:

```bash
pip install pypdf
python scripts/ingest_public_web.py \
  --source-id civ_dgi_2026_documentation \
  https://dgi.gouv.ci/
```

Raw downloads and processed local corpora are ignored by Git. See `registry/ingestion_policy.csv` and `docs/RIGHTS_AND_ACCESS.md`.

## Repository map

```text
registry/               master sources + active non-language sources + ingestion policy
configs/                ingestion and collection policy
scripts/                discovery, ingestion, validation and provenance tools
data/seed/              small publishable factual seed records with provenance
data/raw/               local cache (gitignored)
data/processed/         local normalized/RAG material (gitignored)
docs/                   architecture, coverage, rights, privacy, roadmap
schemas/                record schemas
tests/                  validation tests
```

## Licensing

Repository code: Apache-2.0. Repository-authored metadata/docs: CC BY 4.0. External sources retain their own licences/terms. IvoireData never claims that public visibility alone transfers copyright.
