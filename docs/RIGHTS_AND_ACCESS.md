# Rights & access policy

IvoireData distingue **accès technique**, **usage local**, **droit de redistribution** et **droit d’intégrer au corpus d’entraînement**. Une URL publiquement accessible ne signifie pas automatiquement que son contenu peut être redistribué sans condition.

## Tiers de droits

- `A_REDISTRIBUTABLE` : licence ouverte vérifiée ; collecte, transformation et redistribution permises selon ses termes et obligations d’attribution.
- `B_SOURCE_TERMS` : données accessibles avec obligations propres à la source (par exemple attribution/share-alike). Le connecteur doit préserver la provenance et le corpus doit respecter ces termes.
- `C_PUBLIC_LOCAL_INGEST` : source publique sans licence générale de redistribution établie. Collecte locale autorisée par la politique IvoireData pour extraction factuelle, indexation et préparation interne ; pas de miroir public automatique du document brut.
- `C_POINTER_ONLY_LICENSE_UNCLEAR` : métadonnées/pointeur seulement tant que les droits ne sont pas suffisamment établis.
- `D_RESEARCH_OR_DATASET_TERMS` : microdonnées ou accès de recherche ; aucune ingestion automatique générale du payload.

## Access tiers

- `OPEN` : données publiques directement accessibles ;
- `OPEN_PUBLIC` : pages/documents publics ;
- `MIXED` : partie publique + partie contrôlée ;
- niveaux research/review/controlled : traitement manuel selon conditions.

## `metadata_only`

Une source `MIXED` peut être configurée avec :

```json
{"metadata_only": true}
```

Dans ce mode :

- catalogue, description, dictionnaires et pages de métadonnées publiques peuvent être synchronisés ;
- routes contenant des marqueurs de téléchargement/microdonnées sont filtrées avant requête ;
- extensions de formats statistiques sensibles (`.sav`, `.dta`, `.rds`, `.sas7bdat`, etc.) sont exclues du crawler ;
- cela ne constitue jamais une autorisation de télécharger le fichier protégé.

ANStat/NADA utilise ce mode pour la couverture automatique de son catalogue public.

## Règles invariantes

1. Pas de contournement d’authentification, CAPTCHA, paywall ou contrôle par rôle.
2. Chaque artefact conserve `source_id`, URL canonique et, lorsque possible, checksum/hash.
3. Les données personnelles/sensibles sont exclues ou passent en quarantaine avant usage.
4. Les faits dérivés conservent la source et la période de validité lorsqu’elle existe.
5. Les textes réglementaires/fiscaux sont versionnés pour ne pas mélanger des règles de périodes différentes.
6. Un corpus déjà utilisé pour entraînement reste immuable.
7. Les payloads locaux ne sont pas poussés vers GitHub.
8. Si les conditions d’une source changent, `auto_sync` doit être désactivé jusqu’à réévaluation.

## Sources avec obligations spécifiques

OpenStreetMap/Geofabrik conserve ses obligations ODbL et l’attribution à OpenStreetMap contributors. Les autres sources `B_SOURCE_TERMS` doivent être évaluées selon leurs propres conditions avant redistribution du corpus final.

## Séparation code / données

GitHub contient le code, le registre, la configuration et la documentation. `data_lake/`, `corpora/` et les snapshots binaires restent locaux et sont ignorés par Git.
