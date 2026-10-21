import pytest

import settings_store


def test_write_keeps_other_lines_and_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_store.config, "DATA_DIR", tmp_path)
    env = tmp_path / ".env"
    env.write_text("# 주석\nORACLE_USER=old\nORACLE_PASSWORD=secret\n# ORACLE_DSN=예시\nOTHER=keep\n", encoding="utf-8")
    r = settings_store.write_settings({"ORACLE_USER": "app", "ORACLE_PASSWORD": "", "ORACLE_DSN": "10.0.0.1:1521/ORCL",
                                       "NOT_ALLOWED": "x"})
    assert set(r["changed"]) == {"ORACLE_USER", "ORACLE_DSN"} and r["restart_required"]
    text = env.read_text(encoding="utf-8")
    assert "ORACLE_PASSWORD=secret" in text and "OTHER=keep" in text and "# 주석" in text
    assert "ORACLE_DSN=10.0.0.1:1521/ORCL" in text and "NOT_ALLOWED" not in text
    s = settings_store.read_settings()
    assert s["values"]["ORACLE_USER"] == "app"
    assert s["values"]["ORACLE_PASSWORD"] == "" and s["has_secret"]["ORACLE_PASSWORD"] is True   # 비밀번호는 내보내지 않음


def test_reject_newline(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_store.config, "DATA_DIR", tmp_path)
    with pytest.raises(ValueError):
        settings_store.write_settings({"ORACLE_DSN": "a\nORACLE_USER=x"})
