# Moteur IvoireData v0.8.0

## Rôle

Le moteur coordonne registre, configuration, connecteurs, dlt, manifests, catalogue, fraîcheur, contrôles dynamiques et qualification CI Gold.

## Initialisation

`IvoireDataEngine` charge :

```text
Settings
RuntimeControl
SourceRegistry
FreshnessStore
QualificationStore
```

Configuration effective :

```text
configs/runtime_sources.json
→ configs/ci_gold_sources.json
→ .ivoiredata/state/runtime_overrides.json
```

L’override local a toujours la priorité.

## Registre

`SourceRegistry.all()` retourne toutes les sources enregistrées, y compris désactivées.

`SourceRegistry.list()` exclut `enabled=false`, puis peut filtrer :

- sources publiques ;
- sources automatiques.

Un `sync(source_id)` direct refuse une source désactivée ou non autorisée à l’ingestion publique automatique.

## Routage connecteurs

Connecteurs spécialisés :

```text
data_gouv_ci
world_bank_wdi
world_bank_projects
faostat_country
uis_country
ilostat_ref_area
geoboundaries
osm_geofabrik
http_file
bulk_catalog
public_web
```

Les connecteurs spécialisés restent préférés lorsqu’une API/bulk stable existe. `public_web` couvre les pages/PDF institutionnels avec mêmes hôtes, robots.txt et limites de crawl.

## Métadonnées CI Gold

`metadata.py` fournit :

- identité pays CIV ;
- métadonnées source ;
- classification déterministe domaine principal/secondaires ;
- classification de type documentaire ;
- confiance/statut de classification.

Le moteur passe ces métadonnées aux connecteurs capables d’enrichir les lignes :

- `public_web` ;
- `data_gouv_ci` ;
- `world_bank_wdi`.

## Pipeline par source

Chaque source possède un pipeline dlt isolé et son dossier :

```text
data_lake/domains/<domain>/<source_id>/
```

Cette isolation limite les effets d’une évolution de schéma et simplifie suppression/restauration/requêtes.

## Manifest et catalogue

Après chaque tentative, le moteur écrit un manifest schema v3 contenant :

- sync ;
- delivery ;
- freshness ;
- transport ;
- rights ;
- metadata CIV ;
- warnings ;
- inventaire.

Le catalogue schema v3 regroupe les sources et domaines et porte `country_code=CIV`.

## Audit

`engine.audit()` expose :

- état source par source ;
- distribution sync/delivery/freshness/transport ;
- `rows.structured` ;
- `rows.documents` ;
- `rows.total_parquet`.

Les autres audits sont :

```text
coverage_audit()
quality_audit()
ci_gold()
write_ci_gold()
```

## Contrôle dynamique

`RuntimeControl` persiste dans :

```text
.ivoiredata/state/runtime_overrides.json
```

Modes :

```text
AUTOMATIC
MANUAL
DISABLED
```

L’interrupteur global `automatic_enabled` n’affecte jamais un sync manuel explicite.

## Scheduler

Le scheduler :

1. relit les réglages à chaque cycle ;
2. vérifie `automatic_enabled` ;
3. sélectionne les sources `enabled + auto_sync + due` ;
4. synchronise ;
5. écrit fraîcheur/manifests/catalogue ;
6. enregistre le cycle dans `QualificationStore`.

Les sync manuels ne sont pas enregistrés comme cycles de qualification.

## QualificationStore

Fichier :

```text
.ivoiredata/state/ci_gold_qualification.json
```

État minimum : début, dernier cycle, nombre de cycles, erreurs, dernières erreurs.

CI Gold exige au moins 14 jours réels, 14 cycles et aucune erreur pendant la fenêtre.

## CI Gold

`ci_gold.py` combine :

```text
coverage
quality/provenance
classification
freshness
stability
rights
handoff
```

Le score est informatif ; les gates obligatoires gardent la priorité.

## Erreurs et conservation

Une erreur upstream marque le run en erreur mais ne doit pas supprimer une ancienne livraison valide. L’audit peut alors afficher `STALE` + `SYNC_ERROR_WITH_STALE_DATA`.

## Frontière

Le moteur s’arrête au data lake CI Gold et aux preuves de qualification. Le nettoyage ML, PII, dédup, corpus, tokenizer et entraînement restent downstream.
