# Ajouter une source à IvoireData

Chaque source doit être décrite avant d’être automatisée. Le but est d’éviter les connecteurs ad hoc sans provenance, sans politique de fraîcheur ou sans compréhension des droits.

## 1. Ajouter la source au registre

Modifier `registry/sources.csv` :

```csv
source_id,title,domain,provider,source_url,rights_tier,access_tier,priority
civ_example,Exemple,economy,Institution,https://example.ci/,C_PUBLIC_LOCAL_INGEST,OPEN_PUBLIC,P1
```

Champs :

- `source_id` : identifiant stable ;
- `title` : nom lisible ;
- `domain` : domaine métier ;
- `provider` : institution productrice ;
- `source_url` : page/API/fichier officiel ;
- `rights_tier` : niveau de droits IvoireData ;
- `access_tier` : public, mixte, contrôlé ;
- `priority` : importance/autorité de la source.

## 2. Choisir le connecteur

Priorité :

1. connecteur structuré spécialisé ;
2. `http_file` pour fichier direct ;
3. `bulk_catalog` pour catalogue de gros téléchargements ;
4. `public_web` pour site/PDF.

Ne pas créer un connecteur spécialisé si une brique générique couvre correctement la source.

## 3. Configurer la fraîcheur

Dans `configs/runtime_sources.json` :

```json
"civ_example": {
  "connector": "public_web",
  "refresh_hours": 168,
  "auto_sync": true,
  "options": {"crawl": true, "max_pages": 25}
}
```

Exemples de fréquence :

- données très dynamiques : 24 h ;
- institution/statistiques périodiques : 72–168 h ;
- limites, plans ou référentiels lents : 720 h ;
- immuable : synchronisation manuelle.

## 4. Source MIXED/contrôlée

Ne jamais rendre automatiquement les fichiers contrôlés téléchargeables en changeant seulement `access_tier`.

Si la page de catalogue est publique mais les microdonnées contrôlées :

```json
"options": {"metadata_only": true}
```

Le moteur peut alors synchroniser le catalogue public mais pas les fichiers soumis à autorisation.

## 5. Provenance obligatoire

Chaque enregistrement ou snapshot doit conserver, selon le format :

- `source_id` ;
- URL source ;
- identifiant dataset/document ;
- date de récupération via l’état du moteur ;
- SHA-256 du contenu brut lorsque possible ;
- métadonnées de version/dernière modification si disponibles.

## 6. Détection de changement

Un connecteur doit utiliser au moins une stratégie :

- ETag ;
- Last-Modified ;
- checksum officiel ;
- SHA-256 ;
- version de dataset ;
- signature de métadonnées ;
- curseur/incrémental dlt.

## 7. Tests

Les tests ne doivent pas dépendre d’Internet. Tester les fonctions de parsing, URL, sécurité, filtrage, permissions et transformation avec des fixtures locales/mocks.

Puis :

```bash
python scripts/validate_registry.py
python -m compileall -q src scripts
pytest -q
ivoiredata --help
```

## 8. Documentation

Mettre à jour au minimum :

- `docs/SOURCES.md` si la source crée une nouvelle famille ;
- `docs/CONNECTORS.md` si nouveau connecteur ;
- `docs/SOURCE_COVERAGE.md` si le niveau de couverture change ;
- `README.md` uniquement si cela change les capacités principales.

## 9. Première synchronisation

```bash
ivoiredata sync civ_example
ivoiredata status --public
```

Inspecter le résultat local avant d’activer un usage dans IvoireCorpus.
