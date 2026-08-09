# Automatiser la préparation corpus → tokenizer → données d'entraînement

> **Hors responsabilité opérationnelle d'IvoireData.**
>
> IvoireData livre le `data_lake/` réel, classé et mis à jour. Ce document explique comment l'équipe modèle peut automatiser tout ce qui vient ensuite : sélection, nettoyage avancé, filtres, déduplication, corpus, tokenizer, tokenisation, packing, sharding et chargement d'entraînement.

Voir d'abord [`DATA_HANDOFF_CONTRACT.md`](DATA_HANDOFF_CONTRACT.md).

---

## 1. Architecture recommandée

```text
IvoireData data_lake/
        │
        ▼
01 INVENTORY / FREEZE INPUT
        │
        ▼
02 ELIGIBILITY / RIGHTS GATE
        │
        ▼
03 PARSING / CANONICAL DOCUMENTS
        │
        ▼
04 NORMALIZATION / CLEANING
        │
        ▼
05 PII / SECRETS / SAFETY FILTERS
        │
        ▼
06 QUALITY FILTERS
        │
        ▼
07 EXACT DEDUP
        │
        ▼
08 NEAR-DUP / FUZZY DEDUP
        │
        ▼
09 DOMAIN / SOURCE CLASSIFICATION
        │
        ▼
10 EVAL CONTAMINATION GATE
        │
        ▼
11 MIXTURE / SAMPLING
        │
        ▼
12 TRAIN / VALIDATION / TEST SPLIT
        │
        ▼
13 CORPUS RELEASE
        │
        ▼
14 TOKENIZER TRAINING
        │
        ▼
15 TOKENIZER VALIDATION
        │
        ▼
16 TOKENIZATION
        │
        ▼
17 DOCUMENT BOUNDARIES / EOD
        │
        ▼
18 SEQUENCE PACKING
        │
        ▼
19 SHARDING / INDEXING
        │
        ▼
20 TRAINING LOADER
```

Chaque étape doit être **idempotente** : relancer la même étape avec les mêmes entrées/configuration doit produire le même résultat ou au minimum le même manifest logique.

---

## 2. Ne jamais travailler directement sur le data lake vivant

Avant de fabriquer un corpus, figer les entrées :

```text
training_workspace/
└── inputs/
    └── snapshot-2026-08-09/
        ├── ivoiredata-catalog.json
        ├── source-selection.json
        └── checksums.json
```

Deux options :

1. copier les fichiers sélectionnés dans un snapshot immuable ;
2. conserver uniquement la liste exacte des chemins + SHA-256 si les fichiers source ne seront jamais réécrits en place.

La première approche est la plus simple à reproduire.

---

## 3. Format canonique interne

Tous les formats (PDF, HTML, CSV, JSON, Excel, Parquet...) doivent converger vers un format de document canonique avant nettoyage avancé.

Exemple JSONL :

```json
{
  "doc_id": "sha256:...",
  "text": "contenu textuel...",
  "source_id": "civ_dgi",
  "domain": "taxation",
  "provider": "Direction Générale des Impôts",
  "source_url": "https://...",
  "retrieved_at": "2026-08-09T18:00:00Z",
  "rights_tier": "C_PUBLIC_LOCAL_INGEST",
  "content_sha256": "...",
  "document_date": "2026-01-15",
  "title": "...",
  "metadata": {}
}
```

Le texte destiné au modèle et la provenance doivent rester séparés.

---

## 4. Parsing

### Documents Web/PDF

Objectif : supprimer la structure technique sans supprimer l'information métier.

Extraire si possible :

- titre ;
- texte principal ;
- sections ;
- tableaux ;
- listes ;
- date du document ;
- auteur/institution ;
- URL ;
- langue détectée ;
- références réglementaires/numéros de textes.

Ne pas transformer immédiatement chaque page en petits chunks RAG : le pré-entraînement préfère généralement conserver des **documents cohérents** et laisser l'étape de tokenisation/packing gérer les séquences.

### Données tabulaires

Une ligne CSV brute n'est pas toujours un bon texte d'entraînement. Conserver au moins deux représentations :

