# Corpus et préparation d'entraînement — frontière de responsabilité

Depuis **v0.6**, le produit officiel d'IvoireData est le **data lake local vivant, classé par domaine/source**, pas un corpus de pré-entraînement.

```text
IvoireData
  → collecte
  → mise à jour
  → provenance
  → classement domaine/source
  → raw + tables Parquet + documents + manifests
  → data_lake/catalog.json

Équipe modèle
  → snapshot des entrées
  → nettoyage avancé
  → filtres PII/sécurité
  → qualité
  → déduplication exacte/fuzzy
  → contamination eval
  → mixture
  → train/validation/test
  → corpus figé
  → tokenizer
  → tokenisation
  → packing/sharding
  → loader d'entraînement
```

## Pourquoi cette séparation

Le `data_lake/` doit rester à jour. Un corpus ayant servi à un entraînement doit au contraire être figé et reproductible. Mélanger les deux responsabilités rendrait difficile de savoir quelles données exactes ont produit un checkpoint.

Le contrat précis de livraison d'IvoireData est décrit dans [`DATA_HANDOFF_CONTRACT.md`](DATA_HANDOFF_CONTRACT.md).

L'automatisation complète de la partie équipe modèle est décrite dans [`DOWNSTREAM_AUTOMATION.md`](DOWNSTREAM_AUTOMATION.md).

## Modules historiques

Le dépôt peut encore contenir des helpers historiques de corpus/tokenizer issus des versions précédentes. Ils ne constituent plus l'interface opérationnelle principale et ne doivent pas être considérés comme le pipeline officiel d'entraînement.

L'équipe modèle peut les réutiliser, les déplacer dans son propre dépôt ou les remplacer par son pipeline de préparation.

## Règle d'intégration

Le pipeline downstream doit commencer uniquement à partir de :

```text
data_lake/catalog.json
+
data_lake/domains/<domain>/<source_id>/manifest.json
+
raw/ tables/ documents/
```

Il ne doit pas dépendre des détails internes des connecteurs IvoireData.
