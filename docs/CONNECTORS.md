# Connecteurs IvoireData

Les connecteurs transforment une source externe en ressource dlt. Le routeur sélectionne le connecteur à partir de `configs/runtime_sources.json` ou, à défaut, de l’URL du registre.

## `data_gouv_ci`

Source : `data.gouv.ci` / Data Fair.

- découvre le catalogue public ;
- crée une table `datagouv_catalog` ;
- crée une table par dataset ;
- garde URL, identifiant dataset, index de ligne et SHA-256 du brut ;
- mémorise la signature des métadonnées afin d’éviter un rechargement inutile.

## `world_bank_wdi`

Source : World Bank API v2.

- cible le pays `CIV` ;
- charge la liste des indicateurs ;
- interroge les indicateurs par lots ;
- conserve indicateur, période, valeur et provenance.

## `ilostat_ref_area`

Source : ILOSTAT bulk backend.

- cible `CIV` ;
- charge par fréquence (`A` annuel par défaut) ;
- lit le format RDS avec `pyreadr` ;
- normalise les enregistrements dans une table par fréquence ;
- conserve URL et SHA-256 du fichier source.

## `geoboundaries`

Source : API geoBoundaries.

- charge les métadonnées CIV ;
- suit le lien GeoJSON officiel ;
- stocke propriétés et géométries ;
- conserve le SHA-256 du GeoJSON.

## `osm_geofabrik`

Source : extraits OpenStreetMap distribués par Geofabrik.

- format par défaut : `ivory-coast-latest.osm.pbf` ;
- supporte aussi GPKG et SHP ZIP ;
- stocke le binaire dans `data_lake/raw_external/civ_osm_geofabrik/` ;
- compare le MD5 distant lorsque disponible ;
- remplace le snapshot uniquement lorsqu’il change ;
- enregistre taille, MD5, SHA-256, chemin local et URL.

Le PBF n’est pas commité dans Git.

## `bulk_catalog`

Utilisé pour les services officiels proposant de gros téléchargements, notamment FAOSTAT et UNESCO UIS.

Mode par défaut : **catalogue seulement**.

- extrait les liens de téléchargement CSV/JSON/XML/XLSX/Parquet/ZIP/GZ ;
- enregistre l’URL et le libellé ;
- peut télécharger certains fichiers si `download_patterns` est configuré ;
- `max_downloads` et `max_bytes` empêchent un téléchargement massif accidentel.

Exemple :

```json
{
  "connector": "bulk_catalog",
  "options": {
    "download_patterns": ["SDG", "Education"],
    "max_downloads": 2,
    "max_bytes": 250000000
  }
}
```

## `public_web`

Pour les sites institutionnels sans API adaptée.

- requêtes HTTP publiques uniquement ;
- respect de `robots.txt` ;
- crawl borné au même domaine ;
- limite du nombre de pages et de taille ;
- extraction HTML/PDF ;
- chunking ;
- SHA-256 ;
- retraitement uniquement si le contenu change.

Ce connecteur est utilisé pour DGI, CNPS, Justice, ministères, ARTCI, Douanes, etc.

## `http_file`

Pour une URL pointant directement vers CSV, JSON, JSONL, XLS/XLSX ou Parquet.

Le moteur télécharge et transforme le fichier en enregistrements tabulaires normalisés.

## Règle de choix

1. API/dataset structuré connu → connecteur spécialisé.
2. Fichier direct → `http_file`.
3. Catalogue bulk très volumineux → `bulk_catalog`.
4. Site/document public → `public_web`.
5. Source contrôlée → pas d’ingestion automatique des données ; métadonnées publiques seulement si `metadata_only=true`.

## Ajouter un connecteur

Voir [`ADDING_SOURCE.md`](ADDING_SOURCE.md). Chaque nouveau connecteur doit :

- conserver la provenance ;
- avoir une politique de timeout ;
- être idempotent ou détecter les changements ;
- ne jamais écrire de payload réel dans Git ;
- disposer de tests unitaires sans dépendre du réseau.