```text
structured/
  table.parquet
textualized/
  table.jsonl
```

Exemple de textualisation déterministe :

```text
Source : ANStat. Région : Gbêkê. Année : 2021. Population : 1 352 900 habitants.
```

Les templates de textualisation doivent être versionnés.

---

## 5. Nettoyage / normalisation

Pipeline type :

```text
Unicode normalize
→ remove control chars
→ normalize line endings
→ collapse pathological whitespace
→ remove navigation boilerplate
→ remove repeated headers/footers
→ repair common PDF hyphenation
→ preserve accents/case by default
→ normalize URLs only if policy says so
```

### Unicode

NFC ou NFKC peuvent être utilisés selon la stratégie tokenizer. Ne jamais changer silencieusement de normalisation entre deux versions de corpus.

### A éviter

- supprimer tous les accents français ;
- mettre tout en minuscules sans raison ;
- supprimer chiffres, unités ou symboles monétaires ;
- supprimer la ponctuation juridique ;
- supprimer les retours de paragraphes utiles.

---

## 6. Filtres PII / secrets

Le pipeline doit avoir un **gating explicite avant le corpus**.

Détecter au minimum :

- emails personnels ;
- numéros de téléphone ;
- numéros de cartes/identifiants sensibles ;
- secrets/tokens/API keys ;
- mots de passe exposés ;
- coordonnées bancaires ;
- données médicales individuelles ;
- identifiants administratifs individuels ;
- dumps accidentels de bases privées.

Sorties possibles :

```text
KEEP
REDACT
QUARANTINE
DROP
```

Chaque décision doit garder un `reason_code`.

Exemple :

```json
{
  "doc_id": "...",
  "decision": "QUARANTINE",
  "reasons": ["PERSONAL_PHONE", "PERSONAL_EMAIL"]
}
```

Ne jamais supprimer silencieusement : produire des statistiques de rejet.

---

## 7. Qualité

Ne pas utiliser un score unique opaque. Conserver plusieurs métriques :

- longueur texte ;
- ratio caractères alphabétiques ;
- ratio chiffres ;
- ratio symboles ;
- répétition de lignes ;
- répétition n-grams ;
- densité de liens ;
- ratio mots uniques ;
- proportion de caractères invalides ;
- boilerplate ;
- langue/confidence ;
- source authority ;
- extraction PDF suspecte ;
- document presque vide.

Puis calculer une décision :

```text
PASS
REVIEW
DROP
```

Le seuil doit pouvoir varier par domaine. Un tableau statistique de 20 lignes ne doit pas être rejeté avec les mêmes règles qu'un article de 2 000 mots.

---

## 8. Déduplication exacte

Avant toute méthode coûteuse :

```text
canonical_text = normalize_for_dedup(text)
exact_hash = SHA256(canonical_text)
```

Dédupliquer :

1. dans la même source ;
2. entre versions d'une source ;
3. entre toutes les sources.

Conserver la meilleure copie selon une règle stable :

```text
source officielle > miroir
licence claire > licence incertaine
version complète > version tronquée
métadonnées riches > pauvres
plus récente si même document versionné
```

Ne pas confondre **nouvelle version d'un texte réglementaire** avec doublon : la date/version doit entrer dans la logique.

---

## 9. Déduplication fuzzy / near-duplicate

Approches recommandées par ordre de coût :

```text
normalized hash
→ shingles + MinHash/LSH
→ SimHash éventuel
→ embeddings/semantic dedup seulement si nécessaire
```

Pour le Web, MinHash sur shingles permet de retirer :

- même article recopié ;
- pages imprimables ;
- version mobile/desktop ;
- communiqués repris par plusieurs ministères ;
- documents quasi identiques avec menus différents.

Conserver :

```json
{
  "cluster_id": "...",
  "canonical_doc_id": "...",
  "members": ["..."],
  "similarity_method": "minhash",
  "threshold": 0.85
}
```

Le seuil est un paramètre de release, pas une constante cachée dans le code.

---

## 10. Contamination de l'évaluation

Créer/figer les jeux d'évaluation **avant** la release d'entraînement ou au minimum enregistrer leurs hashes.

Avant train :

