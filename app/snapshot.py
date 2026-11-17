from decimal import Decimal as D


def _amount(value):
    try:
        return D(str(value))
    except (ArithmeticError, ValueError):
        return D(0)


def state(result):
    """Materialize the three switchable outputs of one batch.

    Seat eligibility, ticket validity and channel pending orders are all
    derived here from the same replay result, so a version switch can move all
    three together in one transaction.
    """
    tickets = list(result.get("tickets", []))
    payments = list(result.get("payments", []))
    leases = list(result.get("leases", []))
    valid_tickets = {}
    for ticket in tickets:
        valid_tickets[ticket["ticket"]] = {
            "ticket": ticket["ticket"], "seat": ticket["seat"], "order": ticket["order"],
            "lease": ticket.get("lease"), "amount": ticket.get("amount"), "valid": True}
    ticketed_leases = {t.get("lease") for t in tickets}
    pending = []
    for payment in payments:
        if payment.get("lease") not in ticketed_leases:
            pending.append({"order": payment["order"], "seat": payment["seat"],
                            "channel": payment["channel"], "lease": payment.get("lease"),
                            "amount": payment.get("amount"), "state": "悬单"})
    seat_states = []
    for number in range(1, 17):
        seat = f"A-{number:02d}"
        if any(t["seat"] == seat for t in valid_tickets.values()):
            name = "已出票"
        elif any(p["seat"] == seat for p in pending):
            name = "待出票"
        else:
            current = None
            for lease in leases:
                if lease["seat"] == seat:
                    current = lease
            if current is None:
                name = "空闲"
            else:
                name = {"占用中": "占用中", "已释放": "已释放"}.get(current.get("state"), "已过期")
        seat_states.append({"seat": seat, "state": name})
    channels = []
    for balance in result.get("channel_balances", []):
        channels.append({
            "channel": balance["channel"],
            "paid": str(_amount(balance.get("paid"))),
            "ticketed": str(_amount(balance.get("ticketed"))),
            "delta": str(_amount(balance.get("delta"))),
            "payments": int(balance.get("payments", 0)),
            "tickets": int(balance.get("tickets", 0)),
            "pending": sum(1 for p in pending if p["channel"] == balance["channel"]),
            "state": balance.get("state")})
    return {"seats": seat_states, "tickets": list(valid_tickets.values()),
            "channels": channels, "pending": pending}
