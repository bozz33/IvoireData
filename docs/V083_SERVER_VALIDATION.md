# IvoireData v0.8.3 — migration serveur et preuve anti-retéléchargement

Ce protocole valide la migration v0.8.1/v0.8.2 vers v0.8.3 sur le data lake réel. Il ne supprime ni `data_lake/` ni `.ivoiredata/`.

## 1. Sauvegarde et arrêt du scheduler

```bash
docker compose stop scheduler
mkdir -p backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
tar -czf "backups/ivoiredata-state-pre-083-${STAMP}.tgz" .ivoiredata
```

Sauvegarder également `data_lake/` sur le second disque selon la politique d'exploitation.

## 2. Mise à jour

```bash
git status
git pull
docker compose build --pull
docker compose up -d api

docker compose exec api ivoiredata --version
curl -fsS http://127.0.0.1:8000/health
```

Attendu : `0.8.3`.

Ne pas supprimer `.ivoiredata/state/upstreams.json`. Il contient les signatures, validators et chemins de cache nécessaires à la reprise incrémentale.

## 3. État avant migration

```bash
docker compose exec api ivoiredata upstreams
docker compose exec api ivoiredata audit
docker compose exec api ivoiredata quality-audit
```

Optionnel mais recommandé :

```bash
du -sh data_lake .ivoiredata
find data_lake -type f | wc -l
```

## 4. Première migration structurée

Migrer séquentiellement les principales sources afin d'isoler tout incident upstream :

```bash
for s in \
  civ_datagouv_catalog \
  civ_ilostat \
  civ_faostat \
  civ_worldbank_wdi \
  civ_worldbank_projects \
  civ_uis \
  civ_geoboundaries \
  civ_osm_geofabrik
do
  echo "===== FIRST RUN $s ====="
  docker compose exec api ivoiredata sync "$s" --force || exit 1
done
```

`--force` signifie « vérifier maintenant ». Les connecteurs v0.8.3 continuent de comparer version/signature/cache et ne doivent pas volontairement retransférer une version identique déjà matérialisée.

Le premier passage peut adopter des snapshots/tables antérieurs ou télécharger ce qui manque pour construire les nouveaux états incrémentaux.

## 5. Rapports structurés

```bash
docker compose exec -T api python - <<'PY'
from ivoiredata.engine import IvoireDataEngine
from ivoiredata.delivery import source_paths
import json

e = IvoireDataEngine()
reports = [
    ("civ_datagouv_catalog", "datagouv_sync_stats.json"),
    ("civ_ilostat", "ilostat_sync_stats.json"),
    ("civ_faostat", "faostat_sync_stats.json"),
    ("civ_worldbank_wdi", "worldbank_wdi_sync_stats.json"),
    ("civ_worldbank_projects", "worldbank_projects_sync_stats.json"),
    ("civ_uis", "uis_sync_stats.json"),
]
for sid, filename in reports:
    p = source_paths(e.settings, e.registry.get(sid))["raw"] / filename
    print(f"\n===== {sid} =====")
    if not p.exists():
        print("MISSING", p)
        continue
    print(json.dumps(json.loads(p.read_text()), ensure_ascii=False, indent=2))
PY
```

### Data.gouv.ci

Vérifier notamment :

```text
catalog_visible_anonymous
selected
unchanged
downloaded
via_full
via_lines
failed
failures
removed_upstream
business_rows_changed
```

Le critère de complétude est le catalogue public anonyme réellement retourné au moment du run. La cible n'est pas un ancien total historique figé.

### ILOSTAT

Vérifier notamment :

```text
selected_indicators
unchanged
with_country_rows
without_country_rows
failed
ref_area_unchanged_gate
indicator_toc_requested
network_queries
```

Le run complet doit couvrir les indicateurs associés à la signature REF_AREA Côte d'Ivoire ; l'ancien résultat de quelques centaines de lignes n'est plus utilisé comme preuve d'exhaustivité.

### FAOSTAT

Vérifier notamment :

```text
selected_current_datasets
unchanged
adopted_v081
downloaded
replayed_from_local_cache
with_country_rows
without_country_rows
failed
skipped_oversize
deferred_budget
backlog_count
business_rows
downloaded_bytes
```