```text
training candidate
     │
     ├─ exact hash vs eval
     ├─ n-gram overlap vs eval
     └─ near-duplicate vs eval
           ↓
      DROP / REVIEW
```

Ne pas mettre les documents d'évaluation dans le corpus de pré-entraînement si l'objectif est de mesurer la généralisation.

---

## 11. Classification domaine/source

Réutiliser le `domain` IvoireData comme premier niveau :

```text
agriculture
administration
business_law
demography
economy
education
environment_climate
extractives_energy
geography
governance
health
labor
land_housing
law_justice
media_communication
public_procurement
social_protection
taxation
telecom
tourism_culture
transport
water_sanitation
multidomain
```

Le pipeline downstream peut ajouter des sous-domaines sans modifier IvoireData.

---

## 12. Mélange / sampling

Ne pas simplement concaténer toutes les sources : une source énorme écraserait les autres.

Créer un fichier de mixture :

```yaml
seed: 20260809
domains:
  agriculture:
    weight: 1.0
    max_fraction: 0.15
  education:
    weight: 1.0
    max_fraction: 0.15
  health:
    weight: 1.0
    max_fraction: 0.10
  taxation:
    weight: 1.2
    max_fraction: 0.10
sources:
  civ_datagouv_catalog:
    max_fraction: 0.20
```

Les valeurs ci-dessus sont des **exemples**, pas des poids recommandés universels.

Toujours générer un rapport :

```text
documents par source
tokens estimés par source
documents par domaine
tokens estimés par domaine
pourcentage final
rejets
```

---

## 13. Splits

Une stratégie simple et reproductible :

```text
train 98%
validation 1%
test 1%
```

Ce n'est qu'un exemple. Le plus important est :

- hash-based split déterministe ;
- pas de documents quasi identiques dans deux splits ;
- les membres d'un même cluster de dédup restent dans le même split ;
- certains benchmarks peuvent être totalement séparés du corpus.

Exemple :

```python
bucket = int(sha256(doc_id).hexdigest()[:8], 16) % 10000
```

Puis attribuer des plages fixes à train/val/test.

---

## 14. Release de corpus

Structure recommandée :

```text
corpus/releases/civ-pretrain-0001/
├── manifest.json
├── mixture.yaml
├── quality-report.json
├── dedup-report.json
├── rights-report.json
├── source-stats.parquet
├── train/
├── validation/
└── test/
```

Le `manifest.json` doit inclure :

- release ID ;
- date ;
- input IvoireData catalog hash ;
- commit du pipeline downstream ;
- configuration cleaning ;
- configuration PII ;
- configuration qualité ;
- configuration dedup ;
- seed ;
- documents gardés/rejetés ;
- bytes ;
- tokens estimés ;
- distribution domaines/sources ;
- checksums des shards.

Une release publiée pour entraînement devient immuable.

---

## 15. Tokenizer : processus automatisable

Le tokenizer appartient à la génération du modèle, pas à IvoireData.

### 15.1 Échantillon d'entraînement tokenizer

Ne pas entraîner le tokenizer uniquement sur la plus grosse source.

Créer un échantillon stratifié :

```text
sample par domaine
→ cap par source
→ shuffle avec seed fixe
→ tokenizer-training.txt/jsonl
```

### 15.2 Choisir la famille

Selon l'architecture :

- BPE ;
- Unigram ;
- WordPiece ;
- byte-level BPE.

Le choix doit être fixé avec l'architecture du modèle.

### 15.3 Validation tokenizer

Mesurer au minimum :

- taille vocabulaire ;
- couverture Unicode ;
- tokens/caractère ;
- tokens/mot ;
- longueur moyenne par domaine ;
- comportement sur nombres, dates, FCFA, unités ;
- noms ivoiriens ;
- termes administratifs/juridiques ;
- URL/code si le modèle doit en traiter ;
- stabilité des special tokens.

### 15.4 Artefacts

```text
tokenizers/model-17m-v1/
├── tokenizer.json
├── tokenizer_config.json
├── special_tokens.json
├── training_sample.sha256
├── metrics.json
└── README.md
```

Le hash du tokenizer doit être enregistré dans chaque checkpoint d'entraînement.

