# Architecture IvoireData

```text
Registry → discovery queue → fetcher → raw local snapshot → SHA-256/provenance
                                   ↓
                          extraction HTML/PDF/table
                                   ↓
             ┌─────────────────────┼─────────────────────┐
             ↓                     ↓                     ↓
          CIV-Open              CIV-Facts           CIV-Public-RAG
             └─────────────────────┼─────────────────────┘
                                   ↓
                              AI / RAG / ETL
                                   ↓
                         CIV-Eval (held out)
```

Git versionne le code, les métadonnées, les manifests, les petits échantillons et les faits dérivés. Les gros artefacts bruts vont dans `data/raw` local, DVC/LakeFS ou un object storage.
