import json

from graflex import hoststore


def test_upsert_inserts_then_updates(tmp_path):
    conn = hoststore.connect(str(tmp_path / "g.db"))
    hoststore.upsert(conn, "ollama", "1.2.3.4:11434", "working",
                     {"host": "1.2.3.4:11434", "models": ["a"]}, "2026-01-01T00:00:00Z")
    row = conn.execute("SELECT service, host, status, payload, checked FROM host").fetchone()
    assert row[0] == "ollama" and row[1] == "1.2.3.4:11434" and row[2] == "working"
    assert json.loads(row[3]) == {"host": "1.2.3.4:11434", "models": ["a"]}
    # update in place, no second row
    hoststore.upsert(conn, "ollama", "1.2.3.4:11434", "notworking",
                     {"host": "1.2.3.4:11434", "reason": "timeout"}, "2026-01-02T00:00:00Z")
    assert conn.execute("SELECT COUNT(*) FROM host").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM host").fetchone()[0] == "notworking"


def test_same_host_two_services_are_two_rows(tmp_path):
    conn = hoststore.connect(str(tmp_path / "g.db"))
    hoststore.upsert(conn, "ollama", "5.6.7.8:80", "working", {"host": "5.6.7.8:80"}, None)
    hoststore.upsert(conn, "vllm", "5.6.7.8:80", "working", {"host": "5.6.7.8:80"}, None)
    assert conn.execute("SELECT COUNT(*) FROM host").fetchone()[0] == 2


def test_concurrent_writers_both_land(tmp_path):
    p = str(tmp_path / "g.db")
    c1, c2 = hoststore.connect(p), hoststore.connect(p)
    hoststore.upsert(c1, "ollama", "a:1", "working", {"host": "a:1"}, None)
    hoststore.upsert(c2, "ollama", "b:2", "working", {"host": "b:2"}, None)  # must not raise 'database is locked'
    assert c1.execute("SELECT COUNT(*) FROM host").fetchone()[0] == 2
