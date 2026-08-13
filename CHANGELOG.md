# Changelog

## v0.8.3 — final upstream hardening

### Partial structured retries

- Structured sources that finish a dlt load but still expose `failed`, `backlog_count`, `deferred_budget` or `skipped_oversize` are retried on a short cadence instead of waiting the normal 24/168/720-hour freshness window.
- The default partial retry cadence is 6 hours and can be overridden per source with `options.partial_retry_hours`.
- Partial retries always call the connector with `force=false`: official upstream signatures, HTTP validators and local snapshots remain authoritative, so already acquired unchanged payloads are not intentionally retransferred.
- A scheduler result with an unresolved structured backlog is marked `partial`; the committed data remains usable, but the cycle cannot falsely count as a perfect CI Gold automatic success.

### Release consistency

- API, Docker image, compose deployment, package metadata and upstream User-Agent are aligned on v0.8.3.
- Added regression tests for short-cadence retry, retry throttling, partial qualification status and completed-success behavior.

### Operational rule

After upgrading from v0.8.1/v0.8.2, migrate the large structured sources one by one, inspect their `*_sync_stats.json`, and immediately run them a second time with `--force`. The second run is the proof that unchanged content is not retransferred. Restart the 14-day CI Gold qualification only when structured `failed=0` and backlog is zero.

## v0.8.2 — official incremental upstreams

### Correctness

- Data.gouv.ci is now synchronized through the actual Data Fair contract: anonymous public catalogue with `page>=1`, `/full` bulk transfer when available, then official `/lines` fallback following the returned `next` cursor until absent.
- Data Fair catalogue pagination no longer assumes that the server honors the requested page size; the advertised total count is followed until reached and repeated/stalled pages fail loudly instead of silently truncating coverage.
- Data.gouv failures are no longer silently collapsed into “dataset ignored”; sync statistics record catalogue size, downloaded/unchanged datasets, `/full` vs `/lines`, removals and exact failures.
- Datasets removed from the anonymous public Data.gouv catalogue are marked `REMOVED_UPSTREAM`; their old Parquet tables are archived under `raw/legacy/removed_upstream/` rather than deleted. Pre-v0.8.2 orphan tables are also archived non-destructively after a successful migration run.
- ILOSTAT no longer treats `/data/indicator?ref_area=CIV` as country-wide data. It uses the official `REF_AREA` table of contents as the country-level change gate, then loads the official indicator TOC and requests every new/updated indicator with both `id=<indicator>` and `ref_area=CIV` in CSV format only when Côte d'Ivoire has changed.
- The unsafe RDS path remains disabled.
- FAOSTAT no longer freezes collection to five domains. It reads the official `datasets_E.json` bulk catalogue, discovers current domains and excludes discontinued archives by default.

### Incremental downloads

- Added `.ivoiredata/state/upstreams.json` with persistent upstream versions, validators, cache paths and last network outcomes.
- Version priority: official release/version metadata → ETag/Last-Modified/304 → SHA-256 fallback.
- `--force` now means “check now”; identical already-materialized versions are not intentionally downloaded again.
- Crash-safe replay: if a payload was downloaded but dlt did not commit the load, the next run can replay the local snapshot without another body transfer.
- ILOSTAT compares the small official `REF_AREA` entries for `CIV_A`, `CIV_Q`, `CIV_M` (as available). If their `last.update`/metadata signature is unchanged, the expensive indicator TOC/data sweep is skipped entirely.
- FAOSTAT uses official `DateUpdate/FileRows/FileSize/FileLocation` signatures and a bounded per-run transfer budget.
- World Bank WDI uses official source `lastupdated` metadata before expensive indicator/data requests.
- World Bank Projects, UIS, geoBoundaries and direct HTTP files use conditional HTTP where available and hashes otherwise.
- Geofabrik PBF uses the official `.md5` sidecar first and HTTP validators as fallback.
- Public web crawling uses HTTP validators per page and preserves cached child links across HTTP 304 responses.

### Robustness

- Freshness, runtime overrides, qualification state, upstream state, manifests, catalogue and snapshot sidecars use atomic JSON writes.
- Shared mutable state is protected by cross-process locks so API, scheduler and one-shot containers cannot lose each other's updates.
- A per-source lock prevents the same dlt source from running concurrently in multiple containers; different sources remain independent.
- `catalog.json` has a global lock plus atomic replacement, preventing concurrent source completions from producing a partial/corrupt catalogue.
- Malformed JSON state is quarantined as `*.corrupt-<timestamp>` rather than preventing engine startup.
- ILOSTAT requests retry 429/5xx/timeouts with bounded exponential backoff.
- FAOSTAT enforces per-file and per-run transfer budgets; backlog is explicit and resumable.
- Incremental structured connectors protect existing dynamic tables when an unchanged table is omitted from a run.

### Audit / CI Gold

- Added `ivoiredata upstreams [source_id]`.
- Added `GET /upstreams` and `GET /upstreams/{source_id}`.
- Structured source reports are written under each source `raw/` directory.
- CI Gold now blocks on `UPSTREAM_PARTIAL_FAILURE` and `UPSTREAM_BACKLOG` for priority P0 structured sources.
- `ci-gold --write` includes `upstreams.json`.

### Migration

See `docs/UPSTREAM_INCREMENTAL.md`. Upgrade structured sources individually, verify immediate second runs do not transfer unchanged payloads, then restart the 14-day qualification only after structured backlogs/failures are zero.

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
