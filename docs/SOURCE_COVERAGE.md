# Couverture multisectorielle — v0.2.0

Les langues sont volontairement hors périmètre actif de cette phase.

| Secteur | Sources prioritaires | Traitement |
|---|---|---|
| Open data national | data.gouv.ci | catalogue complet + téléchargements/API sous Licence Ouverte |
| Statistiques | ANStat / NADA | métadonnées + microdonnées selon le niveau d’accès |
| Fiscalité / FNE | DGI | pages/PDF publics → texte, faits et RAG avec provenance |
| Droit / Justice | OHADA, Ministère de la Justice | textes, procédures, rapports et statistiques publics |
| Administration | servicepublic.gouv.ci | démarches, pièces, coûts et délais quand publiés |
| Budget / dette | Budget, Trésor | bulletins, lois/rapports et tableaux publics |
| Marchés publics | DGMP / SIGOMAP | plans, appels d’offres, résultats, statistiques publics |
| Douanes / commerce | Douanes ivoiriennes | Excel/PDF publics, statistiques 1999–2025 |
| Finance | BCEAO / APIF | séries macro-financières et inclusion financière |
| Agriculture | data.gouv.ci, ANStat EAA, FAOSTAT | productions, prix, cheptel, commerce, recensements |
| Santé | RASS, E-DEPPS, WHO | rapports, établissements et indicateurs agrégés |
| Éducation | MENA, UNESCO UIS | annuaires, programmes, examens et indicateurs |
| Télécoms | ARTCI | abonnements, Internet, mobile money, revenus et rapports |
| Mines / pétrole / énergie | MMPE, MNV Énergie | production, ressources, GES, investissements |
| Environnement / climat | MINETE/SIE, World Bank Climate | rapports, CDN, indicateurs, climat |
| Transport | Ministère des Transports | aérien, portuaire, routier, ferroviaire |
| Foncier / logement | MULCV, IDUFCI public | procédures et référentiels publics; aucun contournement d’accès parcellaire |
| Eau / assainissement | ONEP, ONAD | rapports, infrastructures, politiques et procédures |
| Météo | SODEXAM | stations, bulletins et données publiquement accessibles |
| Géographie | OSM/Geofabrik, geoBoundaries | données géographiques ouvertes selon leurs licences |
| Développement | World Bank, FAOSTAT, ILOSTAT | APIs et séries filtrées pour CIV |

## Sources publiques sans licence ouverte explicite

Elles sont utilisables dans le pipeline **local** : téléchargement lorsqu’elles sont directement accessibles, calcul du SHA-256, extraction du texte/tableau, découpage RAG et extraction de faits. Le dépôt public ne republie pas automatiquement le document intégral lorsqu’un droit de redistribution n’a pas été établi. Les authentifications, CAPTCHAs, paywalls ou contrôles par rôle ne sont jamais contournés.
