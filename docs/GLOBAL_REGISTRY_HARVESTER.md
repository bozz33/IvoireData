# Global Registry Harvester

IvoireData v0.8.4 introduces a two-stage global technology discovery pipeline.

## Why SQLite for discovery

`technology_catalog.json` contains qualified technology records. It is intentionally not used as the world-scale package-name queue because registry indexes can contain hundreds of thousands or millions of names.

The candidate queue is stored in:

```text
.ivoiredata/state/technology_harvest.sqlite3
```

It uses WAL mode and a unique `(registry, name)` key.

## Safety boundary

Harvesting never enables corpus ingestion.

```text
official bulk/change feed
        -> SQLite candidate queue
        -> bounded qualification
        -> technology_catalog.json
        -> documentation resolver (later stage)
```

A harvested candidate remains `enabled_for_corpus=false` after qualification unless a later explicit promotion policy enables it.

## Commands

Audit the queue:

```bash
ivoiredata-tech harvest-audit
```

Harvest the most popular Composer packages without enumerating the entire registry:

```bash
ivoiredata-tech harvest packagist --limit 500
```

Explicit full Packagist enumeration:

```bash
ivoiredata-tech harvest packagist --full --limit 10000
```

Track Packagist changes using its official cursor feed:

```bash
ivoiredata-tech harvest packagist-changes --limit 1000
```

Harvest RubyGems recent additions/updates:

```bash
ivoiredata-tech harvest rubygems
```

Harvest the paginated pub.dev mirror index:

```bash
ivoiredata-tech harvest pub --limit 1000
```

When the pub.dev cursor reaches the last page it is stored as `__COMPLETE__`; later runs do not restart at page 1. Use `--full` to explicitly restart a full scan.

PyPI exposes all projects from the official JSON Simple API as one large index. IvoireData therefore requires an explicit full flag:

```bash
ivoiredata-tech harvest pypi --full --limit 10000
```

The response ETag and PyPI serial are persisted so an unchanged subsequent index can return HTTP 304 without reparsing the body.

Qualify high-priority pending candidates:

```bash
ivoiredata-tech qualify --limit 50
```

Qualification calls the native registry first and then available cross-check sources. One failed candidate does not abort the batch.

## Incremental semantics

Ordinary rediscovery of an already qualified candidate does not put it back into the queue.

A true upstream update event can set `requeue=true`, which returns the candidate to `PENDING`. Packagist's official metadata changes feed and RubyGems update activity use this behavior.

## NuGet repository semantics

NuGet `projectUrl` is a project/home page. It is not repository metadata. IvoireData only accepts an actual repository field as NuGet repository evidence. This prevents documentation URLs such as Microsoft Learn from being misclassified as source repositories.

## Current official harvesting adapters

- Packagist popular index
- Packagist full package-name list
- Packagist metadata changes feed
- RubyGems recent/new activity
- pub.dev paginated package-name mirror API
- PyPI JSON Simple API (explicit full scan)

Other ecosystems remain discoverable through the existing global discovery layer and will receive native bulk adapters when their registries expose stable supported APIs.
