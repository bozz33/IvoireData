# Couverture multisectorielle — v0.5.0

Les langues restent volontairement hors périmètre actif. La couverture ci-dessous décrit **le niveau technique réellement implémenté**, pas seulement les sources connues.

## Niveaux

- **Structuré** : connecteur dédié, données transformées en tables ou snapshot local vérifiable.
- **Bulk catalog** : catalogue officiel suivi automatiquement, gros fichiers téléchargés seulement sur sélection.
- **Web public** : crawl/documentation/PDF avec hash et provenance.
- **Metadata only** : catalogue public synchronisé mais microdonnées contrôlées exclues.
- **Manuel/contrôlé** : source référencée mais aucune ingestion automatique du payload.

| Secteur | Sources prioritaires | Niveau v0.5 |
|---|---|---|
| Open data national | data.gouv.ci | **Structuré** : Data Fair catalogue + datasets |
| Statistiques nationales | ANStat / NADA | **Metadata only** pour le catalogue ; microdonnées contrôlées manuelles |
| Fiscalité / FNE | DGI | **Web public** automatique |
| Droit / Justice | OHADA, Ministère Justice | **Web public** automatique |
| Administration | Service Public | **Web public** automatique |
| Dette publique | Trésor | **Web public** automatique |
| Marchés publics | DGMP | **Web public** automatique |
| Douanes / commerce | Douanes | **Web public** automatique |
| Finance | BCEAO / APIF | **Web public** automatique |
| Agriculture nationale | data.gouv.ci, Ministère Agriculture | structuré + web public |
| Agriculture internationale | FAOSTAT | **Bulk catalog** automatique ; payloads volumineux sur sélection |
| Travail / emploi | ILOSTAT | **Structuré** pour CIV, fréquence annuelle par défaut |
| Santé nationale | RASS, E-DEPPS | **Web public** automatique |
| Santé internationale | WHO | **Web public** pendant la transition de l’API WHO |
| Éducation nationale | MENA | **Web public** automatique |
| Éducation internationale | UNESCO UIS | **Bulk catalog** automatique |
| Télécoms | ARTCI | **Web public** automatique |
| Mines / pétrole / énergie | MMPE, MNV | **Web public** automatique |
| Environnement | Ministère / SIE | **Web public** automatique |
| Climat international | World Bank Climate | **Web public** ; connecteur spécialisé à développer si besoin analytique |
| Transport | Ministère Transports | **Web public** automatique |
| Foncier / logement | IDUFCI / Construction | **Web public** automatique |
| Eau / assainissement | ONEP / ONAD | **Web public** automatique |
| Météo | SODEXAM | **Web public** automatique |
| Géographie administrative | geoBoundaries | **Structuré** GeoJSON |
| Géographie OSM | Geofabrik | **Structuré snapshot** PBF local + checksums |
| Développement macro | World Bank WDI | **Structuré** API v2 CIV |
| Projets World Bank | World Bank Projects | **Web public** actuellement |

## Ce qui est prêt pour l’auto-update local

Les sources configurées avec `auto_sync=true` sont contrôlées par `configs/runtime_sources.json`. La commande :

```bash
ivoiredata coverage
```

fournit le nombre actuel de sources auto, leur répartition par connecteur et domaine.

## Ce qui reste volontairement non automatique

- microdonnées ANStat nécessitant une acceptation/autorisation ;
- datasets classés `D_*` ;
- fichiers pour lesquels la redistribution ou l’accès n’est pas suffisamment clair ;
- téléchargements bulk potentiellement massifs tant qu’aucun motif n’est explicitement configuré.

## Principe de vérité

Une source n’est pas considérée « couverte » simplement parce que son URL est dans le registre. La couverture devient réelle lorsqu’un connecteur est configuré, testé et capable de conserver provenance + fraîcheur + résultat local.
