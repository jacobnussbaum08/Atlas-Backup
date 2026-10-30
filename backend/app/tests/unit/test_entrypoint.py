from __future__ import annotations

from app.__main__ import main


def test_entrypoint_refuses_to_start_with_invalid_configuration(
    clean_env, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    clean_env.setenv("ATLAS_ENVIRONMENT", "live")

    assert main() == 2
    err = capsys.readouterr().err
    assert "Invalid Atlas configuration" in err
    assert "environment" in err
