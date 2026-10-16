"""Q15 — The Ledger Agent.

Plain-English question -> structured query (rule-based parser) -> exact computation.
No LLM involved, so answers are deterministic and fast.

Data rules (from the API's own notes):
  * Business dates use Asia/Kolkata (IST, UTC+05:30).
  * An order id can appear more than once; the row with the latest updated_at is current.
  * Only status 'paid' counts as revenue.
  * Amounts are in USD/EUR/INR; convert with /rates (usd_per_unit).
"""
import json
import re
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))

API = ("https://exam.sanand.workers.dev/questionData?email=23f2001984%40ds.study.iitm.ac.in"
       "&quizSign=kTlIf7q%2Fg2uVUEGHatOCoXzhU1YBwUUAcx7PQQOWNggdQQIXUKCd60Y%2B%2F5cqSfBf1JX1wPgD3gYFG0tKJ5EjTnl1l6VNo3HQpJzMu4pyx2f4xDj2rJxum0%2FtiOygR1CZ0WLKApzDzl53zK%2F563KOSsQhYuRcWzzTzaVeqoHr%2FiVyEyUDblNaqBZWXWwb%2Be0axNsOfJI6tvDHX5PEPszPPjuYEfBktYZo5nlI4pRvkwoskeu3A3Gm3DaILME%2BpoaaMu9F9HCVYx89QEbViajVNisMLHBeCfZFhgi5jSiRp0KssDTdjpDD0ZzsqPcIdPjGa9juOV3VH%2FlBmWDWa7Q7fQ%3D%3D"
       "&questionId=q-ledger-agent-server&path=")

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9


