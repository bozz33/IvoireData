from ivoiredata.cli import parser
from ivoiredata.settings import Settings


def test_settings_are_local_only(tmp_path, monkeypatch):
    data_dir = tmp_path / "lake"
    monkeypatch.setenv("IVOIREDATA_DATA_DIR", str(data_dir))
    settings = Settings.from_env()
    assert settings.data_dir == data_dir
    assert not hasattr(settings, "endpoint_url")
    assert not hasattr(settings, "access_key_id")
    settings.configure_dlt_env()
    assert data_dir.exists()


def test_cli_exposes_local_scheduler_and_status():
    assert parser().parse_args(["scheduler", "--once"]).command == "scheduler"
    assert parser().parse_args(["status", "--public"]).command == "status"
