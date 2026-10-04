from graflex import hoststore


def test_store_record_roundtrips(tmp_path, monkeypatch):
    import graflex
    conn = hoststore.connect(str(tmp_path / "g.db"))
    monkeypatch.setattr(graflex, "_HOSTSTORE", conn, raising=False)
    graflex._store_record("ollama", "1.2.3.4:11434", "working",
                          {"host": "1.2.3.4:11434", "models": ["m"], "checked": "t"})
    assert hoststore.rows(conn, "ollama", "working") == [
        ("1.2.3.4:11434", {"host": "1.2.3.4:11434", "models": ["m"], "checked": "t"}, "t")]


def test_export_writes_files_via_save_fn(tmp_path, monkeypatch):
    import graflex
    conn = hoststore.connect(str(tmp_path / "g.db"))
    monkeypatch.setattr(graflex, "_HOSTSTORE", conn, raising=False)
    graflex._store_record("ollama", "a:1", "working", {"host": "a:1", "checked": "t"})
    saved = {}
    monkeypatch.setattr(graflex, "_save_json_atomic", lambda p, d: saved.__setitem__(p, d))
    monkeypatch.setattr(graflex, "_cache_file", lambda s, x: f"{s}-{x}.json")
    graflex.export()
    assert saved["ollama-working.json"] == [{"host": "a:1", "checked": "t"}]
    assert saved["ollama-notworking.json"] == {}
