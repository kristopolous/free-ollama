import json

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


def test_export_single_service_leaves_others_untouched(tmp_path, monkeypatch):
    # The periodic flush during a one-service sweep must regenerate ONLY that service's
    # files — not re-serialize every service's whole file (notworking can be 32MB).
    import graflex
    conn = hoststore.connect(str(tmp_path / "g.db"))
    monkeypatch.setattr(graflex, "_HOSTSTORE", conn, raising=False)
    graflex._store_record("ollama", "a:1", "working", {"host": "a:1", "checked": "t"})
    graflex._store_record("vllm", "b:2", "working", {"host": "b:2", "checked": "t"})
    saved = {}
    monkeypatch.setattr(graflex, "_save_json_atomic", lambda p, d: saved.__setitem__(p, d))
    monkeypatch.setattr(graflex, "_cache_file", lambda s, x: f"{s}-{x}.json")
    graflex.export("ollama")
    assert "ollama-working.json" in saved
    assert not any(k.startswith("vllm-") for k in saved)   # vllm's big files not rewritten


def test_check_hosts_runs_import_before_writing(tmp_path, monkeypatch):
    # The fetch-check path reaches _check_hosts WITHOUT going through _check_all, so the
    # one-time import must trigger here too — otherwise the first probe write makes the DB
    # non-empty and the migration is skipped forever (then export can't refill the files).
    import asyncio
    import graflex
    (tmp_path / "ollama-working.json").write_text(json.dumps([{"host": "legacy:1", "checked": "t"}]))
    monkeypatch.setattr(graflex, "CACHE_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(graflex, "_HOSTSTORE", None, raising=False)
    # empty to_check -> no network; the import must still have run at the top of _check_hosts
    asyncio.run(graflex._check_hosts([], "ollama", str(tmp_path / "ollama-working.json"),
                                     str(tmp_path / "ollama-notworking.json"), 1, 1, []))
    conn = graflex._store()
    assert "legacy:1" in {h for h, _, _ in hoststore.rows(conn, "ollama")}
