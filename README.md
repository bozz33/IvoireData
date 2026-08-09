# IvoireData 🇨🇮

**v0.2.0 — fondation multisectorielle de données pour une IA adaptée à la Côte d’Ivoire**

La phase active couvre les données ivoiriennes **hors langues** : administration, fiscalité, droit, économie, agriculture, santé, éducation, télécoms, mines/pétrole/énergie, environnement, transport, foncier/logement, eau, météo, géographie et développement.

## Ce que contient le dépôt

- un registre actif de **60+ sources officielles et collections prioritaires** ;
- découverte dynamique du catalogue officiel `data.gouv.ci` (**202 jeux observés au 2026-08-09**) ;
- ingestion de sources publiques HTML/PDF avec SHA-256 et provenance ;
- séparation `CIV-Open` / `CIV-Public-RAG` / `CIV-Facts` / `CIV-Microdata` / `CIV-Eval` ;
- **25 faits structurés** de démarrage avec sources ;
- CI et tests ;
- langues et speech volontairement différés.

Le paquet de recherche v0.2 maintient également le registre détaillé de 98 sources/collections établi pendant l’audit; le registre Git privilégie les familles actives et laisse les catalogues officiels se développer dynamiquement plutôt que de figer des centaines de lignes à la main.

## Sources publiques à droits incertains

Elles ne sont plus ignorées : si une ressource est publiquement accessible sans contournement, IvoireData peut la récupérer **localement**, conserver le document brut dans `data/raw` (gitignored), en extraire texte/tableaux/faits et produire des chunks RAG avec URL/date/hash. Le document intégral n’est pas automatiquement remiroiré dans ce dépôt public lorsqu’un droit de redistribution n’est pas établi.

## Démarrage

```bash
pip install -e '.[ingest,dev]'
python scripts/validate_registry.py
python scripts/build_public_queue.py
python scripts/validate_seed_facts.py
pytest -q
```

Ingestion d’une page ou d’un PDF public :

```bash
python scripts/ingest_public_web.py --source-id civ_dgi https://www.dgi.gouv.ci/
```

Découverte du catalogue open data :

```bash
python scripts/discover_data_gouv_ci.py
```

## Stockage

Git contient le code, les registres, manifests et petits faits vérifiables. Les gros fichiers restent dans `data/raw`, `data/processed`, DVC/LakeFS ou un object storage afin que le projet puisse monter à des dizaines/centaines de Go sans casser Git.

## Licences

Le code original du projet est Apache-2.0. Les sources externes conservent leurs propres droits et obligations. Voir `docs/RIGHTS_AND_ACCESS.md`.
