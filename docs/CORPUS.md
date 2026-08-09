# IvoireCorpus

IvoireCorpus est le produit destiné au pré-entraînement. Il est différent du `data_lake` : le lac reste vivant et se met à jour, tandis qu’une version de corpus est figée.

## Pipeline

```text
tables dlt / documents
        │
        ▼
row_to_training_text
        │
        ▼
nettoyage Unicode
        │
        ▼
score qualité
        │
        ▼
déduplication exacte
        │
        ▼
shards JSONL
        │
        ▼
manifest.json + manifest.sha256
```

## Construire

```bash
ivoiredata corpus-build civ-0.1 datagouv_rgph_2021 public_documents --output corpora
```

Options principales :

```bash
ivoiredata corpus-build civ-0.1 TABLE1 TABLE2 \
  --output corpora \
  --shard-size 100000 \
  --min-quality 0.35
```

## Enregistrement d’entraînement

Chaque ligne JSONL contient au minimum :

```json
{
  "text": "...",
  "sha256": "...",
  "quality": 0.82,
  "meta": {
    "source_id": "...",
    "source_url": "..."
  }
}
```

Les champs de provenance disponibles sont préservés dans `meta`.

## Immutabilité

Une version utilisée par un entraînement ne doit jamais être remplacée.

```text
civ-0.1 → training modèle A
nouvelles données
civ-0.2 → training modèle B
```

Le manifest contient la version, la date de création, les statistiques et `immutable=true`. Son propre SHA-256 est écrit dans `manifest.sha256`.

## Tokenizer

```bash
python -m pip install -e '.[training]'
ivoiredata tokenizer-train corpora/civ-0.1 --vocab-size 32000
```

Le tokenizer doit être versionné avec le modèle ou au minimum identifié par hash/version afin qu’un checkpoint puisse être reproduit.

## Mélange de sources

La V0.5 fabrique un corpus à partir des tables choisies. La pondération avancée par domaine/source sera une couche supplémentaire : agriculture, droit, économie, santé, éducation, connaissance générale, etc. Les statistiques du manifest doivent être inspectées avant tout entraînement.

## Exclusions

Ne pas inclure automatiquement :

- microdonnées contrôlées non autorisées ;
- documents dont les droits interdisent l’usage prévu ;
- HTML de navigation vide ou répétitif ;
- données de très faible qualité ;
- secrets, identifiants ou données personnelles non nécessaires.

Voir [`QUALITY_ASSURANCE.md`](QUALITY_ASSURANCE.md), [`RIGHTS_AND_ACCESS.md`](RIGHTS_AND_ACCESS.md) et [`SOURCES.md`](SOURCES.md).
