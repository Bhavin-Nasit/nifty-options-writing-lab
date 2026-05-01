from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, render_template_string


ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "configs"
IST = ZoneInfo("Asia/Kolkata")
KITE_BASE_URL = "https://api.kite.trade"
NIFTY_LTP_KEY = "NSE:NIFTY 50"

app = Flask(__name__)


@dataclass(frozen=True)
class OptionInstrument:
    tradingsymbol: str
    expiry: date
    strike: int
    option_type: str
    lot_size: int


def today_ist() -> date:
    return datetime.now(IST).date()


def now_ist_label() -> str:
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")


def round_to_step(value: float, step: int = 50) -> int:
    return int(round(value / step) * step)


def floor_to_step(value: float, step: int = 50) -> int:
    return int(math.floor(value / step) * step)


def ceil_to_step(value: float, step: int = 50) -> int:
    return int(math.ceil(value / step) * step)


def next_tuesday(base: date) -> date:
    days = (1 - base.weekday()) % 7
    return base + timedelta(days=days)


def last_tuesday(year: int, month: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != 1:
        cursor -= timedelta(days=1)
    return cursor


def fallback_monthly_expiry(base: date) -> date:
    expiry = last_tuesday(base.year, base.month)
    if expiry < base:
        year = base.year + 1 if base.month == 12 else base.year
        month = 1 if base.month == 12 else base.month + 1
        expiry = last_tuesday(year, month)
    return expiry


def kite_headers() -> dict[str, str] | None:
    api_key = os.getenv("KITE_API_KEY")
    access_token = os.getenv("KITE_ACCESS_TOKEN")
    if not api_key or not access_token:
        return None
    return {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{access_token}",
    }


def kite_get(path: str, headers: dict[str, str], params=None) -> requests.Response:
    response = requests.get(f"{KITE_BASE_URL}{path}", headers=headers, params=params, timeout=20)
    response.raise_for_status()
    return response


def parse_instruments(csv_text: str, base: date) -> list[OptionInstrument]:
    instruments: list[OptionInstrument] = []
    for row in csv.DictReader(StringIO(csv_text)):
        try:
            if row.get("segment") != "NFO-OPT":
                continue
            if row.get("instrument_type") not in {"CE", "PE"}:
                continue
            name = (row.get("name") or "").upper()
            symbol = row.get("tradingsymbol") or ""
            if name not in {"", "NIFTY"} and not symbol.startswith("NIFTY"):
                continue
            expiry = date.fromisoformat((row.get("expiry") or "")[:10])
            if expiry < base:
                continue
            instruments.append(
                OptionInstrument(
                    tradingsymbol=symbol,
                    expiry=expiry,
                    strike=int(float(row.get("strike") or 0)),
                    option_type=row.get("instrument_type") or "",
                    lot_size=int(float(row.get("lot_size") or 50)),
                )
            )
        except (TypeError, ValueError):
            continue
    return sorted(instruments, key=lambda item: (item.expiry, item.strike, item.option_type))


def index_instruments(instruments: list[OptionInstrument]) -> dict[tuple[date, int, str], OptionInstrument]:
    return {(item.expiry, item.strike, item.option_type): item for item in instruments}


def nearest_available_strike(target: int, option_type: str, expiry: date, index: dict[tuple[date, int, str], OptionInstrument]) -> int:
    strikes = sorted(strike for exp, strike, typ in index if exp == expiry and typ == option_type)
    if not strikes:
        return target
    return min(strikes, key=lambda strike: abs(strike - target))


def choose_expiries(instruments: list[OptionInstrument], base: date) -> tuple[date, date]:
    expiries = sorted({item.expiry for item in instruments if item.expiry >= base})
    if not expiries:
        return next_tuesday(base), fallback_monthly_expiry(base)

    weekly = expiries[0]
    last_by_month: dict[tuple[int, int], date] = {}
    for expiry in expiries:
        key = (expiry.year, expiry.month)
        last_by_month[key] = max(last_by_month.get(key, expiry), expiry)
    monthly_candidates = sorted(last_by_month.values())
    monthly = monthly_candidates[0] if monthly_candidates else fallback_monthly_expiry(base)
    return weekly, monthly


def fetch_market_snapshot() -> dict[str, object]:
    base = today_ist()
    headers = kite_headers()
    if not headers:
        weekly = next_tuesday(base)
        monthly = fallback_monthly_expiry(base)
        return {
            "source": "sample",
            "status": "Kite env vars missing. Showing sample action plan from fallback NIFTY spot.",
            "spot": 24500.0,
            "as_of": now_ist_label(),
            "weekly_expiry": weekly,
            "monthly_expiry": monthly,
            "instruments": [],
            "index": {},
            "headers": None,
        }

    try:
        ltp_payload = kite_get("/quote/ltp", headers, params=[("i", NIFTY_LTP_KEY)]).json()
        spot = float(ltp_payload["data"][NIFTY_LTP_KEY]["last_price"])
        instruments_csv = kite_get("/instruments/NFO", headers).text
        instruments = parse_instruments(instruments_csv, base)
        weekly, monthly = choose_expiries(instruments, base)
        return {
            "source": "kite",
            "status": "Live Kite data. Refreshes every 5 minutes while market/token is available.",
            "spot": spot,
            "as_of": now_ist_label(),
            "weekly_expiry": weekly,
            "monthly_expiry": monthly,
            "instruments": instruments,
            "index": index_instruments(instruments),
            "headers": headers,
        }
    except Exception as exc:  # noqa: BLE001 - dashboard should degrade gracefully
        weekly = next_tuesday(base)
        monthly = fallback_monthly_expiry(base)
        return {
            "source": "sample",
            "status": f"Kite fetch failed: {exc}. Showing sample action plan.",
            "spot": 24500.0,
            "as_of": now_ist_label(),
            "weekly_expiry": weekly,
            "monthly_expiry": monthly,
            "instruments": [],
            "index": {},
            "headers": None,
        }


def find_contract(expiry: date, strike: int, option_type: str, index: dict[tuple[date, int, str], OptionInstrument]) -> OptionInstrument | None:
    return index.get((expiry, strike, option_type))


def build_leg(side: str, expiry: date, strike: int, option_type: str, lot_size: int, index: dict[tuple[date, int, str], OptionInstrument]) -> dict[str, object]:
    contract = find_contract(expiry, strike, option_type, index)
    return {
        "side": side,
        "strike": strike,
        "type": option_type,
        "expiry": expiry.isoformat(),
        "symbol": contract.tradingsymbol if contract else f"NIFTY {expiry.isoformat()} {strike}{option_type}",
        "lot_size": contract.lot_size if contract else lot_size,
        "ltp": None,
    }


def price_legs(plans: list[dict[str, object]], headers: dict[str, str] | None) -> None:
    if not headers:
        return
    symbol_to_leg: dict[str, dict[str, object]] = {}
    params = []
    for plan in plans:
        for leg in plan["legs"]:
            symbol = leg.get("symbol")
            if isinstance(symbol, str) and symbol.startswith("NIFTY") and " " not in symbol:
                symbol_to_leg[symbol] = leg
                params.append(("i", f"NFO:{symbol}"))
    if not params:
        return
    try:
        payload = kite_get("/quote/ltp", headers, params=params).json().get("data", {})
        for key, value in payload.items():
            symbol = key.split(":", 1)[-1]
            if symbol in symbol_to_leg:
                symbol_to_leg[symbol]["ltp"] = float(value.get("last_price") or 0)
    except Exception:
        return


def estimate_plan_values(plan: dict[str, object]) -> None:
    legs = plan["legs"]
    if any(leg.get("ltp") is None for leg in legs):
        plan["net_credit"] = None
        plan["max_risk"] = None
        plan["suggested_lots"] = 1
        plan["target_profit"] = None
        plan["stop_loss"] = None
        return

    credit = sum(float(leg["ltp"]) for leg in legs if leg["side"] == "SELL") - sum(
        float(leg["ltp"]) for leg in legs if leg["side"] == "BUY"
    )
    lot_size = int(legs[0].get("lot_size") or 50)
    wing = int(plan["wing_width"])
    max_risk_per_lot = max(0.0, (wing - credit) * lot_size)
    risk_budget = float(plan["capital"]) * float(plan["risk_pct"])
    lots = 1 if max_risk_per_lot <= 0 else max(1, min(int(plan["max_lots"]), int(risk_budget // max_risk_per_lot)))

    plan["net_credit"] = round(credit, 2)
    plan["max_risk"] = round(max_risk_per_lot * lots, 0)
    plan["suggested_lots"] = lots
    plan["target_profit"] = round(credit * lot_size * lots * float(plan["target_capture"]), 0)
    plan["stop_loss"] = round(credit * lot_size * lots * float(plan["stop_multiple"]), 0)


def build_action_plan(kind: str, snapshot: dict[str, object]) -> dict[str, object]:
    base = today_ist()
    spot = float(snapshot["spot"])
    index = snapshot["index"]
    atm = round_to_step(spot)

    if kind == "weekly":
        expiry = snapshot["weekly_expiry"]
        distance = max(350, ceil_to_step(spot * 0.025))
        wing = 300
        capital = 400000
        risk_pct = 0.06
        max_lots = 3
        target_capture = 0.55
        stop_multiple = 1.75
        title = "Weekly Iron Condor"
        timing = "Plan after the first 30-45 minutes. Avoid new entry if NIFTY is trending beyond the morning range."
    else:
        expiry = snapshot["monthly_expiry"]
        distance = max(700, ceil_to_step(spot * 0.045))
        wing = 500
        capital = 400000
        risk_pct = 0.045
        max_lots = 2
        target_capture = 0.60
        stop_multiple = 1.90
        title = "Monthly Wide Iron Condor"
        timing = "Prefer entry only when VIX/IV is elevated but cooling. Avoid major event weeks."

    expiry = expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry))
    pe_short = nearest_available_strike(floor_to_step(atm - distance), "PE", expiry, index)
    ce_short = nearest_available_strike(ceil_to_step(atm + distance), "CE", expiry, index)
    pe_buy = nearest_available_strike(pe_short - wing, "PE", expiry, index)
    ce_buy = nearest_available_strike(ce_short + wing, "CE", expiry, index)
    lot_size = 50
    first_contract = find_contract(expiry, pe_short, "PE", index) or find_contract(expiry, ce_short, "CE", index)
    if first_contract:
        lot_size = first_contract.lot_size

    dte = (expiry - base).days
    plan = {
        "kind": kind,
        "title": title,
        "expiry": expiry.isoformat(),
        "dte": dte,
        "spot": round(spot, 2),
        "capital": capital,
        "risk_pct": risk_pct,
        "max_lots": max_lots,
        "wing_width": wing,
        "target_capture": target_capture,
        "stop_multiple": stop_multiple,
        "legs": [
            build_leg("SELL", expiry, pe_short, "PE", lot_size, index),
            build_leg("SELL", expiry, ce_short, "CE", lot_size, index),
            build_leg("BUY", expiry, pe_buy, "PE", lot_size, index),
            build_leg("BUY", expiry, ce_buy, "CE", lot_size, index),
        ],
        "entry_filter": timing,
        "invalid_if": "Do not enter if spot is within 150 points of either short strike, if IV is expanding sharply, or if the net credit is too small versus max risk.",
        "exit_plan": "Book 50-60% of credit or exit if combined spread loss reaches the stop. Avoid holding near-ATM shorts into the final settlement window.",
        "suggested_lots": 1,
        "net_credit": None,
        "max_risk": None,
        "target_profit": None,
        "stop_loss": None,
    }
    return plan


def trade_line(plan: dict[str, object]) -> str:
    lots = plan.get("suggested_lots", 1)
    sell_legs = [leg for leg in plan["legs"] if leg["side"] == "SELL"]
    buy_legs = [leg for leg in plan["legs"] if leg["side"] == "BUY"]
    sell_text = " + ".join(f"SELL {lots} lot {leg['strike']} {leg['type']}" for leg in sell_legs)
    buy_text = " + ".join(f"BUY {lots} lot {leg['strike']} {leg['type']}" for leg in buy_legs)
    return f"{sell_text}; hedge with {buy_text}"


def load_action_board() -> dict[str, object]:
    snapshot = fetch_market_snapshot()
    plans = [build_action_plan("weekly", snapshot), build_action_plan("monthly", snapshot)]
    price_legs(plans, snapshot.get("headers"))
    for plan in plans:
        estimate_plan_values(plan)
        plan["trade_line"] = trade_line(plan)
    return {
        "source": snapshot["source"],
        "status": snapshot["status"],
        "spot": snapshot["spot"],
        "as_of": snapshot["as_of"],
        "plans": plans,
    }


PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>NIFTY Options Action Board</title>
  <style>
    :root { --ink:#172026; --muted:#59656f; --line:#d7dde2; --panel:#ffffff; --page:#f4f7f5; --green:#0f7a55; --amber:#b7791f; --red:#b42318; --teal:#0e7490; --slate:#243746; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--ink); background:var(--page); }
    header { border-bottom:1px solid var(--line); background:#fbfcfb; }
    .wrap { width:min(1180px, calc(100vw - 32px)); margin:0 auto; }
    .topbar { display:flex; align-items:center; justify-content:space-between; gap:20px; padding:18px 0; }
    .brand { display:flex; align-items:center; gap:12px; min-width:0; }
    .mark { width:38px; height:38px; border:2px solid var(--green); display:grid; place-items:center; font-weight:800; color:var(--green); }
    h1 { margin:0; font-size:20px; line-height:1.2; letter-spacing:0; }
    .brand span, .muted { color:var(--muted); font-size:13px; }
    .status { display:flex; align-items:center; gap:9px; padding:8px 11px; border:1px solid var(--line); background:#fff; font-size:13px; max-width:460px; }
    .dot { width:9px; height:9px; border-radius:99px; background:var(--green); flex:0 0 auto; }
    .dot.sample { background:var(--amber); }
    main { padding:26px 0 40px; }
    .hero { display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:10px; margin-bottom:18px; }
    .kpi, .panel, .action { background:var(--panel); border:1px solid var(--line); border-radius:8px; }
    .kpi { padding:14px; min-height:86px; }
    .kpi b { display:block; font-size:23px; margin-bottom:6px; }
    .grid { display:grid; grid-template-columns: 1.35fr .85fr; gap:18px; align-items:start; }
    .section { padding:18px; }
    .section + .section { border-top:1px solid var(--line); }
    h2 { margin:0 0 14px; font-size:16px; letter-spacing:0; }
    .actions { display:grid; gap:12px; }
    .action { padding:16px; }
    .action-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px; }
    .action h3 { margin:0 0 4px; font-size:16px; letter-spacing:0; }
    .badge { border:1px solid var(--line); padding:7px 9px; font-size:12px; background:#fafafa; white-space:nowrap; }
    .trade { padding:13px; border-radius:8px; background:#111820; color:#e9f2ef; line-height:1.55; font-family: Consolas, ui-monospace, monospace; font-size:13px; margin-bottom:12px; }
    .metrics { display:grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap:8px; margin-bottom:12px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fff; }
    .metric b { display:block; font-size:15px; margin-bottom:3px; }
    .metric span { color:var(--muted); font-size:12px; }
    .legs { width:100%; border-collapse:collapse; font-size:13px; margin-bottom:12px; }
    .legs th, .legs td { padding:9px 8px; border-bottom:1px solid var(--line); text-align:left; }
    .legs th { color:var(--muted); font-weight:650; }
    .sell { color:var(--red); font-weight:700; }
    .buy { color:var(--green); font-weight:700; }
    .notes { display:grid; gap:8px; }
    .note { border-left:3px solid var(--teal); padding:8px 10px; background:#f8fbfb; color:#34424d; font-size:13px; line-height:1.45; }
    .note.warn { border-color:var(--amber); background:#fff9ef; }
    .flow { display:grid; gap:10px; }
    .step { display:grid; grid-template-columns:28px 1fr; gap:10px; align-items:start; }
    .step i { display:grid; place-items:center; width:28px; height:28px; border:1px solid var(--line); color:var(--teal); font-style:normal; font-weight:700; }
    .step strong { display:block; font-size:13px; margin-bottom:3px; }
    .step span { display:block; color:var(--muted); font-size:12px; line-height:1.35; }
    code { display:block; padding:12px; border:1px solid var(--line); border-radius:8px; background:#111820; color:#e9f2ef; overflow-x:auto; font-size:12px; line-height:1.6; }
    footer { padding:18px 0 30px; color:var(--muted); font-size:12px; }
    @media (max-width: 920px) { .topbar { align-items:flex-start; flex-direction:column; } .grid, .hero, .metrics { grid-template-columns:1fr; } .status { max-width:none; } .action-head { flex-direction:column; } }
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <div class="brand"><div class="mark">N</div><div><h1>NIFTY Options Action Board</h1><span>Weekly and monthly option-writing plans from available data</span></div></div>
      <div class="status"><span class="dot {% if board.source == 'sample' %}sample{% endif %}"></span>{{ board.status }}</div>
    </div>
  </header>

  <main class="wrap">
    <div class="hero">
      <div class="kpi"><b>{{ '%.2f'|format(board.spot) }}</b><span>NIFTY spot used for strike selection</span></div>
      <div class="kpi"><b>{{ board.source|upper }}</b><span>Data source</span></div>
      <div class="kpi"><b>{{ board.as_of }}</b><span>Last refresh</span></div>
      <div class="kpi"><b>5 min</b><span>Auto-refresh interval</span></div>
    </div>

    <div class="grid">
      <div class="panel">
        <section class="section">
          <h2>Actionable Writing Plans</h2>
          <div class="actions">
            {% for plan in board.plans %}
            <article class="action">
              <div class="action-head">
                <div><h3>{{ plan.title }}</h3><div class="muted">Expiry {{ plan.expiry }} | DTE {{ plan.dte }} | Spot {{ plan.spot }}</div></div>
                <div class="badge">{{ plan.suggested_lots }} lot{% if plan.suggested_lots != 1 %}s{% endif %}</div>
              </div>
              <div class="trade">{{ plan.trade_line }}</div>
              <div class="metrics">
                <div class="metric"><b>{% if plan.net_credit is not none %}{{ plan.net_credit }}{% else %}Needs Kite{% endif %}</b><span>Net credit / unit</span></div>
                <div class="metric"><b>{% if plan.max_risk is not none %}₹{{ '{:,.0f}'.format(plan.max_risk) }}{% else %}Needs prices{% endif %}</b><span>Approx max risk</span></div>
                <div class="metric"><b>{% if plan.target_profit is not none %}₹{{ '{:,.0f}'.format(plan.target_profit) }}{% else %}50-60% credit{% endif %}</b><span>Target</span></div>
                <div class="metric"><b>{% if plan.stop_loss is not none %}₹{{ '{:,.0f}'.format(plan.stop_loss) }}{% else %}Credit multiple{% endif %}</b><span>Stop reference</span></div>
              </div>
              <table class="legs">
                <tr><th>Side</th><th>Strike</th><th>Type</th><th>Symbol</th><th>LTP</th></tr>
                {% for leg in plan.legs %}
                <tr><td class="{{ leg.side|lower }}">{{ leg.side }}</td><td>{{ leg.strike }}</td><td>{{ leg.type }}</td><td>{{ leg.symbol }}</td><td>{% if leg.ltp is not none %}{{ leg.ltp }}{% else %}-{% endif %}</td></tr>
                {% endfor %}
              </table>
              <div class="notes">
                <div class="note">Entry: {{ plan.entry_filter }}</div>
                <div class="note warn">Invalid if: {{ plan.invalid_if }}</div>
                <div class="note">Exit: {{ plan.exit_plan }}</div>
              </div>
            </article>
            {% endfor %}
          </div>
        </section>
      </div>

      <aside class="panel">
        <section class="section">
          <h2>How To Use</h2>
          <div class="flow">
            <div class="step"><i>1</i><div><strong>Check source</strong><span>If source is SAMPLE, add Kite env vars in Render before trusting strikes.</span></div></div>
            <div class="step"><i>2</i><div><strong>Prefer defined risk</strong><span>Enter the hedge legs with the short legs, not later.</span></div></div>
            <div class="step"><i>3</i><div><strong>Respect invalidation</strong><span>Skip trades near short strikes, during IV expansion, or event risk.</span></div></div>
            <div class="step"><i>4</i><div><strong>Refresh before order</strong><span>Use the board as a planning input, then verify in Zerodha order window.</span></div></div>
          </div>
        </section>
        <section class="section">
          <h2>Render Env Vars</h2>
          <code>KITE_API_KEY=...<br>KITE_ACCESS_TOKEN=...</code>
          <p class="muted">Kite access tokens are daily tokens. If the token expires, the board safely falls back to sample mode.</p>
        </section>
      </aside>
    </div>
  </main>

  <footer class="wrap">Research software only. This is not investment advice or an order instruction. Verify liquidity, margin, and risk before trading.</footer>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(PAGE, board=load_action_board())


@app.get("/api/action-plan")
def action_plan():
    return jsonify(load_action_board())


@app.get("/api/strategy-configs")
def strategy_configs():
    configs: list[dict[str, object]] = []
    for path in sorted(CONFIG_DIR.glob("*.json")):
        try:
            configs.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return jsonify(configs)


@app.get("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8050, debug=True)