La bibliothèque Hugging Face Tokenizers permet notamment d'entraîner un tokenizer depuis un itérateur et d'expliciter normalizer, pre-tokenizer et trainer ; elle convient à une implémentation locale reproductible. Voir `UPSTREAM_SOURCES.md` pour les références.

---

## 16. Tokenisation

Après gel du tokenizer :

```text
corpus release
   ↓
read document
   ↓
encode text
   ↓
append EOD/document separator
   ↓
write token IDs
```

Ne jamais réentraîner/modifier le tokenizer au milieu d'une tokenisation de release.

Produire des stats :

```text
total_tokens
min/mean/p50/p95/p99 document tokens
empty documents
very long documents
per-domain token counts
per-source token counts
```

---

## 17. Document boundaries et EOD

Pour un modèle autoregressif GPT-like, préserver la notion de frontière documentaire.

Schéma courant :

```text
[doc A tokens] <EOD> [doc B tokens] <EOD> ...
```

Ne pas concaténer silencieusement deux documents sans marqueur si le framework suppose des frontières.

Le comportement exact doit être aligné sur le loader/framework d'entraînement.

---

## 18. Packing

Objectif : remplir les séquences de longueur `seq_len` en limitant le padding.

Exemple `seq_len=2048` :

```text
docA 700 + EOD
docB 900 + EOD
docC 446 + EOD
-----------------
≈ 2048 tokens
```

Conserver une stratégie déterministe et documenter :

- séquence maximum ;
- autorisation de couper un document ;
- EOD ;
- padding ;
- overlap éventuel ;
- seed de shuffle.

---

## 19. Sharding

Ne pas produire un fichier géant.

Exemple :

```text
tokenized/model-17m-v1/
├── train/
│   ├── shard-00000.*
│   ├── shard-00001.*
│   └── ...
├── validation/
└── test/
```

Chaque shard doit avoir :

- checksum ;
- nombre documents/séquences ;
- nombre tokens ;
- taille bytes ;
- split ;
- tokenizer ID ;
- corpus release ID.

---

## 20. Format final selon framework

### Option générique

Conserver une release intermédiaire JSONL/Parquet :

```json
{"text":"...","doc_id":"...","source_id":"...","domain":"..."}
```

Puis adapter vers le framework.

### Megatron / NeMo

Les outils NVIDIA de pré-entraînement utilisent des données pré-tokenisées et Megatron Core expose l'`IndexedDataset` basé sur des fichiers `.bin` + `.idx`. La documentation NeMo fournit aussi un préprocesseur qui transforme du JSON textuel en format mmap Megatron.

Le pipeline doit donc avoir un **adapter final**, par exemple :

```text
canonical corpus
├── adapter_hf
├── adapter_megatron
├── adapter_custom_pytorch
└── adapter_<framework_frere>
```

Ne jamais coupler les étapes nettoyage/dedup à un seul framework de training.

---

## 21. Orchestration locale minimale

Pour une seule machine, commencer simple :

```text
Makefile / PowerShell
        ↓
Python CLI
        ↓
stage manifests + exit codes
```

Exemple logique :

```makefile
freeze:
	python pipeline.py freeze --input ../IvoireData/data_lake

normalize:
	python pipeline.py normalize --release $(RELEASE)

filter:
	python pipeline.py filter --release $(RELEASE)

dedup:
	python pipeline.py dedup --release $(RELEASE)

mix:
	python pipeline.py mix --release $(RELEASE)

tokenizer:
	python pipeline.py tokenizer-train --release $(RELEASE)

tokenize:
	python pipeline.py tokenize --release $(RELEASE)

shard:
	python pipeline.py shard --release $(RELEASE)

all: freeze normalize filter dedup mix tokenizer tokenize shard
```

Quand la chaîne devient distribuée, un orchestrateur (Dagster, Prefect, Airflow...) peut remplacer Make sans changer les étapes métier.

---

## 22. Reprise après panne

Chaque étape écrit :

```text
.stage/<stage>/manifest.json
.stage/<stage>/_SUCCESS
```

Une étape n'est terminée que si `_SUCCESS` existe et si les checksums correspondent.

