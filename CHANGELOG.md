# Changelog

## v0.8.1 — CI Gold completeness

### Added

- Second CI Gold registry overlay with 19 additional official/institutional sources: gender/family, youth, industry/commerce, CEPICI investment, digital ministry, interior/decentralization, ONEF, civil service, HABG, defense, National Assembly, Senate, Constitutional Council, Court of Accounts, CESEC, Presidency, diplomacy, solidarity/poverty and MIRAH.
- Coverage matrix v2 with more than 50 national knowledge families, including gender, youth, poverty, migration, decentralization, industry, investment, digital, public cybersecurity, innovation, civil protection, defense, diplomacy and anti-corruption.
- `ivoiredata discoveries` and `GET /discoveries` to compare the synchronized Data.gouv.ci catalog with explicit registry mappings. Discoveries are review-only and never auto-ingested.
- `NEEDS_OCR` detection for scanned/text-poor PDFs, with local `*.needs_ocr.json` sidecars and no automatic OCR.
- Quality audit counters for legacy manifests, zero-byte files and OCR-needed documents.
- CI Gold gate `manifest_v3_complete`.
- Query/document-search layers now honor the same effective registry, CI Gold overlays and persistent runtime overrides as the main engine.
- Validation across both registry files and the complete CI Gold coverage matrix.

### Changed

- Version 0.8.0 → 0.8.1.
- Search returns richer document metadata including domain, title, language and document type.
- Public document tables include `content_type` and `extraction_status`.
- Taxonomy expanded for national social, institutional, economic and digital knowledge.

### Operational migration

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

Only after the local full sync is clean should the qualification window be started/reset.

## v0.8.0 — CI Gold foundation

- National CIV metadata contract and manifest/catalog schema v3.
- Deterministic classification for documents, Data.gouv.ci datasets and WDI indicators.
- Initial CI Gold source overlay and coverage matrix.
- Coverage, quality, qualification and CI Gold audits.
- 14-day real automatic stability qualification and mandatory gates.
- CI Gold report bundle under `data_lake/reports/ci-gold/`.

## v0.7.2

- Persistent AUTO / MANUAL / DISABLED controls.
- Global automatic update ON/OFF respected by scheduler.
- Persistent runtime overrides shared between Docker services.
- Dynamic scheduler interval.

## v0.7.1

- Connector-aware delivery classification.
- Enabled/disabled source control.
- Unresolved Data.gouv sources disabled.

## v0.7.0

- Manifest v2 and audit.
- ILOSTAT CSV correction preserving `obs_status`.
- Specialized FAOSTAT and UIS connectors.
- World Bank Projects connector.