Tant que `backlog_count > 0`, le mirror n'est pas complet. Le scheduler v0.8.3 retente automatiquement une source structurée partielle après 6 h par défaut, sans `force`, donc les domaines déjà acquis sont ignorés/rejoués depuis cache et seuls les éléments manquants sont transférés.

Pour accélérer manuellement un backlog FAOSTAT :

```bash
docker compose exec api ivoiredata sync civ_faostat --force
```

Répéter jusqu'à `failed=0` et `backlog_count=0`. Les signatures empêchent le retéléchargement volontaire des ZIP déjà matérialisés.

## 6. Deuxième run immédiat — preuve anti-retéléchargement

Relancer immédiatement exactement les mêmes sources :

```bash
for s in \
  civ_datagouv_catalog \
  civ_ilostat \
  civ_faostat \
  civ_worldbank_wdi \
  civ_worldbank_projects \
  civ_uis \
  civ_geoboundaries \
  civ_osm_geofabrik
do
  echo "===== SECOND RUN $s ====="
  docker compose exec api ivoiredata sync "$s" --force || exit 1
done
```

En l'absence de changement upstream entre les deux passages, le résultat recherché est :

```text
Data.gouv : downloaded = 0, datasets déjà matérialisés = unchanged
ILOSTAT   : ref_area_unchanged_gate = true, indicator_toc_requested = false
FAOSTAT   : downloaded = 0 lorsque le backlog du premier run est terminé
WDI       : source lastupdated identique, pas de sweep massif
OSM       : MD5 identique, pas de transfert PBF
UIS       : HTTP 304 quand disponible, sinon SHA identique sans réécriture
Projects  : HTTP validator ou hash canonique identique
```

Contrôler le cache réseau :

```bash
docker compose exec api ivoiredata upstreams civ_datagouv_catalog
docker compose exec api ivoiredata upstreams civ_ilostat
docker compose exec api ivoiredata upstreams civ_faostat
docker compose exec api ivoiredata upstreams civ_worldbank_wdi
docker compose exec api ivoiredata upstreams civ_worldbank_projects
docker compose exec api ivoiredata upstreams civ_uis
docker compose exec api ivoiredata upstreams civ_osm_geofabrik
```

## 7. Vérification du data lake

```bash
docker compose exec api ivoiredata audit
docker compose exec api ivoiredata quality-audit
docker compose exec api ivoiredata coverage-audit
docker compose exec api ivoiredata upstreams
docker compose exec api ivoiredata ci-gold
```

Pour CI Gold :

```text
active ERROR = 0
active EMPTY = 0
critical quality issues = 0
structured partial failures = 0
structured backlogs = 0
P0 blockers = 0
manifest v3 complete = true
document metadata gate = true
```

## 8. Full public

Après validation des grosses sources :

```bash
docker compose exec api ivoiredata sync --all-public --force
```

Pour les portails Web, `--force` déclenche une vérification immédiate. ETag/Last-Modified/304 sont utilisés lorsqu'ils existent ; sans validator serveur, le body doit être reçu pour calculer le SHA, mais un SHA identique ne crée pas un nouveau snapshot/chunk.

## 9. Scheduler et qualification

Une source structurée avec backlog/échec partiel est marquée `partial` au niveau du cycle automatique et ne compte pas comme un cycle CI Gold parfait. Elle est réessayée après `partial_retry_hours` (6 h par défaut) sans redownload forcé.

Une fois tous les rapports structurés propres :

```bash
docker compose --profile run up -d scheduler
docker compose exec api ivoiredata qualification start
```

Puis :

```bash
docker compose exec api ivoiredata qualification status
docker compose exec api ivoiredata ci-gold
```

## 10. Ne jamais faire pour une mise à jour normale

```text
- ne pas supprimer data_lake/
- ne pas supprimer .ivoiredata/
- ne pas supprimer upstreams.json pour « forcer » une mise à jour
- ne pas effacer les tables parce qu'une ressource a disparu upstream : IvoireData archive l'historique
- ne pas lancer simultanément API sync + scheduler + sync-once sur la même source pour contourner le lock
```

Le mécanisme normal est : vérifier la version distante, comparer, reprendre le cache local en cas de crash et transférer uniquement ce qui est nouveau/modifié/manquant.
