# Connecteurs IvoireData v0.6

Les connecteurs transforment une source externe en ressource dlt. Le moteur place ensuite leurs sorties dans :

```text
data_lake/domains/<domain>/<source_id>/
├── raw/
├── tables/
├── documents/
└── manifest.json
```

Les tables normalisées sont chargées en Parquet.

## `data_gouv_ci`

Source : `data.gouv.ci` / Data Fair.

- découvre le catalogue public ;
- table `datagouv_catalog` ;
- table par dataset ;
- archive chaque CSV `/full` réellement récupéré dans `raw/` ;
- conserve URL, dataset ID, index de ligne, SHA-256 et chemin brut ;
- mémorise les signatures de métadonnées pour éviter les rechargements inutiles.

## `world_bank_wdi`

Source : World Bank API v2.

- cible `CIV` ;
- charge les indicateurs ;
- interroge par lots ;
- archive les réponses JSON reçues dans `raw/` ;
- produit `worldbank_wdi_indicators` et `worldbank_wdi`.

## `ilostat_ref_area`

Source : ILOSTAT bulk backend.

- cible `CIV` ;
- fréquence annuelle `A` par défaut ;
- archive le RDS source dans `raw/` ;
- lit avec `pyreadr` ;
- normalise en table Parquet ;
- conserve URL, SHA-256 et chemin du RDS.

## `geoboundaries`

Source : API geoBoundaries.

- charge les métadonnées CIV ;
- récupère le GeoJSON officiel ;
- produit géométries/propriétés structurées ;
- conserve le SHA-256 du GeoJSON dans les données normalisées.

## `osm_geofabrik`

Source : OpenStreetMap via Geofabrik.

- PBF par défaut ;
- GPKG et SHP ZIP également supportés ;
- binaire stocké dans `raw/` du package source ;
- vérifie le MD5 distant lorsque disponible ;
- téléchargement temporaire `.part` puis remplacement atomique ;
- taille, MD5, SHA-256 et URL sont exposés dans la table de snapshot.

## `bulk_catalog`

Pour les services de gros téléchargements, notamment FAOSTAT et UNESCO UIS.

Mode par défaut : **catalogue seulement**.

- découvre les liens CSV/JSON/XML/XLSX/Parquet/ZIP/GZ ;
- chaque source a sa propre table `bulk_catalog_<source_id>` ;
- `download_patterns` sélectionne les payloads à matérialiser ;
- `max_downloads` et `max_bytes` protègent le disque ;
- les fichiers sélectionnés vont dans `raw/`.

Exemple :

```json
{
  "connector": "bulk_catalog",
  "options": {
    "download_patterns": ["pattern-officiel-a-valider"],
    "max_downloads": 2,
    "max_bytes": 250000000
  }
}
```

Une source `bulk_catalog` avec `max_downloads=0` livre **le catalogue**, pas encore le payload statistique complet. La couverture doit le signaler clairement.

## `public_web`

Pour les sites institutionnels sans API adaptée.

- HTTP public uniquement ;
- respect `robots.txt` ;
- même domaine ;
- crawl borné ;
- limites pages/taille ;
- extraction HTML/PDF/text ;
- SHA-256 ;
- snapshots des pages/documents changés dans `documents/` ;
- table `public_documents` pour recherche/inspection ;
- `metadata_only=true` bloque les routes/formats de microdonnées avant requête.

Utilisé notamment pour DGI, CNPS, Justice, ministères, ARTCI, Douanes, etc., en attendant des connecteurs plus structurés lorsque l'upstream le permet.

## `http_file`

Pour une URL directe CSV/JSON/JSONL/XLS/XLSX/Parquet.

- archive le fichier reçu dans `raw/` ;
- produit une table normalisée ;
- conserve source URL, format, SHA-256, chemin brut et index de ligne ;
- Excel : conserve le nom de feuille.

## Isolation

Chaque source utilise un pipeline dlt séparé. Un changement de schéma ou une erreur d'une source ne doit pas mélanger ses tables/états avec ceux d'une autre source.

## Règle de choix

1. API/dataset structuré connu → connecteur spécialisé ;
2. fichier direct → `http_file` ;
3. catalogue bulk → `bulk_catalog` ;
4. site/document public → `public_web` ;
5. source contrôlée → pas de payload automatique, métadonnées seulement si autorisé.

## Ajouter un connecteur

Voir [`ADDING_SOURCE.md`](ADDING_SOURCE.md). Un nouveau connecteur doit :

- conserver la provenance ;
- respecter droits/accès ;
- définir timeouts/retry ;
- être idempotent ou détecter les changements ;
- sauvegarder le brut lorsqu'approprié ;
- produire une sortie structurée lorsque possible ;
- ne jamais écrire les données réelles dans Git ;
- avoir des tests unitaires hors réseau.
