# Matrice de couverture Côte d’Ivoire — v2

La source de vérité machine-readable est `configs/ci_coverage.json`.

## Statuts

```text
COVERED      minimum de sources réellement livré
PARTIAL      domaine connu mais couverture/livraison incomplète
CONTROLLED   domaine évalué mais accès/droits contrôlés
UNAVAILABLE  information identifiée mais non publiquement ingérable
UNRESOLVED   source attendue désactivée/non résolue
MISSING      aucune source adéquate enregistrée
```

## Portée v2

La matrice évalue désormais plus de 50 familles nationales, regroupables ainsi :

### État et institutions

- administration ;
- gouvernance / Présidence / Parlement / CESEC ;
- finances publiques / Cour des comptes ;
- marchés publics ;
- droit / justice / constitutionnalité ;
- droit des affaires ;
- fiscalité ;
- élections ;
- anti-corruption ;
- décentralisation ;
- fonction publique ;
- diplomatie ;
- défense / sécurité institutionnelle ;
- protection civile.

### Population et société

- démographie ;
- migration ;
- emploi / travail ;
- protection sociale ;
- pauvreté / vulnérabilité ;
- genre / femmes / famille / enfants ;
- jeunesse ;
- handicap.

### Économie et production

- économie / commerce / banque / finance ;
- industrie ;
- investissement ;
- agriculture ;
- sécurité alimentaire ;
- élevage / pêche / aquaculture ;
- forêts ;
- mines ;
- pétrole / gaz ;
- électricité.

### Services et connaissance

- santé ;
- éducation ;
- enseignement supérieur ;
- recherche ;
- télécoms ;
- numérique ;
- cybersécurité publique ;
- innovation ;
- médias / communication.

### Territoire et environnement

- environnement / climat / météo ;
- biodiversité ;
- géographie / limites administratives ;
- foncier / logement ;
- eau / assainissement ;
- transport ;
- routes.

### Culture et identité

- tourisme ;
- culture / patrimoine ;
- sport ;
- histoire / mémoire / archives ;
- langues ivoiriennes ;
- français ivoirien / nouchi ;
- gastronomie.

## Deux registres complémentaires

```text
registry/sources.csv
registry/ci_gold_completeness.csv
```

Le second ajoute les institutions/familles identifiées lors de la seconde passe CI Gold : Femme/Famille/Enfant, Jeunesse, Commerce/Industrie, CEPICI, Numérique, Intérieur/Décentralisation, ONEF, Fonction publique, HABG, Défense, Assemblée nationale, Sénat, Conseil constitutionnel, Cour des comptes, CESEC, Présidence, Diplomatie, Solidarité/Pauvreté et MIRAH.

## Calcul dynamique

```bash
ivoiredata coverage-audit
```

La commande croise : registre, activation, politique d’accès, manifest local, `delivery_status` et nombre minimal de sources requis. Une URL enregistrée mais jamais livrée ne devient jamais `COVERED`.

## Découverte Data.gouv

```bash
ivoiredata discoveries
```

signale les datasets présents dans le catalogue Data.gouv.ci mais sans mapping explicite. La découverte n’ajoute pas automatiquement une source : domaine et droits doivent être revus avant activation.

## Priorités

- P0 : bloque CI Gold si `MISSING`/`UNRESOLVED` ;
- P1 : couverture nationale importante ;
- P2 : enrichissement complémentaire.

## Familles contrôlées

Langues ivoiriennes et nouchi/français ivoirien peuvent rester `CONTROLLED` si les corpus disponibles ne disposent pas de licences compatibles. La matrice mesure aussi les limites légales ; elle ne doit jamais encourager une collecte illégitime.

## Évolution

Lorsqu’une nouvelle famille importante est identifiée :

1. l’ajouter à `configs/ci_coverage.json` ;
2. identifier les sources officielles ;
3. enregistrer source + droits ;
4. configurer le connecteur ;
5. synchroniser ;
6. auditer ;
7. ne passer `COVERED` qu’après livraison réelle.

La matrice ne doit jamais être réduite artificiellement pour augmenter le score CI Gold.
