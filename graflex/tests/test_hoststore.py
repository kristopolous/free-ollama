import json
import os

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


def test_rows_and_services(tmp_path):
    conn = hoststore.connect(str(tmp_path / "g.db"))
    hoststore.upsert(conn, "ollama", "a:1", "working", {"host": "a:1"}, "t1")
    hoststore.upsert(conn, "ollama", "b:2", "notworking", {"host": "b:2"}, "t2")
    hoststore.upsert(conn, "ollama", "c:3", None, {"service": "ollama", "host": "c:3"}, None)
    hoststore.upsert(conn, "vllm", "d:4", "working", {"host": "d:4"}, "t3")
    assert hoststore.services(conn) == ["ollama", "vllm"]
    assert hoststore.rows(conn, "ollama", "working") == [("a:1", {"host": "a:1"}, "t1")]
    assert {h for h, _, _ in hoststore.rows(conn, "ollama")} == {"a:1", "b:2", "c:3"}   # _UNSET => all
    assert [h for h, _, _ in hoststore.rows(conn, "ollama", None)] == ["c:3"]           # NULL only


def test_export_shapes_match_current_files(tmp_path):
    conn = hoststore.connect(str(tmp_path / "g.db"))
    hoststore.upsert(conn, "ollama", "b:2", "working", {"host": "b:2", "models": ["m"]}, "2026-01-02")
    hoststore.upsert(conn, "ollama", "a:1", "working", {"host": "a:1", "models": []}, "2026-01-01")
    hoststore.upsert(conn, "ollama", "x:9", "notworking", {"host": "x:9", "reason": "timeout"}, "2026-01-03")
    hoststore.upsert(conn, "ollama", "u:0", None, {"service": "ollama", "host": "u:0"}, None)

    written = {}
    hoststore.export_files(conn,
                           lambda path, data: written.__setitem__(os.path.basename(path), data),
                           lambda svc, suffix: f"/x/{svc}-{suffix}.json")
    # working = LIST, sorted by (checked, host)
    assert written["ollama-working.json"] == [{"host": "a:1", "models": []}, {"host": "b:2", "models": ["m"]}]
    # notworking = DICT keyed by host
    assert written["ollama-notworking.json"] == {"x:9": {"host": "x:9", "reason": "timeout"}}
    # hosts = LIST of {service, host}, every row for the service
    assert sorted(written["ollama-hosts.json"], key=lambda e: e["host"]) == [
        {"service": "ollama", "host": "a:1"}, {"service": "ollama", "host": "b:2"},
        {"service": "ollama", "host": "u:0"}, {"service": "ollama", "host": "x:9"}]


def test_export_emits_full_set_so_guard_sees_true_count(tmp_path):
    # Export must hand save_fn the FULL current set (so the real _save_json_atomic LOBOTOMY
    # guard compares against the true count, not a partial), never a subset.
    conn = hoststore.connect(str(tmp_path / "g.db"))
    for i in range(10):
        hoststore.upsert(conn, "ollama", f"h:{i}", "working", {"host": f"h:{i}"}, "t")
    seen = {}
    hoststore.export_files(conn,
                           lambda p, d: seen.__setitem__(p.rsplit("/", 1)[-1], len(d)),
                           lambda s, x: f"{s}-{x}.json")
    assert seen["ollama-working.json"] == 10


def _entry_host(e):
    return e.get("host") or e.get("url", "").split("://")[-1].rstrip("/")


def _load(p, silent=False):
    with open(p) as f:
        return json.load(f)


def test_import_order_preserves_status_and_two_services(tmp_path):
    d = tmp_path
    (d / "ollama-working.json").write_text(json.dumps([{"host": "a:1", "models": ["m"], "checked": "t1"}]))
    (d / "ollama-notworking.json").write_text(json.dumps({"b:2": {"host": "b:2", "reason": "timeout", "checked": "t2"}}))
    # a:1 ALSO appears in hosts (discovery) — must NOT downgrade it to NULL
    (d / "ollama-hosts.json").write_text(json.dumps([{"service": "ollama", "host": "a:1"}, {"service": "ollama", "host": "c:3"}]))
    # same host:port under a second service
    (d / "vllm-working.json").write_text(json.dumps([{"host": "a:1", "models": ["v"], "checked": "t3"}]))

    conn = hoststore.connect(str(d / "g.db"))
    hoststore.import_files(conn, str(d), _load, _entry_host)

    got = dict(conn.execute("SELECT host, status FROM host WHERE service='ollama'").fetchall())
    assert got == {"a:1": "working", "b:2": "notworking", "c:3": None}
    assert conn.execute("SELECT status FROM host WHERE service='vllm' AND host='a:1'").fetchone()[0] == "working"
