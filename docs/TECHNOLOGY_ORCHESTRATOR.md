# Industrial technology orchestrator

`ivoiredata-tech-orchestrator` advances the already harvested global package universe through the bounded documentation pipeline:

```text
harvested SQLite candidates
  -> native qualification
  -> independent authority verification
  -> documentation target resolution
  -> active official-docs discovery
  -> incremental official-docs fetch
```

It does **not** run registry bootstraps or full harvests. Existing npm, Maven, crates.io, NuGet and Go bulk snapshots/cursors remain untouched.

## Safety properties

- one SQLite lease prevents two orchestrators from draining the same queue concurrently;
- every stage has a bounded total budget and a smaller per-registry quantum;
- every stage keeps its own persistent registry rotation, so a very large ecosystem cannot starve smaller ecosystems;
- empty/backoff registries are skipped for the rest of the current stage without blocking the next registry;
- underlying qualification/authority/discovery/fetch retry state remains authoritative;
- documentation fetch always uses `force=False`;
- target migrations continue to use the two-phase `_superseded` invariant;
- a GitHub blob rate-limit response opens a shared persisted fetch cooldown and stops the rest of the fetch stage instead of producing a cross-registry request storm;
- unchanged packages/documents remain zero-redownload through the existing native version, Git commit/blob SHA, HTTP validator and content-hash layers.

## Commands

Audit without network access:

```bash
ivoiredata-tech-orchestrator audit --top 20
```

One bounded cycle:

```bash
ivoiredata-tech-orchestrator run
```

Small production canary:

```bash
ivoiredata-tech-orchestrator run \
  --qualification-budget 10 \
  --authority-budget 5 \
  --target-budget 20 \
  --discovery-budget 3 \
  --fetch-budget 1
```

Restrict a diagnostic cycle to selected ecosystems:

```bash
ivoiredata-tech-orchestrator run --registries npm,maven,crates
```

Continuous bounded worker (minimum interval 300 seconds):

```bash
ivoiredata-tech-orchestrator loop --interval 900
```

Docker keeps the worker opt-in. The normal `run` profile does not start it:

```bash
docker compose --profile technology up -d technology-scheduler
```

Stop it independently:

```bash
docker compose stop technology-scheduler
```

## Default per-cycle budgets

| Stage | Budget | Per-registry quantum |
|---|---:|---:|
| qualification | 90 | 10 |
| authority | 36 | 4 |
| target resolution | 180 | 20 |
| active docs discovery | 18 | 2 |
| docs fetch | 9 | 1 |

All values can be overridden with `IVOIREDATA_TECH_*_BUDGET` and `IVOIREDATA_TECH_*_QUANTUM`. `IVOIREDATA_TECH_ORCHESTRATOR_INTERVAL` controls the loop cadence and defaults to 900 seconds.

## GitHub credentials

For GitHub-hosted official documentation, configure a read-only GitHub token in the host `.env`:

```dotenv
GITHUB_TOKEN=...
```

The token is passed into the containers but is never written to the technology SQLite database. With a token, immutable file bodies use GitHub's authenticated Git Blob REST endpoint; without a token the engine keeps the public raw transport with rate-limit circuit breaking.

## Recommended activation sequence

1. keep `technology-scheduler` stopped;
2. deploy/rebuild the new image;
3. run `ivoiredata-tech-orchestrator audit`;
4. run one small canary with `--fetch-budget 1`;
5. verify the five stage summaries, rotations, HTTP budget and documentation fetch audit;
6. only then enable the `technology` Compose profile.
