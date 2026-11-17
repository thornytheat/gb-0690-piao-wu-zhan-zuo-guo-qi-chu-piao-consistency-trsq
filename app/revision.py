from app import store, snapshot
from app.config import MAX_BYTES
from app.errors import DomainError
from app.manifest import make
from app.pipeline import run
from app.supplement import canonical, merge, rebuild


def compare(original, current):
    """Read-only parent/child diff for page and export verification."""
    before = snapshot.state(original)
    after = snapshot.state(current)
    seats = []
    prior = {s["seat"]: s["state"] for s in before["seats"]}
    for seat in after["seats"]:
        old = prior.get(seat["seat"])
        if old != seat["state"]:
            seats.append({"seat": seat["seat"], "before": old, "after": seat["state"]})
    tickets = []
    old_tickets = {t["ticket"]: t for t in before["tickets"]}
    new_tickets = {t["ticket"]: t for t in after["tickets"]}
    for ticket in sorted(set(old_tickets) | set(new_tickets)):
        left, right = old_tickets.get(ticket), new_tickets.get(ticket)
        if (left is None) != (right is None) or left != right:
            tickets.append({"ticket": ticket, "order": (right or left)["order"],
                            "seat": (right or left)["seat"],
                            "before": "有效" if left else "无",
                            "after": "有效" if right else "失效"})
    channels = []
    old_channels = {c["channel"]: c for c in before["channels"]}
    new_channels = {c["channel"]: c for c in after["channels"]}
    for name in sorted(set(old_channels) | set(new_channels)):
        left, right = old_channels.get(name), new_channels.get(name)
        old_pending = sum(1 for p in before["pending"] if p["channel"] == name)
        new_pending = sum(1 for p in after["pending"] if p["channel"] == name)
        old_delta = left["delta"] if left else "0"
        new_delta = right["delta"] if right else "0"
        if old_pending != new_pending or old_delta != new_delta:
            channels.append({"channel": name,
                             "before_pending": old_pending, "after_pending": new_pending,
                             "before_delta": old_delta, "after_delta": new_delta})
    return {"seats": seats, "tickets": tickets, "channels": channels,
            "before_revision": original["revision"], "after_revision": current["revision"],
            "before_run": original["manifest"]["run_id"],
            "after_run": current["manifest"]["run_id"],
            "before_quarantine": len(original["quarantine"]),
            "after_quarantine": len(current["quarantine"]),
            "before_records": len(original["records"]),
            "after_records": len(current["records"])}


def validate(result):
    """Result-level invariants that must hold before we switch a lineage."""
    if len(result["records"]) + len(result["quarantine"]) != result["input_count"]:
        raise DomainError("INCOMPLETE", "渠道行数不守恒")
    state = snapshot.state(result)
    if len(state["seats"]) != 16 or len({s["seat"] for s in state["seats"]}) != 16:
        raise DomainError("INCOMPLETE", "席位状态不完整")
    if len({t["ticket"] for t in state["tickets"]}) != len(state["tickets"]):
        raise DomainError("CONFLICT", "票据状态不唯一")
    return state


def upload(base_id, supplement_raw, params):
    """Replay base input + supplement and commit it as the next revision.

    Idempotent: the same supplement against the same lineage resolves to the
    same batch id, which save_supplement returns without adding a revision --
    even when the base has since advanced to the version that supplement
    produced.  Any failure happens before a row is written, so the lineage
    keeps its current state.
    """
    base, base_text = store.fetch_stored(base_id)
    if not isinstance(params, (dict, type(None))):
        raise DomainError("PARAMS", "参数必须是对象")
    actual = {**base["manifest"]["params"], **(params or {})}
    combined, supplement_text = merge(base_text or rebuild(base), supplement_raw)
    identity = canonical(supplement_text)
    if len(combined.encode("utf-8")) > MAX_BYTES:
        raise DomainError("TOO_LARGE", "合并后的输入超过4MB", 413)
    root = base["root"]
    # Identity is lineage root + supplement payload + params, so a retried
    # supplement maps to the existing batch regardless of which revision it
    # was based on.
    provisional = make(combined.encode("utf-8"), actual, base=base_id, root=root,
                       supplement=identity)
    existing = store.optional(provisional["run_id"])
    if existing is not None:
        return {"batch": existing, "created": False}
    if not base.get("current"):
        raise DomainError("STALE", "原批次已不是当前版本，请基于当前版本补传", 409)
    result = run(combined, actual, persist=False, base=base_id, root=root,
                 content=combined, supplement=identity)
    validate(result)
    saved = store.save_supplement(result, base_id, combined, supplement_text)
    return {"batch": saved, "created": True}
