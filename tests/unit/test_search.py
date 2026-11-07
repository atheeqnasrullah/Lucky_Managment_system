from datetime import date

from app.ai.fake_provider import FakeAIProvider
from app.models.invoice import InvoiceRecord, Party
from app.search.analytics import aggregate
from app.search.planner import GroupBy, Metric, QueryIntent
from app.search.vector_store import MemoryVectorStore, VectorRecord

ai = FakeAIProvider()
TODAY = date(2026, 11, 7)


def _inv(id: str, vendor: str, total: float, currency: str, d: date) -> InvoiceRecord:
    return InvoiceRecord(id=id, tenant_id="t", vendor=Party(name=vendor), total=total, currency=currency, invoice_date=d)


def test_aggregate_never_mixes_currencies():
    rows = aggregate(
        [
            _inv("1", "A", 100, "USD", date(2026, 1, 1)),
            _inv("2", "B", 50, "USD", date(2026, 2, 1)),
            _inv("3", "C", 1000, "INR", date(2026, 2, 1)),
        ],
        GroupBy.none,
    )
    by_currency = {r.currency: r.total for r in rows}
    assert by_currency == {"USD": 150, "INR": 1000}


def test_aggregate_group_by_month_sorted():
    rows = aggregate(
        [_inv("1", "A", 10, "USD", date(2026, 2, 3)), _inv("2", "A", 5, "USD", date(2026, 1, 9))],
        GroupBy.month,
    )
    assert [r.group for r in rows] == ["2026-01", "2026-02"]


def test_planner_intents():
    p = ai.plan_query("How much did I spend in March 2026?", TODAY, [])
    assert p.intent == QueryIntent.aggregate
    assert (p.date_from, p.date_to) == (date(2026, 3, 1), date(2026, 3, 31))

    p = ai.plan_query("How many invoices from ACME last month?", TODAY, [])
    assert p.intent == QueryIntent.aggregate and p.metric == Metric.count
    assert p.vendor == "ACME"

    p = ai.plan_query("show unpaid invoices", TODAY, [])
    assert p.intent == QueryIntent.list and p.payment_status and p.payment_status.value == "unpaid"

    p = ai.plan_query("find invoice no INV-1001", TODAY, [])
    assert p.intent == QueryIntent.lookup and p.invoice_number == "INV-1001"

    p = ai.plan_query("spend by vendor this year", TODAY, [])
    assert p.group_by == GroupBy.vendor

    assert ai.plan_query("thanks!", TODAY, []).intent == QueryIntent.smalltalk
    assert ai.plan_query("toner cartridges", TODAY, []).intent == QueryIntent.search


def test_memory_vector_store_filters_and_namespaces():
    store = MemoryVectorStore()
    vecs = ai.embed(["printer toner", "office chairs"])
    store.upsert("t1", [VectorRecord("a#0", vecs[0], {"invoice_date": 100}), VectorRecord("b#0", vecs[1], {"invoice_date": 200})])
    store.upsert("t2", [VectorRecord("c#0", vecs[0], {})])

    q = ai.embed(["toner"])[0]
    assert store.query("t1", q, 1)[0].id == "a#0"
    assert [m.id for m in store.query("t1", q, 5, {"invoice_date": {"$gte": 150}})] == ["b#0"]
    assert all(m.id != "c#0" for m in store.query("t1", q, 5))  # tenant isolation

    store.delete_prefix("t1", "a#")
    assert store.count("t1") == 1