# ------------------------------------------------------------------ data
def _fetch(path: str, timeout: float = 4.0) -> str:
    req = urllib.request.Request(API + path, headers={"User-Agent": "curl/8.5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


_CACHE = {"at": 0.0, "data": None}


def load_data(max_age: float = 600.0):
    """Live /export + /rates (cached 10 min); falls back to the bundled snapshot."""
    if _CACHE["data"] and time.time() - _CACHE["at"] < max_age:
        return _CACHE["data"]
    try:
        raw = _fetch("%2Fexport")
        rates = json.loads(_fetch("%2Frates"))["usd_per_unit"]
        source = "live"
    except Exception:
        raw = (HERE / "orders.ndjson").read_text(encoding="utf-8")
        rates = json.loads((HERE / "rates.json").read_text(encoding="utf-8"))["usd_per_unit"]
        source = "snapshot"

    latest = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        o["_upd"] = _parse_ts(o["updated_at"])
        cur = latest.get(o["id"])
        if cur is None or o["_upd"] > cur["_upd"]:      # compare real instants, not strings
            latest[o["id"]] = o

    orders = []
    for o in latest.values():
        o["_ist"] = _parse_ts(o["created_at"]).astimezone(IST)
        o["_usd"] = o["amount"] * rates[o["currency"]]
        orders.append(o)

    data = {"orders": orders, "rates": rates, "source": source,
            "products": sorted({o["product"] for o in orders}),
            "regions": sorted({o["region"] for o in orders}),
            "customers": sorted({o["customer"] for o in orders})}
    _CACHE.update(at=time.time(), data=data)
    return data


# ------------------------------------------------------------------ parsing
def _plural_re(name: str) -> str:
    w = re.escape(name.lower()).replace(r"\ ", r"[\s-]+")
    return rf"\b{w}(?:e?s)?\b"


def _find_year(q: str):
    m = re.search(r"\b(20\d\d)\b", q)
    return int(m.group(1)) if m else None


def parse_time(q: str, default_year: int):
    """Returns (start, end) IST datetimes (end exclusive), or (None, None)."""
    year = _find_year(q) or default_year

    def mstart(y, m):
        return datetime(y, m, 1, tzinfo=IST)

    def mend(y, m):
        return mstart(y + (m == 12), 1 if m == 12 else m + 1)

    # explicit ISO dates: between 2026-03-01 and 2026-03-15 / from .. to ..
    dates = re.findall(r"\b(20\d\d)-(\d\d)-(\d\d)\b", q)
    if len(dates) >= 2:
        a = datetime(*map(int, dates[0]), tzinfo=IST)
        b = datetime(*map(int, dates[1]), tzinfo=IST) + timedelta(days=1)
        return a, b
    if len(dates) == 1:
        a = datetime(*map(int, dates[0]), tzinfo=IST)
        return a, a + timedelta(days=1)

    # quarters / halves
    m = re.search(r"\bq([1-4])\b", q) or re.search(r"\b(first|second|third|fourth) quarter\b", q)
    if m:
        g = m.group(1)
        n = {"first": 1, "second": 2, "third": 3, "fourth": 4}.get(g, None) or int(g)
        return mstart(year, 3 * n - 2), mend(year, 3 * n)
    m = re.search(r"\bh([12])\b", q) or re.search(r"\b(first|second) half\b", q)
    if m:
        n = {"first": 1, "second": 2}.get(m.group(1), None) or int(m.group(1))
        return mstart(year, 6 * n - 5), mend(year, 6 * n)

    # month names (one, or a range "from January to March")
    found = [(mm.start(), MONTHS[mm.group(1)]) for mm in
             re.finditer(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b", q)]
    # 'may' is also a verb: only trust it if followed by a year / preceded by in/of/during
    found = [(p, mo) for p, mo in found
             if not (mo == 5 and not re.search(r"(in|of|during|for|from|to|and|through)\s+may\b|\bmay\s+20\d\d", q))]
    if found:
        found.sort()
        first, last = found[0][1], found[-1][1]
        if len(found) >= 2 and re.search(r"\b(from|between|through|to|until|-)\b", q):
            lo, hi = min(first, last), max(first, last)
            return mstart(year, lo), mend(year, hi)
        return mstart(year, first), mend(year, first)

    if _find_year(q):
        return datetime(year, 1, 1, tzinfo=IST), datetime(year + 1, 1, 1, tzinfo=IST)
    return None, None


def parse(question: str, data: dict) -> dict:
    q = " " + question.lower().strip() + " "
    spec = {"filters": {}}

    # ---- entity filters
    regions = [r for r in data["regions"] if re.search(rf"\b{r.lower()}\b", q)]
    products = [p for p in data["products"] if re.search(_plural_re(p), q)]
    customers = [c for c in data["customers"] if re.search(rf"\b{c.lower()}\b", q)]

    # ---- group-by / ranking target ("which product", "top customer", "best-selling region")
    group = None
    m = (re.search(r"\b(which|top|best|worst|leading|biggest|largest|highest|lowest|most|least)\b"
                   r"[\w\s-]{0,30}?\b(products?|customers?|regions?|months?|items?|clients?|buyers?)\b", q)
         or re.search(r"\bwhat(?:\s+\w+)?\s+(products?|customers?|regions?|months?|items?|clients?|buyers?)\b", q))
    if m:
        word = m.group(m.lastindex)
        group = ("product" if word.startswith(("product", "item")) else
                 "customer" if word.startswith(("customer", "client", "buyer")) else
                 "region" if word.startswith("region") else "month")
    if re.search(r"\bbest[\s-]?sell", q):
        group = group or "product"
    named = {"product": products, "customer": customers, "region": regions}
    if group in named and named[group] and not re.search(r"\b(which|top|best|worst|most|least|highest|lowest)\b", q):
        group = None

    if group != "region" and regions:
        spec["filters"]["region"] = regions
    if group != "product" and products:
        spec["filters"]["product"] = products
    if group != "customer" and customers:
        spec["filters"]["customer"] = customers

    # original-currency filter (only when it clearly describes the orders, not the unit)
    m = (re.search(r"\b(usd|eur|inr|euro|rupee|dollar)s?[\s-]+(denominated|priced|orders?|transactions?|sales|purchases?)", q)
         or re.search(r"\b(?:orders?|transactions?|sales|purchases?)\s+(?:that\s+were\s+|were\s+)?(?:placed\s+|paid\s+|made\s+|billed\s+|priced\s+)?in\s+(usd|eur|inr|euros?|rupees?|dollars?)\b", q)
         or re.search(r"\bcurrency\s+(?:is\s+|was\s+|of\s+)?(usd|eur|inr)\b", q))
    if m:
        cur = m.group(1)
        cur = {"euro": "EUR", "rupee": "INR", "dollar": "USD"}.get(cur.rstrip("s"), cur.upper())
        spec["filters"]["currency"] = [cur]

    # ---- time window (IST)
    default_year = max(o["_ist"].year for o in data["orders"])
    start, end = parse_time(q, default_year)
    if start:
        spec["filters"]["start"], spec["filters"]["end"] = start, end

    # ---- status
    if re.search(r"\brefund", q):
        status = "refunded"
    elif re.search(r"\bvoid|\bcancel", q):
        status = "void"
    else:
        status = "paid"
    counting_all = re.search(r"\b(all|any)\s+(statuses|status)\b|\bregardless of status\b", q)

    # ---- metric
    if re.search(r"\b(how many|number of|count of|count)\b[\w\s-]{0,25}\b(customers?|clients?|buyers?)\b", q) \
            and re.search(r"\b(unique|distinct|different|how many|number of)\b", q):
        metric = "customers"
    elif re.search(r"\baverage|\bmean\b|\baov\b|\bper order\b", q):
        metric = "avg"
    elif re.search(r"\b(units?|quantity|quantities|qty|pieces|items sold)\b", q) \
            or re.search(r"\bhow many\b[\w\s-]{0,30}\b(sold|bought|purchased)\b", q) and not re.search(r"\borders?\b", q):
        metric = "qty"
    elif re.search(r"\b(how many|number of|count of|count)\b", q) and re.search(r"\b(orders?|transactions?|refunds?|purchases?|sales)\b", q):
        metric = "count"
    elif group and re.search(r"\bmost orders\b|\bfewest orders\b|\bmost (purchases|transactions)\b", q):
        metric = "count"
    else:
        metric = "usd"
    if metric == "count" and status == "paid" and not re.search(r"\bpaid\b|\brevenue\b|\bsold\b|\bsales\b", q):
        status = None  # "how many orders" with no status word -> every current order
    if counting_all:
        status = None
    spec["filters"]["status"] = status

    spec["metric"] = metric
    spec["group"] = group
    spec["order"] = "asc" if re.search(r"\b(least|lowest|fewest|smallest|worst|minimum|min)\b", q) else "desc"
    m = re.search(r"\btop\s+(\d+|two|three|four|five)\b", q)
    spec["limit"] = ({"two": 2, "three": 3, "four": 4, "five": 5}.get(m.group(1)) or int(m.group(1))) if m else 1
    return spec


# ------------------------------------------------------------------ compute
def select(data, f):
    out = []
    for o in data["orders"]:
        if f.get("status") and o["status"] != f["status"]:
            continue
        if "region" in f and o["region"] not in f["region"]:
            continue
        if "product" in f and o["product"] not in f["product"]:
            continue
        if "customer" in f and o["customer"] not in f["customer"]:
            continue
        if "currency" in f and o["currency"] not in f["currency"]:
            continue
        if "start" in f and not (f["start"] <= o["_ist"] < f["end"]):
            continue
        out.append(o)
    return out


def measure(rows, metric):
    if metric == "usd":
        return round(sum(o["_usd"] for o in rows) + 1e-9, 2)
    if metric == "count":
        return len(rows)
    if metric == "qty":
        return sum(o["qty"] for o in rows)
    if metric == "customers":
        return len({o["customer"] for o in rows})
    if metric == "avg":
        return round(sum(o["_usd"] for o in rows) / len(rows) + 1e-9, 2) if rows else 0
    raise ValueError(metric)


def answer(question: str):
    data = load_data()
    spec = parse(question, data)
    rows = select(data, spec["filters"])
    if spec["group"]:
        key = (lambda o: o["_ist"].strftime("%B %Y")) if spec["group"] == "month" else (lambda o: o[spec["group"]])
        buckets = defaultdict(list)
        for o in rows:
            buckets[key(o)].append(o)
        scored = sorted(((measure(v, spec["metric"]), k) for k, v in buckets.items()),
                        key=lambda t: (t[0] if spec["order"] == "asc" else -t[0], t[1]))
        picks = [k for _, k in scored[:spec["limit"]]]
        result = picks[0] if spec["limit"] == 1 else picks
    else:
        result = measure(rows, spec["metric"])
    return result, spec, data["source"]
