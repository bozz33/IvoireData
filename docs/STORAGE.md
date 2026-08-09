# Stockage local

IvoireData est local-first. Pour la V1, **aucun serveur PostgreSQL, S3, R2 ou MinIO n'est requis**.

## Pourquoi les fichiers suffisent

Les données de pré-entraînement sont naturellement volumineuses et sont plus simples à versionner, sauvegarder et fournir au pipeline d'entraînement sous forme de fichiers Parquet/JSONL que sous forme de millions de lignes dans une base serveur.

- `data_lake/` : données acquises et normalisées par dlt ;
- `.ivoiredata/state/` : état opérationnel et fraîcheur ;
- `corpora/` : versions immuables d'IvoireCorpus ;
- `tokenizer/` : tokenizer local.

dlt et son client SQL local permettent de requêter les tables du filesystem. Une base serveur pourra être ajoutée plus tard uniquement si plusieurs machines/utilisateurs doivent écrire simultanément, si l'on veut une forte concurrence transactionnelle ou une API multi-utilisateur à grande échelle.

## Recommandation

Pour l'entraînement initial du modèle, conserver les payloads en fichiers locaux et les métadonnées/checkpoints localement. Sauvegarder régulièrement `data_lake/`, `.ivoiredata/` et `corpora/` sur un second disque.
