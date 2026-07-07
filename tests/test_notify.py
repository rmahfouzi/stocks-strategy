import pytest

from nrfm.notify import EmailConfigError, load_config


def test_load_config_from_file(tmp_path):
    f = tmp_path / "email.env"
    f.write_text(
        "# gmail\n"
        "NRFM_SMTP_HOST=smtp.gmail.com\n"
        "NRFM_SMTP_PORT=465\n"
        "NRFM_SMTP_USER=me@gmail.com\n"
        "NRFM_SMTP_PASSWORD=abcd efgh ijkl mnop\n"
        "NRFM_EMAIL_TO=me@gmail.com\n"
    )
    cfg = load_config(f)
    assert cfg.port == 465
    assert cfg.password == "abcd efgh ijkl mnop"


def test_env_vars_override_file(tmp_path, monkeypatch):
    f = tmp_path / "email.env"
    f.write_text(
        "NRFM_SMTP_HOST=smtp.gmail.com\nNRFM_SMTP_PORT=465\n"
        "NRFM_SMTP_USER=file@gmail.com\nNRFM_SMTP_PASSWORD=x\n"
        "NRFM_EMAIL_TO=file@gmail.com\n"
    )
    monkeypatch.setenv("NRFM_SMTP_USER", "env@gmail.com")
    assert load_config(f).user == "env@gmail.com"


def test_missing_settings_raise(tmp_path, monkeypatch):
    for k in ("NRFM_SMTP_HOST", "NRFM_SMTP_PORT", "NRFM_SMTP_USER",
              "NRFM_SMTP_PASSWORD", "NRFM_EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(EmailConfigError, match="NRFM_SMTP_HOST"):
        load_config(tmp_path / "does-not-exist.env")
