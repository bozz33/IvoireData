# Matrice de couverture Côte d’Ivoire

La source de vérité machine-readable est `configs/ci_coverage.json`. Ce document explique comment l’interpréter.

## Statuts

- `COVERED` : le nombre minimal de sources attendues est réellement livré sans `EMPTY` ;
- `PARTIAL` : domaine identifié mais couverture/livraison encore incomplète ;
- `CONTROLLED` : domaine évalué mais payload soumis à droits/autorisation ;
- `UNAVAILABLE` : information identifiée mais non disponible publiquement dans une forme ingérable ;
- `UNRESOLVED` : source attendue connue mais non résolue/activée ;
- `MISSING` : domaine prioritaire sans source enregistrée adéquate.

## Familles évaluées

La matrice v1 couvre notamment :

1. administration ;
2. gouvernance ;
3. finances publiques ;
4. marchés publics ;
5. droit / justice ;
6. droit des affaires ;
7. fiscalité ;
8. élections ;
9. démographie ;
10. emploi / travail ;
11. protection sociale ;
12. économie / commerce / finance ;
13. agriculture ;
14. sécurité alimentaire ;
15. élevage / pêche ;
16. forêts ;
17. santé ;
18. éducation ;
19. enseignement supérieur ;
20. recherche ;
21. télécoms ;
22. médias / communication ;
23. mines ;
24. pétrole / gaz ;
25. électricité ;
26. environnement / climat / météo ;
27. géographie ;
28. foncier / logement ;
29. eau / assainissement ;
30. transport ;
31. routes ;
32. tourisme ;
33. culture ;
34. sport ;
35. histoire / mémoire ;
36. langues ivoiriennes ;
37. français ivoirien / nouchi ;
38. gastronomie.

## Sources institutionnelles renforcées en v0.8.0

| Famille | Source ajoutée | Priorité |
|---|---|---|
| Textes officiels / histoire institutionnelle | `civ_sgg_official_texts` | P0 |
| Finances publiques | `civ_dgbf_budget` | P0 |
| Enseignement supérieur / recherche | `civ_mesrs` | P0 |
| Élections | `civ_cei` | P0 |
| Routes | `civ_ageroute` | P0 |
| Électricité | `civ_anare` | P0 |
| Culture | `civ_culture` | P0 |
| Gouvernance | `civ_gouv_portal` | P0 |
| Tourisme | `civ_tourism` | P1 |
| Communication | `civ_communication` | P1 |
| Sport | `civ_sports` | P1 |

## Calcul dynamique

Exécuter :

```bash
ivoiredata coverage-audit
```

La commande ne se contente pas de vérifier qu’une ligne existe dans le registre. Elle croise :

- le registre ;
- `enabled` ;
- la politique `public` ;
- le manifest local ;
- le `delivery_status` ;
- le nombre minimal de sources attendu pour le domaine.

Ainsi une URL connue mais jamais livrée reste `PARTIAL` ou `MISSING`, pas `COVERED`.

## Priorités

- **P0** : indispensable avant CI Gold ;
- **P1** : importante pour une base nationale riche ;
- **P2** : complémentaire.

Un domaine P0 `MISSING` ou `UNRESOLVED` est un blocker CI Gold.

## Sources contrôlées

Les microdonnées, corpus linguistiques et contenus soumis à licence ne doivent jamais être forcés pour faire monter artificiellement le taux de couverture. Leur statut doit rester `CONTROLLED` tant que les conditions d’accès/réutilisation ne permettent pas leur ingestion.

## Mise à jour de la matrice

Lorsqu’une nouvelle famille nationale importante est découverte :

1. ajouter le domaine dans `configs/ci_coverage.json` ;
2. attribuer P0/P1/P2 ;
3. identifier les sources officielles ;
4. ajouter les sources au registre ;
5. documenter droits et accès ;
6. configurer le connecteur ;
7. synchroniser ;
8. vérifier `coverage-audit` et `quality-audit`.

La matrice ne doit jamais être réduite uniquement pour atteindre artificiellement le score CI Gold.
