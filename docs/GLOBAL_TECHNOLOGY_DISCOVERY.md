# Global Technology Discovery Engine

This subsystem builds a dynamic catalog of programming languages, frameworks, libraries, runtimes and packages without turning every discovered project into an ingestion source.

## Authority model

Discovery and ingestion are deliberately separate.

1. **GitHub Linguist** discovers programming-language names.
2. **Wikidata** contributes language/framework website, repository, documentation and version claims.
3. **ecosyste.ms Packages** discovers package metadata across many registries/ecosystems.
4. **deps.dev** cross-checks package versions and package-to-project/repository mappings for supported ecosystems.
5. Registry/repository/documentation evidence is combined into an `officiality_score`.
6. A discovered technology remains `enabled_for_corpus=false` until a separate promotion policy enables it.

The catalog is persisted at:

```text
.ivoiredata/state/technology_catalog.json
```

## CLI

Audit the dynamic catalog:

```bash
ivoiredata-tech audit
```

Discover all programming languages known to GitHub Linguist:

```bash
ivoiredata-tech languages
```

Use a bounded run while testing:

```bash
ivoiredata-tech languages --limit 50
```

Discover languages and frameworks with structured authority metadata from Wikidata:

```bash
ivoiredata-tech wikidata --limit 500
```

Resolve one package through ecosyste.ms and, where supported, cross-check it with deps.dev:

```bash
ivoiredata-tech package npm react
ivoiredata-tech package pypi Django
ivoiredata-tech package packagist laravel/framework
ivoiredata-tech package cargo serde
```

Inspect the best-evidenced candidates:

```bash
ivoiredata-tech catalog --limit 100
ivoiredata-tech catalog --verified-only --limit 100
```

## Officiality scoring

Initial deterministic evidence weights:

- registry repository: +40
- deps.dev repository matching registry repository: +30
- deps.dev repository without registry match: +10
- official homepage: +5
- documentation URL: +15
- stable/default version: +10

Statuses:

- `VERIFIED_OFFICIAL`: score >= 80
- `PROBABLE_OFFICIAL`: score >= 55
- `CANDIDATE`: score >= 30
- `UNVERIFIED`: score < 30

This score is not a legal/training gate. It is an **authority-confidence gate** designed to prevent IvoireData from treating forks, tutorials or unrelated repositories as canonical documentation.

## Safety rule

Discovery is intentionally broad, but **never automatically enables corpus ingestion**. This prevents a global registry crawl from causing millions of packages to be downloaded.

The next layer will rank technologies by importance (language/runtime/framework/critical library), merge duplicate identities across PURL/Wikidata/repository evidence, and promote selected technologies into the official documentation source registry automatically.
