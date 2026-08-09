# Dataset Card — IvoireData Registry v0.1.0

## Summary
IvoireData v0.1.0 is a curated metadata registry and acquisition design for Côte d’Ivoire AI data. It does not claim to contain all raw source data. Its first responsibility is to make acquisition **reproducible, attributable and legally reviewable**.

## Coverage
- Source records: 60
- Priority gaps: 22
- Country code: CIV
- Primary language of institutional sources: French
- Local-language seed resources verified: Dyula, Baoulé, Ebrié; additional languages are explicit collection gaps.

## Intended uses
- RAG and knowledge retrieval over authoritative sources
- continued pretraining/fine-tuning **only** on rights-compatible subsets
- speech/translation research with licensed/consented sources
- creation of Ivorian evaluation benchmarks
- dataset discovery and partnership planning

## Out-of-scope by default
- raw identifiable health records
- telecom CDR/location histories
- private banking/customer data
- voter/identity databases
- scraped private/social-media conversations without a lawful basis and appropriate rights
- gated research microdata in a general-purpose LLM corpus

## Known limitations
The registry is a researched seed, not a legal opinion. License terms may change, and some public institutional websites do not publish a clear machine-learning or redistribution license. Those sources are therefore pointer-only until reviewed.
