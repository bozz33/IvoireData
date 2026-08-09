# IvoireCorpus

`ivoiredata corpus-build VERSION TABLE...` transforme les tables acquises en corpus JSONL : nettoyage Unicode, filtre qualité, déduplication exacte, provenance, sharding et manifest SHA-256.

```bash
ivoiredata corpus-build civ-0.1 datagouv_rgph_2021 public_documents --output corpora
```

Chaque version est immuable. Les mises à jour de sources alimentent la version suivante ; elles ne modifient pas un corpus déjà utilisé pour un entraînement.

Tokenizer BPE optionnel :
```bash
pip install -e '.[training]'
ivoiredata tokenizer-train corpora/civ-0.1 --vocab-size 32000
```
