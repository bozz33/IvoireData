from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

import requests

from . import technology_registries as registries


MAVEN_REPOSITORY_BASE = "https://repo1.maven.org/maven2/"
CENTRAL_PORTAL_BASE = "https://central.sonatype.com/artifact/"

# Maven ComparableVersion recognizes alpha/a, beta/b, milestone/m, rc/cr and
# snapshot as qualifiers below a GA/final/release version. Central's
# maven-metadata.xml <release> means the latest non-SNAPSHOT repository release;
# it can therefore still point at alpha/beta/RC/milestone builds. IvoireData's
# latest_stable_version is intentionally stricter and excludes those pre-GA
# qualifiers, plus common early-access aliases used in Maven repositories.
_MAVEN_PRE_GA_QUALIFIERS = {
    "alpha",
    "beta",
    "milestone",
    "rc",
    "cr",
    "snapshot",
    "preview",
    "pre",
    "ea",
    "dev",
    "nightly",
}
_MAVEN_SHORT_PRE_GA = {"a", "b", "m"}


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _child_text(node: ET.Element | None, name: str) -> str | None:
    if node is None:
        return None
    for child in list(node):
        if _local_name(child.tag) == name:
            value = str(child.text or "").strip()
            if value:
                return value
    return None


def _descendant(node: ET.Element | None, name: str) -> ET.Element | None:
    if node is None:
        return None
    for child in node.iter():
        if _local_name(child.tag) == name:
            return child
    return None


def _all_descendant_text(node: ET.Element | None, name: str) -> list[str]:
    if node is None:
        return []
    out: list[str] = []
    for child in node.iter():
        if _local_name(child.tag) == name:
            value = str(child.text or "").strip()
            if value:
                out.append(value)
    return out


def _scm_repository(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for prefix in ("scm:git:", "scm:hg:", "scm:svn:"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    if text.startswith("git@github.com:"):
        text = "https://github.com/" + text.split(":", 1)[1]
    if text.startswith("git@gitlab.com:"):
        text = "https://gitlab.com/" + text.split(":", 1)[1]
    return registries._repo_url(text)


def _maven_version_tokens(value: str) -> list[str]:
    # ComparableVersion treats transitions between digits and letters as
    # separators (for example 1.0alpha1 -> 1, 0, alpha, 1), so tokenizing this
    # way catches both hyphenated and compact qualifiers without assuming SemVer.
    return re.findall(r"[a-z]+|\d+", str(value or "").strip().casefold())


def _is_maven_stable(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    tokens = _maven_version_tokens(text)
    if not tokens:
        return False
    for index, token in enumerate(tokens):
        if token in _MAVEN_PRE_GA_QUALIFIERS:
            return False
        if token in _MAVEN_SHORT_PRE_GA and index + 1 < len(tokens) and tokens[index + 1].isdigit():
            return False
    return True


def _stable_release(metadata_root: ET.Element) -> str | None:
    versioning = _descendant(metadata_root, "versioning")

    # Repository metadata is authoritative, but its <release> can still be a
    # pre-GA Maven release (for example 2.1.0-alpha1). Prefer it only when it
    # satisfies IvoireData's stable policy.
    release = _child_text(versioning, "release")
    if _is_maven_stable(release):
        return release

    # <latest> includes snapshots by definition and may also be pre-GA. It is
    # useful only when it independently passes the same stable policy.
    latest = _child_text(versioning, "latest")
    if _is_maven_stable(latest):
        return latest

    # Fall back to the repository's available-version order and choose the most
    # recently listed version that is stable. This avoids applying PEP 440 or
    # SemVer ordering to Maven coordinates, which would be incorrect for many
    # Maven version strings.
    versions = _descendant(versioning, "versions")
    values = _all_descendant_text(versions, "version")
    for value in reversed(values):
        if _is_maven_stable(value):
            return value
    return None


def maven_package_metadata(
    session: requests.Session,
    name: str,
    user_agent: str,
) -> dict[str, Any]:
    raw = str(name or "").strip()
    if ":" not in raw:
        raise ValueError("Maven package name must be groupId:artifactId")
    group_id, artifact_id = (part.strip() for part in raw.split(":", 1))
    if not group_id or not artifact_id:
        raise ValueError("Maven package name must be groupId:artifactId")

    group_path = "/".join(quote(part, safe="._-") for part in group_id.split("."))
    artifact_path = quote(artifact_id, safe="._-")
    base = f"{MAVEN_REPOSITORY_BASE}{group_path}/{artifact_path}/"
    metadata_url = base + "maven-metadata.xml"
    response = session.get(
        metadata_url,
        headers={"User-Agent": user_agent, "Accept": "application/xml,text/xml"},
        timeout=60,
    )
    response.raise_for_status()
    metadata = ET.fromstring(response.content)
    canonical_group = _child_text(metadata, "groupId") or group_id
    canonical_artifact = _child_text(metadata, "artifactId") or artifact_id
    release = _stable_release(metadata)

    repository = None
    project_url = None
    description = None
    if release:
        encoded_version = quote(release, safe="._+-")
        pom_url = f"{base}{encoded_version}/{artifact_path}-{encoded_version}.pom"
        try:
            pom_response = session.get(
                pom_url,
                headers={"User-Agent": user_agent, "Accept": "application/xml,text/xml"},
                timeout=60,
            )
            pom_response.raise_for_status()
            pom = ET.fromstring(pom_response.content)
            project_url = _child_text(pom, "url")
            description = _child_text(pom, "description")
            scm = _descendant(pom, "scm")
            for key in ("url", "connection", "developerConnection"):
                repository = _scm_repository(_child_text(scm, key))
                if repository:
                    break
        except (requests.RequestException, ET.ParseError):
            pass

    canonical_name = f"{canonical_group}:{canonical_artifact}"
    portal = CENTRAL_PORTAL_BASE + "/".join(
        quote(part, safe="._-") for part in (canonical_group, canonical_artifact)
    )
    if release:
        portal += "/" + quote(release, safe="._+-")
    return {
        "authority_source": "maven",
        "native_registry_url": metadata_url,
        "name": canonical_name,
        "latest_stable_version": release,
        "canonical_repository": repository,
        "documentation_url": portal,
        "official_website": project_url or portal,
        "description": description,
    }


# Register at package import time so Maven authority resolution works through the
# CLI and when IvoireData is imported as a library.
registries._ADAPTERS["repo1.maven.org"] = maven_package_metadata
