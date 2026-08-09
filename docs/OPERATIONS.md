# Exploitation locale d’IvoireData

Ce document décrit l’usage quotidien du moteur sur un PC ou un serveur local.

## Vérifier l’installation

```bash
ivoiredata coverage
ivoiredata sources --public
ivoiredata status --public
```

`coverage` donne la couverture globale. `status` indique pour chaque source : connecteur, fréquence, dernier succès et si une synchronisation est due.

## Synchroniser une source

```bash
ivoiredata sync civ_datagouv_catalog
ivoiredata sync civ_worldbank_wdi
ivoiredata sync civ_ilostat
```

Forcer une vérification :

```bash
ivoiredata sync civ_dgi --force
```

## Synchroniser les sources arrivées à échéance

```bash
ivoiredata scheduler --once
```

Le scheduler permanent :

```bash
ivoiredata scheduler --interval 3600
```

L’intervalle est la fréquence de réveil du scheduler. Il ne remplace pas `refresh_hours` : une source à 168 h ne sera pas téléchargée chaque heure.

## Windows

Le dépôt fournit `scripts/install_windows_scheduler.ps1` pour créer une tâche planifiée locale. Le processus doit s’exécuter sous un compte ayant accès au dossier IvoireData et à Internet.

## Où regarder en cas de problème

1. `ivoiredata status --public` ;
2. `.ivoiredata/state/freshness.json` ;
3. l’espace disque disponible ;
4. l’URL de la source ;
5. `robots.txt` pour les crawlers ;
6. éventuelles modifications d’API ou de format côté producteur.

Une erreur sur une source ne doit pas supprimer le dernier corpus valide. Le nouvel état n’est marqué `success` qu’après un run dlt terminé.

## Sauvegardes

Sauvegarder au minimum :

```text
data_lake/
.ivoiredata/
corpora/
tokenizer/
```

Recommandation : un second disque physique ou une sauvegarde externe hors du PC principal. GitHub sauvegarde le code mais **pas les données**.

## Espace disque

OpenStreetMap PBF, archives statistiques et futurs corpus peuvent devenir volumineux. Surveiller `data_lake/raw_external/` et `corpora/`. Les catalogues bulk n’effectuent pas de gros téléchargements sans `download_patterns` explicites.

## Construire une nouvelle version de corpus

Après synchronisation et validation :

```bash
ivoiredata corpus-build civ-0.2 TABLE1 TABLE2 TABLE3 --output corpora
```

Ne jamais réutiliser le même numéro de version pour remplacer un corpus déjà utilisé en entraînement.

## Requêtes locales

```bash
ivoiredata query 'SELECT * FROM datagouv_catalog LIMIT 20'
```

L’API locale peut être démarrée avec :

```bash
uvicorn ivoiredata.api:app --host 127.0.0.1 --port 8000
```

## Politique d’incident

- source indisponible : conserver la dernière version locale et laisser l’état en erreur ;
- HTML à la place d’un fichier attendu : ne pas considérer le run comme réussi ;
- changement de schéma : mettre à jour le connecteur/test avant de reconstruire le corpus ;
- conflit de valeurs : conserver les deux provenances et utiliser le mécanisme de cross-check ;
- changement de licence/conditions : désactiver `auto_sync` jusqu’à réévaluation.