En cas de panne :

```text
étape précédente validée
       ↓
reprendre étape courante
```

Ne jamais considérer un dossier partiellement écrit comme une release valide.

Utiliser des écritures temporaires :

```text
shard-00012.tmp
→ fsync/close
→ checksum
→ rename shard-00012.bin
```

---

## 23. Logs / métriques

Chaque run doit produire :

```json
{
  "run_id": "...",
  "release": "...",
  "stage": "dedup",
  "started_at": "...",
  "finished_at": "...",
  "input_docs": 1000000,
  "output_docs": 870000,
  "rejected_docs": 130000,
  "errors": 0,
  "config_sha256": "..."
}
```

Rapports minimum :

- ingestion ;
- parsing ;
- PII ;
- qualité ;
- dedup ;
- mixture ;
- tokenizer ;
- tokenisation ;
- shards.

---

## 24. Configuration versionnée

Ne pas coder les seuils en dur.

Exemple :

```yaml
release: civ-pretrain-0001
seed: 20260809
cleaning:
  unicode: NFKC
  collapse_whitespace: true
quality:
  min_chars: 200
  max_repetition_ratio: 0.35
pii:
  mode: redact_or_drop
dedup:
  exact: true
  minhash:
    enabled: true
    threshold: 0.85
tokenizer:
  type: bpe
  vocab_size: 32000
  special_tokens:
    - <PAD>
    - <BOS>
    - <EOS>
    - <EOD>
training_data:
  seq_len: 2048
```

Ces valeurs sont **illustratives**. Elles doivent être choisies et testées par l'équipe modèle.

---

## 25. Gates avant entraînement

Ne lancer l'entraînement que si :

```text
[ ] input catalog figé
[ ] droits/access report généré
[ ] 0 erreur critique parsing
[ ] PII gate passé
[ ] quality report disponible
[ ] exact dedup terminé
[ ] near-dedup terminé ou explicitement désactivé
[ ] eval contamination vérifiée
[ ] splits figés
[ ] corpus manifest hashé
[ ] tokenizer figé + hashé
[ ] tokenizer metrics acceptées
[ ] token count final connu
[ ] shards tous checksum-valides
[ ] training config référence corpus + tokenizer exacts
```

---

## 26. Pipeline cible pour le modèle 17M

```text
IvoireData
   │
   └── data_lake/domains/...
             │
             ▼
      freeze_inputs.py
             │
             ▼
        canonicalize.py
             │
             ▼
          clean.py
             │
             ▼
        pii_filter.py
             │
             ▼
       quality_filter.py
             │
             ▼
       exact_dedup.py
             │
             ▼
      minhash_dedup.py
             │
             ▼
       contamination.py
             │
             ▼
         mixture.py
             │
             ▼
          split.py
             │
             ▼
       release_corpus.py
             │
             ▼
      train_tokenizer.py
             │
             ▼
    validate_tokenizer.py
             │
             ▼
         tokenize.py
             │
             ▼
          pack.py
             │
             ▼
          shard.py
             │
             ▼
      framework_adapter.py
             │
             ▼
        train model 17M
```

---

## 27. Ce qu'IvoireData doit fournir pour que cette automatisation fonctionne

Le downstream ne doit pas avoir à crawler Internet lui-même. Il consomme uniquement :

```text
data_lake/catalog.json
+
data_lake/domains/<domain>/<source>/manifest.json
+
raw / tables / documents de la source
```

C'est le contrat entre les deux projets.

---

## 28. Références techniques

Voir [`UPSTREAM_SOURCES.md`](UPSTREAM_SOURCES.md) pour les références officielles. En particulier :

- Hugging Face Tokenizers : entraînement depuis fichiers/itérateurs, normalizers, pre-tokenizers et trainers ;
- NVIDIA NeMo : préparation des données de pré-entraînement ;
- NVIDIA Megatron Core : `IndexedDataset`, fichiers `.bin/.idx`, datasets mélangés et loaders GPT.

Ces références servent d'exemples d'intégration ; le pipeline de nettoyage/qualité/déduplication doit rester indépendant du framework choisi pour le modèle.
