from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .models import SourceSpec

COUNTRY_CODE = "CIV"
COUNTRY_NAME = "Côte d'Ivoire"

_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "health": ("santé", "sanitaire", "hôpital", "hopital", "médec", "maladie", "vaccin", "mortalité"),
    "education": ("éducation", "education", "école", "ecole", "élève", "eleve", "bac", "scolaire", "enseignant"),
    "higher_education": ("université", "universite", "enseignement supérieur", "étudiant", "etudiant", "master", "doctorat"),
    "research": ("recherche", "laboratoire", "scientifique", "publication scientifique"),
    "agriculture": ("agric", "cacao", "café", "cafe", "anacarde", "hévéa", "hevea", "vivrier"),
    "livestock_fisheries": ("élevage", "elevage", "bétail", "betail", "pêche", "peche", "aquaculture", "halieutique", "vétérinaire", "veterinaire"),
    "food_security": ("sécurité alimentaire", "securite alimentaire", "insécurité alimentaire", "insecurite alimentaire", "nutrition"),
    "economy": ("économie", "economie", "pib", "croissance", "commerce", "import", "export", "entreprise"),
    "industry": ("industrie", "industriel", "manufactur", "transformation locale", "zone industrielle"),
    "investment": ("investissement", "investisseur", "agrément investissement", "agrement investissement", "cepici"),
    "public_finance": ("budget", "loi de finances", "dépense publique", "depense publique", "recette publique", "dgbf", "cour des comptes"),
    "taxation": ("impôt", "impot", "fiscal", "tva", "taxe", "fne", "dgi"),
    "labor": ("emploi", "travail", "chômage", "chomage", "salaire", "main-d'œuvre", "main d'oeuvre", "marché du travail", "marche du travail"),
    "social_protection": ("cnps", "protection sociale", "retraite", "allocation", "cotisation sociale", "cmu"),
    "poverty": ("pauvreté", "pauvrete", "vulnérable", "vulnerable", "filets sociaux", "solidarité", "solidarite"),
    "gender": ("genre", "égalité femme", "egalite femme", "femme", "filles", "violence basée sur le genre", "vbg"),
    "youth": ("jeunesse", "jeunes", "insertion des jeunes", "service civique"),
    "disability": ("handicap", "personne handicapée", "personne handicapee", "inclusion handicap"),
    "demography": ("population", "recensement", "démograph", "demograph", "ménage", "menage", "naissance"),
    "migration": ("migration", "migrant", "immigration", "émigration", "emigration", "ivoiriens de l'extérieur", "diaspora"),
    "governance": ("gouvernement", "gouvernance", "institution", "conseil des ministres", "administration publique"),
    "decentralization": ("décentralisation", "decentralisation", "collectivité territoriale", "collectivite territoriale", "conseil régional", "conseil regional"),
    "civil_service": ("fonction publique", "fonctionnaire", "agent de l'état", "agent de l'etat", "sigfae", "concours administratif"),
    "anti_corruption": ("corruption", "bonne gouvernance", "habg", "intégrité", "integrite", "déclaration de patrimoine", "declaration de patrimoine"),
    "elections": ("élection", "election", "électoral", "electoral", "scrutin", "candidat", "bureau de vote"),
    "law_justice": ("justice", "juridique", "loi", "décret", "decret", "ordonnance", "arrêté", "arrete", "constitution"),
    "business_law": ("ohada", "acte uniforme", "droit des affaires", "société commerciale", "societe commerciale"),
    "public_procurement": ("marché public", "marche public", "appel d'offres", "appel offres", "dgmp"),
    "diplomacy": ("diplomatie", "affaires étrangères", "affaires etrangeres", "ambassade", "consulat", "coopération internationale", "cooperation internationale"),
    "defense_security": ("défense", "defense", "forces armées", "forces armees", "armée", "armee", "gendarmerie", "sécurité nationale", "securite nationale"),
    "civil_protection": ("protection civile", "pompiers", "catastrophe", "secours", "inondation", "gestion des risques"),
    "environment_climate": ("environnement", "climat", "météo", "meteo", "pluie", "émission", "emission", "foret", "forêt"),
    "biodiversity": ("biodiversité", "biodiversite", "parc national", "réserve naturelle", "reserve naturelle", "espèce", "espece", "aire protégée", "aire protegee"),
    "extractives_energy": ("mine", "minier", "pétrole", "petrole", "gaz", "énergie", "energie"),
    "electricity": ("électricité", "electricite", "réseau électrique", "reseau electrique", "centrale", "anare", "distribution"),
    "geography": ("géographie", "geographie", "région", "region", "district", "commune", "département", "departement", "localité", "localite"),
    "land_housing": ("foncier", "cadastre", "logement", "urbanisme", "construction", "permis de construire"),
    "water_sanitation": ("eau potable", "assainissement", "onad", "onep", "hydraulique"),
    "transport": ("transport", "mobilité", "mobilite", "véhicule", "vehicule"),
    "roads": ("route", "routier", "chaussée", "chaussee", "ageroute", "pont", "échangeur", "echangeur"),
    "ports": ("port autonome", "portuaire", "trafic portuaire", "navire", "terminal à conteneurs", "terminal a conteneurs"),
    "aviation": ("aviation civile", "aéroport", "aeroport", "trafic aérien", "trafic aerien", "anac"),
    "telecom": ("télécom", "telecom", "téléphonie", "telephonie", "artci"),
    "digital": ("numérique", "numerique", "transformation digitale", "digital", "services numériques", "services numeriques"),
    "cybersecurity_public_policy": ("cybersécurité", "cybersecurite", "sécurité numérique", "securite numerique", "cybercriminalité", "cybercriminalite"),
    "innovation": ("innovation", "startup", "start-up", "technologie", "incubateur", "entrepreneuriat numérique", "entrepreneuriat numerique"),
    "media_communication": ("média", "media", "presse", "audiovisuel", "communication", "télévision", "television"),
    "culture": ("culture", "patrimoine", "musée", "musee", "tradition", "artist", "cinéma", "cinema", "littérature", "litterature"),
    "tourism_culture": ("tourisme", "touristique", "hôtel", "hotel", "loisir", "destination"),
    "history": ("histoire", "historique", "archives", "mémoire nationale", "memoire nationale", "indépendance", "independance"),
    "sports": ("sport", "athl", "football", "oissu", "compétition", "competition"),
}

_DOCUMENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DECREE", ("décret", "decret")),
    ("ORDINANCE", ("ordonnance",)),
    ("REGULATION", ("arrêté", "arrete", "règlement", "reglement")),
    ("LAW", (" loi ", "loi n°", "loi no", "constitution")),
    ("BUDGET", ("budget", "loi de finances", "dpbep", "dppd", "pap")),
    ("STATISTICAL_REPORT", ("statistique", "annuaire", "tableau de bord", "chiffres clés", "chiffres cles")),
    ("REPORT", ("rapport", "bilan", "bulletin")),
    ("STRATEGY", ("stratégie", "strategie", "politique nationale")),
    ("PLAN", ("plan national", "pnd", "plan d'action", "plan action")),
    ("GUIDE", ("guide", "manuel")),
    ("PROCEDURE", ("procédure", "procedure", "démarche", "demarche")),
    ("FORM", ("formulaire", "fiche à remplir", "fiche a remplir")),
    ("PRESS_RELEASE", ("communiqué", "communique", "communiqué de presse")),
    ("DIRECTORY", ("annuaire", "répertoire", "repertoire", "liste des")),
    ("MAP", ("carte", "géospatial", "geospatial")),
    ("RESEARCH", ("recherche", "étude", "etude", "article scientifique")),
)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").lower()).strip()


def _scores(text: str) -> list[tuple[str, int]]:
    lowered = _norm(text)
    values: list[tuple[str, int]] = []
    for domain, words in _DOMAIN_KEYWORDS.items():
        score = sum(lowered.count(word) for word in words)
        if score:
            values.append((domain, score))
    return sorted(values, key=lambda row: (-row[1], row[0]))


def infer_domains(source_domain: str, text: str) -> tuple[str, list[str], float]:
    if source_domain and source_domain != "multidomain":
        matches = [d for d, _ in _scores(text) if d != source_domain][:3]
        return source_domain, matches, 1.0
    matches = _scores(text)
    if not matches:
        return "multidomain", [], 0.50
    primary, score = matches[0]
    secondary = [domain for domain, _ in matches[1:4]]
    confidence = min(0.98, 0.65 + min(score, 6) * 0.05)
    return primary, secondary, confidence


def infer_document_type(url: str, text: str, default: str = "OTHER") -> str:
    haystack = f" {_norm(url)} {_norm(text[:12000])} "
    for name, words in _DOCUMENT_RULES:
        if any(word in haystack for word in words):
            return name
    if any(token in _norm(url) for token in ("dataset", "data-fair", ".csv", ".xlsx", ".json")):
        return "DATASET"
    return default or "OTHER"


def source_metadata(spec: SourceSpec) -> dict[str, Any]:
    options = spec.options or {}
    primary = str(options.get("primary_domain") or spec.domain or "unknown")
    secondary = options.get("secondary_domains") or []
    if not isinstance(secondary, list):
        secondary = [str(secondary)]
    confidence = 1.0 if primary != "multidomain" else 0.50
    payload: dict[str, Any] = {
        "country_code": str(options.get("country_code") or COUNTRY_CODE),
        "country_name": str(options.get("country_name") or COUNTRY_NAME),
        "source_id": spec.source_id,
        "provider": spec.provider,
        "source_domain": spec.domain,
        "primary_domain": primary,
        "secondary_domains_json": json.dumps([str(x) for x in secondary], ensure_ascii=False),
        "language": str(options.get("language") or "fr"),
        "geographic_scope": str(options.get("geographic_scope") or "NATIONAL"),
        "document_type": str(options.get("document_type") or "OTHER"),
        "rights_tier": spec.rights_tier,
        "access_tier": spec.access_tier,
        "classification_status": "CONFIGURED" if primary != "multidomain" else "PARTIAL",
        "classification_confidence": confidence,
    }
    if spec.connector == "official_docs":
        for key in (
            "source_strategy", "public_docs_url", "version_repository", "version_ref_strategy",
            "canonical_repository", "version_policy", "doc_version", "programming_language",
            "framework", "runtime", "library", "tool", "ecosystem", "corpus_scope",
        ):
            if options.get(key) is not None:
                payload[key] = options.get(key)
    return payload


def classify_from_base(base: dict[str, Any], url: str, text: str, *, document_type: str | None = None) -> dict[str, Any]:
    out = dict(base or {})
    source_domain = str(out.get("source_domain") or out.get("primary_domain") or "multidomain")
    primary, secondary, confidence = infer_domains(source_domain, text)
    out["primary_domain"] = primary
    out["secondary_domains_json"] = json.dumps(secondary, ensure_ascii=False)
    out["document_type"] = document_type or infer_document_type(url, text, str(out.get("document_type") or "OTHER"))
    out["classification_status"] = "CLASSIFIED" if primary != "multidomain" else "PARTIAL"
    out["classification_confidence"] = confidence
    out["retrieved_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return out


def title_from_text(text: str, *, max_length: int = 300) -> str | None:
    for line in (text or "").splitlines():
        value = re.sub(r"\s+", " ", line).strip(" -|\t")
        if len(value) >= 4:
            return value[:max_length]
    return None
