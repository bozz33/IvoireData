# Rights & access policy

IvoireData distingue l’accès technique du droit de redistribution.

- `A_REDISTRIBUTABLE`: licence ouverte vérifiée; collecte, transformation et redistribution permises selon les termes et l’attribution de la source.
- `B_SOURCE_TERMS`: données ouvertes ou accessibles avec obligations spécifiques (share-alike, non-commercial, plateforme, etc.).
- `C_PUBLIC_LOCAL_INGEST`: source publique sans licence générale de redistribution établie. Le contenu peut être collecté localement si directement accessible, utilisé pour extraction factuelle/RAG et conservé avec provenance; les documents bruts ne sont pas remiroirés publiquement par défaut.
- `C_POINTER_ONLY_LICENSE_UNCLEAR`: metadata/pointer tant que l’ingestion automatisée n’est pas appropriée.
- `D_RESEARCH_OR_DATASET_TERMS`: microdonnées ou accès de recherche; pas de corpus général avant examen des termes.

## Règles invariantes

1. Pas de contournement d’authentification, CAPTCHA, paywall ou contrôle par rôle.
2. Chaque artefact conserve `source_id`, URL canonique, date de récupération, type MIME et SHA-256.
3. Les données personnelles/sensibles passent en quarantaine avant toute utilisation.
4. Les faits dérivés citent la source et, si pertinent, la période de validité.
5. Les textes réglementaires/fiscaux sont versionnés afin de ne pas mélanger des règles de périodes différentes.
6. CIV-Eval reste isolé de l’entraînement.
