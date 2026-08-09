import importlib.util,pathlib

spec=importlib.util.spec_from_file_location('ingest',pathlib.Path('scripts/ingest_public_web.py'))
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

def test_html_cleanup():
    raw=b'<html><style>SECRET_STYLE</style><script>SECRET_JS</script><body>Bonjour <b>CIV</b></body></html>'
    text=m.html_text(raw)
    assert 'Bonjour CIV' in text
    assert 'SECRET_STYLE' not in text
    assert 'SECRET_JS' not in text
