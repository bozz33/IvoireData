# Changelog

## v0.8.0 — CI Gold foundation

### Added

- Côte d’Ivoire national metadata contract (`country_code=CIV`, domains, language, document type, geographic scope, rights, classification status/confidence).
- Manifest and catalog schema v3.
- Deterministic document/domain classifier for public documents and multidomain sources.
- Dataset/indicator classification for Data.gouv.ci and World Bank WDI.
- CI Gold coverage matrix (`configs/ci_coverage.json`).
- CI Gold source overlay (`configs/ci_gold_sources.json`).
- New official CI sources: SGG, DGBF, MESRS, CEI, AGEROUTE, ANARE-CI, Culture, Tourism, Communication, Sports and Government portal.
- `coverage-audit`, `quality-audit`, `ci-gold`, `qualification` CLI commands.
- Matching CI Gold API endpoints.
- CI Gold score and mandatory gates.
- 14-day automatic stability qualification ledger.
- Requirement that every active automatic source is actually exercised during qualification.
- CI Gold report bundle under `data_lake/reports/ci-gold/`.
- Documentation: CI Gold specification, coverage matrix, updated architecture, audit, usage, deployment and downstream handoff.

### Changed

- Version 0.7.2 → 0.8.0.
- Audit summary now separates structured rows, document rows and total Parquet rows.
- Runtime configuration now merges base config → packaged CI Gold overlay → persistent local overrides.
- Scheduler records only automatic cycles for qualification; manual syncs cannot satisfy stability gates.
- Docker image/version updated to 0.8.0.

### Migration

After upgrade from v0.7.2, rebuild and run a forced public sync to regenerate manifests v3 and enrich document Parquet metadata:

```bash
docker compose build
docker compose --profile run up -d
docker compose --profile sync run --rm sync-once \
  sh -c "ivoiredata sync --all-public --force"
```

Then run:

```bash
docker compose exec api ivoiredata audit
docker compose exec api ivoiredata coverage-audit
docker compose exec api ivoiredata quality-audit
```

Only after the local data lake is clean should the 14-day qualification be started:

```bash
docker compose exec api ivoiredata qualification start
```

A software release tagged v0.8.0 is a **CI Gold foundation/candidate**. A data lake may be called **CI Gold final** only when `ivoiredata ci-gold` returns `approved=true` after the real qualification window.

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
