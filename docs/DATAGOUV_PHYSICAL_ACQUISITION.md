# Data.gouv CI physical acquisition — v0.8.4-B

This phase turns the Data.gouv CI connector into a physical-delivery source rather than relying on historical dlt state as proof that raw upstream bytes still exist.

## Invariants

- The official catalogue is discovered from `https://data.gouv.ci/data-fair/api/v1/datasets`.
- A dlt dataset signature is **not** sufficient to mark a dataset `FETCHED`.
- `UNCHANGED` is only allowed when the matching raw local snapshot exists.
- A historical dataset without a local raw snapshot is downloaded once to establish physical truth.
- `/full` bodies are streamed directly to a `.part` file, hashed while downloading, then atomically promoted to a digest-addressed snapshot.
- If `/full` is unavailable or invalid, `/lines` is followed through the official `next` cursor and rows are persisted incrementally as NDJSON.
- `/lines` never accumulates the complete dataset in RAM.
- The Artifact Ledger remains the source of truth for physical presence and later SHA-256 verification.

## First v0.8.4-B run

The first full catalogue sync may download datasets that were already materialized by older versions because those versions did not retain an auditable raw artifact. This is intentional and happens once.

```bash
ivoiredata data-gouv audit
ivoiredata sync civ_datagouv_catalog --force
ivoiredata artifacts audit --source-id civ_datagouv_catalog
ivoiredata artifacts verify --source-id civ_datagouv_catalog
ivoiredata data-gouv audit
```

Expected progression:

```text
before sync:
  official_visible = X
  physical         = 0 or partial
  missing_ids      = historical gap

after sync:
  official_visible = X
  physical         = X - real failures
  missing_ids      = []
  failed_ids       = only real upstream failures

after verify:
  verified         = physical
```

`complete_physical=true` means every currently visible official dataset has a physical local snapshot and there are no `FAILED`, `LOCAL_MISSING` or `CORRUPTED` datasets.

`complete_verified=true` is stronger: all current official datasets have been SHA-256 verified by the Artifact Ledger.

## Incremental second run

After the first physical backfill, a second run with unchanged upstream signatures must avoid body downloads. The sync statistics in `datagouv_sync_stats.json` report:

- `unchanged`
- `backfill_missing_raw`
- `physically_backfilled`
- `downloaded`
- `via_full_stream`
- `via_lines_stream`
- `replayed_from_local_cache`
- `failed`

For a stable catalogue after the initial backfill, `backfill_missing_raw` and `downloaded` should fall to zero.

## Public-web repair hardening

The same release also fixes the two SGG anomalies discovered by the Artifact Ledger:

- trailing path whitespace such as `%20` is removed before URL identity and HTTP access;
- upload-container URLs such as `/uploads/publications/` are not tracked as documents;
- legacy malformed/container artifact IDs are tombstoned as `REMOVED_UPSTREAM`, so they no longer stay forever in the repair queue.

After deployment, the safe sequence is:

```bash
ivoiredata sync civ_sgg_official_texts --force
ivoiredata artifacts audit --source-id civ_sgg_official_texts
ivoiredata artifacts repair --source-id civ_sgg_official_texts
```

Only after the dry-run contains real document gaps and no malformed-directory artifacts should `repair --execute` be used.
