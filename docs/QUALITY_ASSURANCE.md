# Quality assurance

IvoireData does not treat a public page as automatically correct merely because it is official.

## Confidence ladder
1. raw table/API record from the primary producer;
2. official downloadable table/PDF;
3. official dataset page/table;
4. official narrative/visualisation;
5. secondary institutional reproduction;
6. third-party source.

When two values disagree, the lower-level factual record is quarantined from `gold` until a stronger source resolves the conflict. Both claims, URLs, observation date and resolution status are retained in `data/quality/conflicts.jsonl`.

## Licence provenance
A portal-level licence and an upstream producer licence are stored separately. If a dataset is derived from another open database, downstream redistribution must account for both layers rather than assuming the portal label erased upstream obligations.

## Temporal data
Fiscal, legal, administrative and regulatory facts require an effective date/version. Historical values remain queryable but must not silently answer a current-law question.
