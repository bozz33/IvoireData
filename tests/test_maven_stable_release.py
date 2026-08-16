from __future__ import annotations

from ivoiredata.technology_maven_authority import (
    _is_maven_stable,
    maven_package_metadata,
)


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.text = content.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.routes[url]


def test_maven_stable_policy_rejects_pre_ga_qualifiers():
    for version in (
        "2.1.0-alpha1",
        "3.0.0-beta3",
        "1.2.0-RC1",
        "1.2.0-CR2",
        "1.2.0-M1",
        "1.0alpha1",
        "4.0.0-SNAPSHOT",
        "5.0.0-preview2",
        "5.0.0-ea1",
    ):
        assert _is_maven_stable(version) is False, version

    for version in (
        "2.0.17",
        "2.22.2",
        "1.0.0.Final",
        "1.0.0-GA",
        "1.0.0-sp1",
        "2026.08.16",
    ):
        assert _is_maven_stable(version) is True, version


def test_maven_authority_falls_back_from_prerelease_release_to_last_stable_version():
    name = "org.slf4j:slf4j-api"
    metadata_url = "https://repo1.maven.org/maven2/org/slf4j/slf4j-api/maven-metadata.xml"
    stable_pom_url = "https://repo1.maven.org/maven2/org/slf4j/slf4j-api/2.0.17/slf4j-api-2.0.17.pom"
    prerelease_pom_url = "https://repo1.maven.org/maven2/org/slf4j/slf4j-api/2.1.0-alpha1/slf4j-api-2.1.0-alpha1.pom"

    metadata = b"""
    <metadata>
      <groupId>org.slf4j</groupId>
      <artifactId>slf4j-api</artifactId>
      <versioning>
        <latest>2.1.0-alpha1</latest>
        <release>2.1.0-alpha1</release>
        <versions>
          <version>2.0.16</version>
          <version>2.0.17</version>
          <version>2.1.0-alpha0</version>
          <version>2.1.0-alpha1</version>
        </versions>
      </versioning>
    </metadata>
    """
    pom = b"""
    <project xmlns='http://maven.apache.org/POM/4.0.0'>
      <url>https://www.slf4j.org</url>
      <scm><url>https://github.com/qos-ch/slf4j</url></scm>
    </project>
    """

    session = FakeSession(
        {
            metadata_url: FakeResponse(metadata),
            stable_pom_url: FakeResponse(pom),
        }
    )
    result = maven_package_metadata(session, name, "test")

    assert result["authority_source"] == "maven"
    assert result["latest_stable_version"] == "2.0.17"
    assert result["canonical_repository"] == "https://github.com/qos-ch/slf4j"
    assert stable_pom_url in [url for url, _ in session.calls]
    assert prerelease_pom_url not in [url for url, _ in session.calls]


def test_maven_authority_rejects_beta_release_and_uses_previous_ga():
    name = "org.apache.logging.log4j:log4j-core"
    metadata_url = "https://repo1.maven.org/maven2/org/apache/logging/log4j/log4j-core/maven-metadata.xml"
    pom_url = "https://repo1.maven.org/maven2/org/apache/logging/log4j/log4j-core/2.25.3/log4j-core-2.25.3.pom"
    metadata = b"""
    <metadata>
      <groupId>org.apache.logging.log4j</groupId>
      <artifactId>log4j-core</artifactId>
      <versioning>
        <latest>3.0.0-beta3</latest>
        <release>3.0.0-beta3</release>
        <versions>
          <version>2.25.2</version>
          <version>2.25.3</version>
          <version>3.0.0-beta2</version>
          <version>3.0.0-beta3</version>
        </versions>
      </versioning>
    </metadata>
    """
    pom = b"""
    <project xmlns='http://maven.apache.org/POM/4.0.0'>
      <scm><url>https://github.com/apache/logging-log4j2</url></scm>
    </project>
    """
    session = FakeSession({metadata_url: FakeResponse(metadata), pom_url: FakeResponse(pom)})
    result = maven_package_metadata(session, name, "test")
    assert result["latest_stable_version"] == "2.25.3"
    assert result["canonical_repository"] == "https://github.com/apache/logging-log4j2"
