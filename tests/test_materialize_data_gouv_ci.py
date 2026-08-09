import importlib.util
import pathlib

MODULE = pathlib.Path(__file__).parents[1] / "scripts" / "materialize_data_gouv_ci.py"
spec = importlib.util.spec_from_file_location("materialize_data_gouv_ci", MODULE)
m = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(m)


def test_dict_rows_semicolon():
    fields, rows = m.dict_rows(b"annee;valeur\n2024;12\n2025;13\n")
    assert fields == ["annee", "valeur"]
    assert rows == [{"annee": "2024", "valeur": "12"}, {"annee": "2025", "valeur": "13"}]


def test_materialize_one_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "fetch_metadata", lambda api, dsid, seed: {
        "id": dsid,
        "title": "Jeu test",
        "ownerName": "Producteur public",
        "license": "Licence Ouverte",
    })
    monkeypatch.setattr(m, "fetch_full_csv", lambda api, dsid: (
        b"annee,valeur\n2024,12\n",
        f"{api}/datasets/{dsid}/full",
        {"content-type": "text/csv"},
    ))
    item = m.materialize_one("https://example.invalid/api", tmp_path, {"id": "jeu-test"}, parquet=False, delay=0)
    assert item["row_count"] == 1
    assert item["status"] == "ok"
    out = tmp_path / "processed/data_gouv_ci/jsonl/jeu-test.jsonl"
    text = out.read_text(encoding="utf-8")
    assert "__ivoiredata_dataset_id" in text
    assert "Licence Ouverte" in text
