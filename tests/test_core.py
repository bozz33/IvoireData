import csv,json,subprocess,sys

def rows(): return list(csv.DictReader(open('registry/sources.csv',encoding='utf-8')))

def test_registry_is_multisector():
    r=rows(); assert len(r)>=50
    domains={x['domain'] for x in r}
    for d in ['agriculture','education','health','economy','transport','environment_climate','law_justice']:
        assert d in domains

def test_languages_deferred():
    assert not any(x['domain'].startswith('language_') for x in rows())

def test_public_local_ingest_present():
    assert any(x['rights_tier']=='C_PUBLIC_LOCAL_INGEST' for x in rows())

def test_seed_provenance():
    facts=[json.loads(x) for x in open('data/seed/public_facts.jsonl',encoding='utf-8') if x.strip()]
    assert len(facts)>=20
    assert all(x['source_url'].startswith('https://') for x in facts)
