# Connecteurs IvoireData v0.7

Chaque connecteur transforme un upstream public en ressource dlt isolée sous :

```text
data_lake/domains/<domain>/<source_id>/
├── raw/
├── tables/
├── documents/
└── manifest.json
```

## `data_gouv_ci`

- découvre le catalogue Data.gouv.ci ;
- matche dataset ciblé par `id` **ou** `slug` ;
- ignore individuellement les datasets `/full` indisponibles/mal formés ;
- snapshot CSV raw + tables Parquet ;
- provenance URL, dataset ID, SHA-256, chemin brut.

## `world_bank_wdi`

- API World Bank v2 ;
- pays `CIV` ;
- catalogue des indicateurs ;
- lots jusqu’à 60 indicateurs ;
- un batch HTTP 400 est subdivisé récursivement pour isoler un indicateur fautif ;
- raw JSON + Parquet.

## `world_bank_projects`

- API `search.worldbank.org/api/v2/projects` ;
- code pays ISO2 `CI` ;
- pagination par `rows`/`os` ;
- projets, secteurs/thèmes et métadonnées normalisés par dlt ;
- chaque page JSON est conservée dans `raw/`.

## `ilostat_ref_area`

- backend CSV `rplumber.ilo.org/data/indicator?ref_area=CIV` ;
- abandon du RDS/`pyreadr` ;
- toutes les observations reçues sont conservées ;
- **`obs_status` est un statut d’observation et n’est jamais utilisé comme filtre de fréquence** ;
- raw CSV + Parquet.

## `faostat_country`

Connecteur v0.7 spécialisé pour FAOSTAT.

- télécharge un ensemble contrôlé de ZIP bulk officiels ;
- streaming vers fichier temporaire avec limite `max_bytes_per_file` ;
- SHA-256 + snapshot ZIP dans `raw/` ;
- lit les CSV normalisés à l’intérieur des archives ;
- filtre uniquement les lignes Côte d’Ivoire via les libellés pays configurés ;
- produit des tables distinctes pour production cultures/élevage, sécurité alimentaire, prix, utilisation des terres et commerce agricole ;
- échoue explicitement si les téléchargements réussissent mais qu’aucune ligne Côte d’Ivoire n’est trouvée.

Les bulk mondiaux peuvent être volumineux. La limite par fichier est configurable dans `runtime_sources.json`.

## `uis_country`

Connecteur v0.7 spécialisé UNESCO UIS.

- API publique UIS ;
- filtre `geoUnit=CIV` ;
- snapshots JSON ;
- définitions des indicateurs ;
- définition géographique Côte d’Ivoire ;
- séries d’indicateurs structurées dans `uis_data` ;
- `start_year`/`end_year` configurables.

## `geoboundaries`

- API geoBoundaries ;
- lorsque `.../CIV/` est un directory listing HTML, exploration ADM0–ADM5 ;
- GeoJSON + tables de géométries/propriétés.

## `osm_geofabrik`

- snapshot PBF Côte d’Ivoire par défaut ;
- téléchargement temporaire puis remplacement ;
- checksum distant lorsqu’il existe + SHA-256 local ;
- livraison classée `SNAPSHOT_ONLY` lorsque le PBF est le payload final attendu.

## `public_web`

- sites/PDF institutionnels sans API adaptée ;
- même domaine ;
- crawl borné ;
- robots.txt ;
- liens individuels 404/SSL ignorés sans tuer toute la source ;
- page racine reste critique ;
- HTML/PDF/text ;
- `metadata_only=true` bloque routes/formats de microdonnées avant requête ;
- `verify_ssl=false` est permis uniquement comme fallback explicite et apparaît comme `DEGRADED_TLS` dans l’audit.

## `http_file`

CSV, JSON, JSONL, XLS, XLSX ou Parquet direct : snapshot raw + table structurée.

## `bulk_catalog`

Connecteur générique conservé pour d’autres catalogues bulk. FAOSTAT et UIS **ne l’utilisent plus en v0.7** : ils ont leurs connecteurs spécialisés.

## Règle de choix

1. API ou format officiel stable → connecteur spécialisé ;
2. fichier direct → `http_file` ;
3. gros catalogue réellement indexable → `bulk_catalog` ;
4. pages/documents publics → `public_web` ;
5. source contrôlée → aucune microdonnée automatique ; métadonnées publiques uniquement si la politique le permet.

## Validation d’un connecteur

Un connecteur n’est considéré complet qu’après :

1. tests unitaires hors réseau ;
2. CI verte ;
3. sync réel contre l’upstream ;
4. `ivoiredata audit` confirmant une livraison non vide et cohérente ;
5. inspection d’un échantillon de tables/raw ;
6. documentation des droits, fréquence et formats.

Voir [`ADDING_SOURCE.md`](ADDING_SOURCE.md) et [`AUDIT.md`](AUDIT.md).
