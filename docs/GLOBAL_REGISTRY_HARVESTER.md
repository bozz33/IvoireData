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

Explicit Packagist enumeration:

```bash
ivoiredata-tech harvest packagist --full --limit 10000
```

Track Packagist changes using its official timestamp cursor feed:

```bash
ivoiredata-tech harvest packagist-changes
```

The first Packagist changes request intentionally has no `since` parameter. Packagist responds with HTTP 400 plus a JSON `timestamp`; IvoireData stores that timestamp as the initial cursor instead of treating the response as a fatal error. An invalid/expired cursor is reinitialized and reported with `resync_required=true`.

A cursor-bearing Packagist change response is always processed in full before its timestamp is advanced. IvoireData never truncates a downloaded change response because doing so could skip updates permanently. `delete` actions are persisted as `DELETED` queue records and `update` actions requeue previously qualified packages.

Harvest RubyGems recent additions/updates:

```bash
ivoiredata-tech harvest rubygems --limit 500
```

### pub.dev: ranked versus complete discovery

Bounded/default pub.dev discovery uses the officially supported ranked package-name completion endpoint:

```bash
ivoiredata-tech harvest pub --limit 500
```

This source is deliberately **not** marked complete because pub.dev documents that the ranked response may omit package names.

A complete mirror enumeration is explicit:

```bash
ivoiredata-tech harvest pub --full
```

Full mode uses `https://pub.dev/api/package-names`. Every package name in a server page that has already been downloaded is inserted into SQLite before the cursor advances. This means a single page containing tens of thousands of names is processed completely even if the CLI `--limit` value is smaller. The full-source cursor is persisted after every page for crash-safe continuation and becomes `__COMPLETE__` only after `nextUrl` is absent.

To intentionally discard the stored completion/cursor state and rescan:

```bash
ivoiredata-tech harvest pub --full --reset
```

PyPI exposes all projects from the official JSON Simple API as one large index. IvoireData therefore requires an explicit full flag:

```bash
ivoiredata-tech harvest pypi --full --limit 10000
```

The response ETag, Last-Modified value and PyPI serial are persisted so an unchanged subsequent index can return HTTP 304 without reparsing the body. Use `--reset` only when a deliberate cursor/cache reset is required.

Qualify high-priority pending candidates:

```bash
ivoiredata-tech qualify --limit 50
```

Qualification calls the native registry first and then available cross-check sources. One failed candidate does not abort the batch.

## Incremental semantics

Ordinary rediscovery of an already qualified candidate does not put it back into the queue.

A true upstream update event can set `requeue=true`, which returns the candidate to `PENDING`. Packagist's official metadata changes feed and RubyGems update activity use this behavior.

```text
rediscovery only: QUALIFIED -> QUALIFIED
real update:       QUALIFIED -> PENDING -> qualification
upstream delete:   *         -> DELETED
```

## Wikidata discovery hardening

The public Wikidata Query Service is not used for one monolithic query with recursive `P279*`, many `OPTIONAL` joins and `SERVICE wikibase:label`.

IvoireData now splits discovery into two layers:

1. cheap class-specific WDQS queries that return only entity IDs for direct instances of programming languages and software frameworks;
2. batched `wbgetentities` calls through the Wikibase Action API to retrieve labels and claims (`P856`, `P1324`, `P2078`, `P348`).

This follows Wikidata's own optimization guidance to avoid the label service on expensive queries and keeps failures isolated per class. GitHub Linguist remains the broader language inventory; Wikidata is an evidence/enrichment seed rather than the sole global language catalogue.

## NuGet repository semantics

NuGet `projectUrl` is a project/home page. It is not repository metadata. IvoireData only accepts an actual repository field as NuGet repository evidence. This prevents documentation URLs such as Microsoft Learn from being misclassified as source repositories.

## Current official harvesting adapters

- Packagist popular index
- Packagist full package-name list
- Packagist metadata changes feed
- RubyGems recent/new activity
- pub.dev ranked package-name completion API
- pub.dev complete package-name mirror API
- PyPI JSON Simple API (explicit full scan)

Other ecosystems remain discoverable through the existing global discovery layer and will receive native bulk adapters when their registries expose stable supported APIs.
