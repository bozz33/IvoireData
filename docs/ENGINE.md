# IvoireData Engine v0.4

IvoireData utilise **dlt OSS** comme moteur Extract/Normalize/Load. Le code IvoireData ajoute ce que dlt ne connaît pas : registre ivoirien, politique de fraîcheur, provenance, connecteurs CIV, qualité, déduplication, fabrication de corpus et API de consultation.

Flux : `Source Registry -> connector -> dlt resource -> filesystem/S3 -> query/corpus builder`.

Le connecteur `data_gouv_ci` distribue dynamiquement chaque jeu Data Fair vers sa propre table dlt. Les signatures de métadonnées sont conservées dans le `resource_state` dlt ; si la signature n'a pas changé, le dataset n'est pas retéléchargé. Le catalogue est rafraîchi à chaque synchronisation.

Pour les pages/PDF et fichiers publics, les connecteurs calculent SHA-256 et ne retraitent que les contenus modifiés.

## Fraîcheur

- `configs/runtime_sources.json` définit `refresh_hours` et `auto_sync`.
- le scheduler appelle les sources arrivées à échéance ;
- dlt conserve son état avec les données dans la destination ;
- `.ivoiredata/state/freshness.json` fournit l'état opérationnel local.

Un corpus d'entraînement reste figé : l'updater prépare de nouvelles données, puis `corpus-build` crée une nouvelle version immuable.
