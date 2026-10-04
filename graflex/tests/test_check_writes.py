from graflex import hoststore


def test_store_record_roundtrips(tmp_path, monkeypatch):
    import graflex
    conn = hoststore.connect(str(tmp_path / "g.db"))
    monkeypatch.setattr(graflex, "_HOSTSTORE", conn, raising=False)
    graflex._store_record("ollama", "1.2.3.4:11434", "working",
                          {"host": "1.2.3.4:11434", "models": ["m"], "checked": "t"})
    assert hoststore.rows(conn, "ollama", "working") == [
        ("1.2.3.4:11434", {"host": "1.2.3.4:11434", "models": ["m"], "checked": "t"}, "t")]
