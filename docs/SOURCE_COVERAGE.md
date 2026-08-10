# Couverture multisectorielle — CI Gold v0.8.0

La couverture n’est plus définie par une liste statique de sources ni par `sync_status=SUCCESS`. La source de vérité est maintenant :

```text
configs/ci_coverage.json
        +
manifest.json locaux
        +
audit de livraison
```

Commande :

```bash
ivoiredata coverage-audit
```

## Statuts de couverture

- `COVERED` : minimum de sources attendu réellement livré ;
- `PARTIAL` : domaine connu mais couverture/livraison incomplète ;
- `CONTROLLED` : domaine évalué, accès/droits contrôlés ;
- `UNAVAILABLE` : information identifiée mais non publiquement ingérable ;
- `UNRESOLVED` : source attendue connue mais désactivée/non résolue ;
- `MISSING` : domaine sans source adéquate enregistrée.

## Priorités

- P0 : bloque CI Gold si `MISSING` ou `UNRESOLVED` ;
- P1 : important ;
- P2 : complémentaire.

## Couverture institutionnelle renforcée en v0.8.0

Nouvelles sources enregistrées/configurées : SGG, DGBF, MESRS, CEI, AGEROUTE, ANARE-CI, Culture, Tourisme, Communication, Sports et portail du Gouvernement.

Elles complètent les sources déjà présentes : Data.gouv.ci, ANStat/NADA metadata, FAOSTAT, ILOSTAT, UIS, WDI, World Bank Projects, geoBoundaries, OSM, DGI, OHADA, ministères, ARTCI, Douanes, Trésor, DGMP, CNPS, Service Public, santé, environnement, mines, énergie, foncier, eau/assainissement et SODEXAM.

## Familles nationales

La matrice v1 évalue notamment administration, gouvernance, finances publiques, droit, fiscalité, élections, démographie, emploi, protection sociale, économie, agriculture, sécurité alimentaire, santé, éducation, enseignement supérieur, recherche, télécoms, communication, mines, hydrocarbures, électricité, environnement/climat, géographie, foncier, eau/assainissement, transport, routes, tourisme, culture, sport, histoire, langues, nouchi/français ivoirien et gastronomie.

Voir `CI_COVERAGE_MATRIX.md` pour la lecture détaillée.

## Règle essentielle

La présence d’une source dans le registre signifie seulement : **source identifiée**.

Elle devient `COVERED` uniquement après livraison réelle non vide conforme à la politique de droits et au minimum défini par la matrice.

## Sources contrôlées

Les microdonnées ANStat, certains corpus linguistiques/culturels et toutes les sources D ne doivent pas être forcés. Leur statut `CONTROLLED` est une évaluation correcte ; il ne signifie pas que leurs payloads sont inclus.

## Validation locale v0.8.0

Après migration :

```bash
ivoiredata sync --all-public --force
ivoiredata audit
ivoiredata coverage-audit
ivoiredata quality-audit
ivoiredata ci-gold
```

Le résultat réel dépend du data lake local et de l’état vivant des upstreams. GitHub CI valide le code/contrats, pas les volumes réseau du PC.
