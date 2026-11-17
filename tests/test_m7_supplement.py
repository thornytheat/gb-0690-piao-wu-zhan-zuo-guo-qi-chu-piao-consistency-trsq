import sqlite3
from app.config import SAMPLES
from app import revision, store

CLEAN = (SAMPLES / "clean.csv").read_text(encoding="utf-8-sig")
HEADER = "id,channel,seat,order,time,type,expires,version,amount,ticket\n"
RELEASE_A13 = HEADER + \
    "R12,官方,A13,00013,2026-11-17T10:37:00+08:00,release,,2,0,\n"
EXPIRY_A01 = HEADER + \
    "00001,官方,A01,00001,2026-11-17T10:00:00+08:00,hold,2026-11-17T10:04:00+08:00,2,0,\n"


def start(client, text=CLEAN):
    reply = client.post("/api/runs", json={"text": text})
    assert reply.status_code == 200, reply.json
    return reply.json


def supplement(client, base_id, text):
    return client.post("/api/supplement/" + base_id, json={"text": text})


def test_supplement_creates_linked_current_version(client):
    base = start(client)
    reply = supplement(client, base["manifest"]["run_id"], RELEASE_A13)
    assert reply.status_code == 200, reply.json
    child = reply.json["batch"]
    assert reply.json["created"] is True
    assert child["base"] == base["manifest"]["run_id"]
    assert child["root"] == base["manifest"]["run_id"]
    assert child["revision"] == base["revision"] + 1
    assert child["current"] is True
    # The original batch is still readable and untouched.
    old = client.get("/api/runs/" + base["manifest"]["run_id"]).json
    assert old["current"] is False
    assert old["revision"] == base["revision"]
    assert old["manifest"]["run_id"] == base["manifest"]["run_id"]


def test_supplement_switches_seats_tickets_and_pending_together(client):
    base = start(client)
    assert len(base["tickets"]) == 12
    child = supplement(client, base["manifest"]["run_id"], EXPIRY_A01).json["batch"]
    seats = {s["seat"]: s["state"] for s in
             __import__("app.snapshot", fromlist=["state"]).state(child)["seats"]}
    assert seats["A-01"] == "已过期"
    assert {t["ticket"] for t in child["tickets"]}.isdisjoint({"00000001"})
    diff = client.get("/api/supplement/" + base["manifest"]["run_id"] + "/diff").json
    assert {"seat": "A-01", "before": "已出票", "after": "已过期"} in diff["seats"]
    assert any(t["ticket"] == "00000001" and t["after"] == "失效" for t in diff["tickets"])


def test_stale_supplement_is_rejected_without_state_change(client):
    base = start(client)
    child = supplement(client, base["manifest"]["run_id"], RELEASE_A13).json["batch"]
    stale = HEADER + "R13,官方,A13,00013,2026-11-17T10:38:00+08:00,release,,2,0,\n"
    reply = supplement(client, base["manifest"]["run_id"], stale)
    assert reply.status_code == 409 and reply.json["code"] == "STALE"
    family = client.get("/api/lineage/" + base["manifest"]["run_id"]).json
    assert len(family["versions"]) == 2
    assert family["current"] == child["manifest"]["run_id"]


def test_retrying_supplement_does_not_add_a_version(client):
    base = start(client)
    first = supplement(client, base["manifest"]["run_id"], RELEASE_A13).json
    again = supplement(client, first["batch"]["manifest"]["run_id"], RELEASE_A13).json
    assert again["created"] is False
    assert again["batch"]["manifest"]["run_id"] == first["batch"]["manifest"]["run_id"]
    family = client.get("/api/lineage/" + base["manifest"]["run_id"]).json
    assert len(family["versions"]) == 2


def test_failed_switch_keeps_original_state(client):
    base = start(client)
    child = supplement(client, base["manifest"]["run_id"], RELEASE_A13).json["batch"]
    db = sqlite3.connect(store._path())
    db.execute("CREATE TRIGGER fail_switch BEFORE UPDATE OF current ON lineages "
               "BEGIN SELECT RAISE(ABORT,'boom'); END")
    db.commit()
    db.close()
    try:
        reply = supplement(client, child["manifest"]["run_id"], EXPIRY_A01)
        assert reply.status_code == 503 and reply.json["code"] == "STORE"
    finally:
        db = sqlite3.connect(store._path())
        db.execute("DROP TRIGGER fail_switch")
        db.commit()
        db.close()
    family = client.get("/api/lineage/" + base["manifest"]["run_id"]).json
    assert family["current"] == child["manifest"]["run_id"]
    assert len(family["versions"]) == 2
    assert client.get("/api/runs/" + child["manifest"]["run_id"]).json["current"] is True


def test_interrupted_lineage_recovers(client):
    base = start(client)
    child = supplement(client, base["manifest"]["run_id"], RELEASE_A13).json["batch"]
    db = sqlite3.connect(store._path())
    db.execute("DELETE FROM lineages")
    db.commit()
    db.close()
    family = client.get("/api/lineage/" + base["manifest"]["run_id"]).json
    assert family["current"] == child["manifest"]["run_id"]
    assert [v["revision"] for v in family["versions"]] == [1, 2]


