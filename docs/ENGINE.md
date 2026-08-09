# IvoireData Engine v0.4.1

IvoireData utilise **dlt OSS** comme moteur Extract/Normalize/Load. IvoireData ajoute le registre ivoirien, la politique de fraîcheur, la provenance, les connecteurs CIV, la qualité, la déduplication, la fabrication de corpus et les interfaces API/CLI.

Le moteur est désormais **local-first et local-only pour le stockage** :

`Source Registry -> connector -> dlt resource -> data_lake/ local -> query/corpus builder`

Il n'utilise ni S3, ni R2, ni MinIO.

## Stockage local

- `data_lake/` : données collectées et normalisées par dlt ;
- `.ivoiredata/state/` : fraîcheur, checkpoints et état opérationnel ;
- `corpora/` : versions immuables d'IvoireCorpus ;
- `tokenizer/` : tokenizer entraîné localement.

Les dossiers de données sont hors Git.

## Fraîcheur

- `configs/runtime_sources.json` définit `refresh_hours` et `auto_sync` ;
- `ivoiredata scheduler` vérifie localement les sources arrivées à échéance ;
- le connecteur `data_gouv_ci` conserve les signatures de métadonnées dans le `resource_state` dlt ;
- les pages/PDF/fichiers publics utilisent SHA-256 pour éviter de retraiter un contenu inchangé ;
- `.ivoiredata/state/freshness.json` conserve l'historique de réussite/échec.

Commandes utiles :

```bash
ivoiredata status --public
ivoiredata sync --due
ivoiredata scheduler --once
ivoiredata scheduler --interval 3600
```

Un corpus d'entraînement reste figé : l'updater maintient le lac local à jour, puis `corpus-build` produit une nouvelle version immuable.
