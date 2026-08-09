# Contrat de sortie IvoireData → pipeline d'entraînement

Ce document définit **où s'arrête IvoireData** et **où commence le pipeline de préparation du modèle**.

## Responsabilité IvoireData

IvoireData doit livrer des données :

- réelles, récupérées depuis les sources déclarées ;
- locales ;
- classées par domaine puis par source ;
- accompagnées de provenance ;
- datées ;
- versionnables par hash/checksum lorsque possible ;
- conservées dans leur forme brute lorsque les droits et le mode d'accès le permettent ;
- également exposées sous forme de tables/documents exploitables lorsque le connecteur sait les structurer ;
- mises à jour automatiquement selon `refresh_hours` ;
- sans contourner authentification, CAPTCHA, paywall ou contrôle d'accès.

IvoireData **ne décide pas** du corpus final d'un modèle et ne doit pas modifier un corpus déjà utilisé pour un entraînement.

## Arborescence de livraison

La cible est :

```text
data_lake/
├── catalog.json
└── domains/
    ├── agriculture/
    │   ├── civ_faostat/
    │   │   ├── raw/
    │   │   ├── tables/
    │   │   ├── documents/
    │   │   └── manifest.json
    │   └── civ_agriculture_ministry/
    │       └── ...
    ├── health/
    │   └── ...
    ├── education/
    │   └── ...
    ├── economy/
    │   └── ...
    └── geography/
        └── ...
```

### `raw/`

Payload brut reçu de l'upstream lorsqu'il est raisonnable et autorisé de le conserver localement : CSV, JSON, XLSX, PDF, archive, etc. Un sidecar `.meta.json` peut conserver : URL, date de récupération, MIME, taille, SHA-256.

### `tables/`

Sortie dlt normalisée pour les sources structurées. Une source dispose de son propre pipeline et de son propre état afin d'éviter qu'une évolution de schéma d'une institution casse une autre source.

### `documents/`

Documents/pages publics archivés ou représentations documentaires exploitables lorsque le connecteur Web/PDF les gère. Le contenu soumis à des restrictions reste traité selon `RIGHTS_AND_ACCESS.md`.

### `manifest.json`

Carte d'identité opérationnelle de la source :

```json
{
  "source_id": "civ_faostat",
  "domain": "agriculture",
  "provider": "FAO",
  "source_url": "...",
  "rights_tier": "B_SOURCE_TERMS",
  "access_tier": "OPEN",
  "connector": "bulk_catalog",
  "status": "success",
  "started_at": "...",
  "finished_at": "...",
  "refresh_hours": 168,
  "inventory": {
    "tables": {"files": 0, "bytes": 0},
    "raw": {"files": 0, "bytes": 0},
    "documents": {"files": 0, "bytes": 0}
  }
}
```

### `catalog.json`

Index global généré à partir de toutes les sources. Il permet au pipeline downstream de découvrir les domaines et les sources sans lire `registry/sources.csv` ni connaître le code interne d'IvoireData.

## Contrat minimum d'une donnée downstream

Le pipeline d'entraînement doit pouvoir retrouver, directement ou via le manifest :

- `source_id` ;
- `domain` ;
- `provider` ;
- `source_url` ;
- `rights_tier` ;
- `access_tier` ;
- date de récupération ;
- hash/checksum lorsque disponible ;
- format ;
- chemin local.

## Données vivantes vs corpus figé

```text
IvoireData data_lake/
       │
       │ se met à jour
       ▼
Snapshot choisi par l'équipe modèle
       │
       │ figé
       ▼
Nettoyage / filtres / dédup / corpus
       │
       ▼
Tokenizer / tokenisation / shards
       │
       ▼
Entraînement
```

Le pipeline downstream doit enregistrer le hash ou la copie du `catalog.json` utilisé afin qu'un entraînement puisse être reproduit plus tard.

## Règle essentielle

**Le data lake n'est pas le corpus.** Le data lake est la livraison exhaustive et organisée des sources. Le corpus est une sélection/transformée construite à partir d'un état précis du data lake par l'équipe entraînement.