def test_diff_export_and_pages(client):
    base = start(client)
    child = supplement(client, base["manifest"]["run_id"], EXPIRY_A01).json["batch"]
    url = f"/api/lineage/{base['manifest']['run_id']}/diff.csv?base={base['manifest']['run_id']}" \
          f"&current={child['manifest']['run_id']}"
    export = client.get(url)
    assert export.status_code == 200 and "失效" in export.text
    page = client.get("/m/7?run=" + child["manifest"]["run_id"])
    assert page.status_code == 200
    assert "原版与当前版差异" in page.get_data(as_text=True)
    old_page = client.get("/m/7?run=" + base["manifest"]["run_id"])
    assert "正在回看旧版" in old_page.get_data(as_text=True)


def test_header_mismatch_leaves_no_version(client):
    base = start(client)
    reply = supplement(client, base["manifest"]["run_id"], "id,channel\nX,官方\n")
    assert reply.status_code == 422 and reply.json["code"] == "HEADER"
    family = client.get("/api/lineage/" + base["manifest"]["run_id"]).json
    assert len(family["versions"]) == 1
    assert family["current"] == base["manifest"]["run_id"]


def test_chain_keeps_every_revision_and_only_latest_is_current(client):
    base = start(client)
    v2 = supplement(client, base["manifest"]["run_id"], RELEASE_A13).json["batch"]
    v3 = supplement(client, v2["manifest"]["run_id"], EXPIRY_A01).json["batch"]
    assert v3["revision"] == 3 and v3["base"] == v2["manifest"]["run_id"]
    family = client.get("/api/lineage/" + base["manifest"]["run_id"]).json
    assert [(v["revision"], v["current"]) for v in family["versions"]] == \
        [(1, False), (2, False), (3, True)]
    # Old versions keep their own evidence and are not rewritten by the switch.
    old = client.get("/api/runs/" + base["manifest"]["run_id"]).json
    assert len(old["tickets"]) == 12 and old["quarantine"] == []
    middle = client.get("/api/runs/" + v2["manifest"]["run_id"]).json
    assert len(middle["payments"]) == 13 and len(middle["tickets"]) == 12
    assert client.get("/api/runs/" + v3["manifest"]["run_id"]).json["current"] is True


def test_evidence_export_keeps_original_versions(client):
    base = start(client)
    child = supplement(client, base["manifest"]["run_id"], EXPIRY_A01).json["batch"]
    for run_id in (base["manifest"]["run_id"], child["manifest"]["run_id"]):
        export = client.get("/api/evidence.csv?run=" + run_id)
        assert export.status_code == 200
        assert run_id in export.text


def test_original_payload_is_not_rewritten(client):
    base = start(client)
    before = client.get("/api/runs/" + base["manifest"]["run_id"]).json
    supplement(client, base["manifest"]["run_id"], EXPIRY_A01)
    after = client.get("/api/runs/" + base["manifest"]["run_id"]).json
    for result in (before, after):
        result.pop("current")
        result.pop("current_run")
        result.pop("lineage_size")
    assert before == after


def test_supplement_columns_may_be_reordered(client):
    base = start(client)
    reordered = ("ticket,amount,version,expires,type,time,order,seat,channel,id\n"
                 ",0,2,,release,2026-11-17T10:37:00+08:00,00013,A13,官方,R12\n")
    reply = supplement(client, base["manifest"]["run_id"], reordered)
    assert reply.status_code == 200, reply.json
    released = [l for l in reply.json["batch"]["leases"]
                if l["seat"] == "A-13" and l["lease"] == "H12"]
    assert released and released[0]["released"] is True


def test_malformed_supplement_row_leaves_no_version(client):
    base = start(client)
    reply = supplement(client, base["manifest"]["run_id"], HEADER + "only,官方\n")
    assert reply.status_code == 422 and reply.json["code"] == "COLUMN_COUNT"
    family = client.get("/api/lineage/" + base["manifest"]["run_id"]).json
    assert len(family["versions"]) == 1


def test_legacy_schema_is_migrated_and_supplementable(client):
    base = start(client)
    path = store._path()
    db = sqlite3.connect(path)
    db.execute("UPDATE runs SET content=NULL,root=NULL,revision=NULL")
    db.execute("DELETE FROM lineages")
    db.commit()
    db.close()
    reply = supplement(client, base["manifest"]["run_id"], RELEASE_A13)
    assert reply.status_code == 200, reply.json
    assert reply.json["batch"]["revision"] == 2
    family = client.get("/api/lineage/" + base["manifest"]["run_id"]).json
    assert family["current"] == reply.json["batch"]["manifest"]["run_id"]


def test_duplicate_revisions_are_renumbered_on_recovery(client):
    base = start(client)
    child = supplement(client, base["manifest"]["run_id"], RELEASE_A13).json["batch"]
    db = sqlite3.connect(store._path())
    db.execute("DROP INDEX IF EXISTS runs_revision")
    db.execute("UPDATE runs SET revision=1 WHERE id=?", (child["manifest"]["run_id"],))
    db.commit()
    db.close()
    family = client.get("/api/lineage/" + base["manifest"]["run_id"]).json
    assert [v["revision"] for v in family["versions"]] == [1, 2]
    assert family["current"] == child["manifest"]["run_id"]


def test_diff_endpoints_reject_cross_lineage(client):
    base = start(client)
    other = start(client, CLEAN.replace("00001", "00099"))
    child = supplement(client, base["manifest"]["run_id"], RELEASE_A13).json["batch"]
    reply = client.get(f"/api/lineage/{base['manifest']['run_id']}/diff.csv"
                       f"?base={other['manifest']['run_id']}"
                       f"&current={child['manifest']['run_id']}")
    assert reply.status_code == 422 and reply.json["code"] == "MISMATCH"
