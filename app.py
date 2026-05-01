from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, render_template_string


ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "configs"
IST = ZoneInfo("Asia/Kolkata")
NSE_BASE_URL = "https://www.nseindia.com"
NSE_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-indices?symbol=NIFTY"
NSE_CHAIN_PAGE = f"{NSE_BASE_URL}/option-chain"
CACHE_SECONDS = int(os.getenv("NSE_CACHE_SECONDS", "900"))
NIFTY_LOT_SIZE = int(os.getenv("NIFTY_LOT_SIZE", "65"))

app = Flask(__name__)
_CACHE: dict[str, object] = {"expires_at": datetime.min.replace(tzinfo=IST), "board": None}


@dataclass(frozen=True)
class OptionRow:
    expiry: date
    strike: int
    ce_ltp: float
    pe_ltp: float
    ce_oi: int
    pe_oi: int
    ce_chg_oi: int
    pe_chg_oi: int
    ce_volume: int
    pe_volume: int
    ce_iv: float
    pe_iv: float


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def now_ist_label() -> str:
    return now_ist().strftime("%d %b %Y, %I:%M %p IST")


def round_to_step(value: float, step: int = 50) -> int:
    return int(round(value / step) * step)


def floor_to_step(value: float, step: int = 50) -> int:
    return int(math.floor(value / step) * step)


def ceil_to_step(value: float, step: int = 50) -> int:
    return int(math.ceil(value / step) * step)


def parse_nse_expiry(value: str) -> date:
    return datetime.strptime(value, "%d-%b-%Y").date()


def last_expiry_by_month(expiries: list[date]) -> list[date]:
    by_month: dict[tuple[int, int], date] = {}
    for expiry in expiries:
        key = (expiry.year, expiry.month)
        by_month[key] = max(expiry, by_month.get(key, expiry))
    return sorted(by_month.values())


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


def nse_headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": NSE_CHAIN_PAGE,
        "Connection": "keep-alive",
    }


def fetch_nse_option_chain() -> dict[str, object]:
    session = requests.Session()
    session.headers.update(nse_headers())
    session.get(NSE_BASE_URL, timeout=12)
    session.get(NSE_CHAIN_PAGE, timeout=12)
    response = session.get(NSE_CHAIN_URL, timeout=20)
    response.raise_for_status()
    return response.json()


def safe_number(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, "-"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_chain(payload: dict[str, object]) -> tuple[float, str, list[date], list[OptionRow]]:
    records = payload.get("records", {}) if isinstance(payload, dict) else {}
    spot = safe_number(records.get("underlyingValue"), 0.0) if isinstance(records, dict) else 0.0
    timestamp = str(records.get("timestamp") or now_ist_label()) if isinstance(records, dict) else now_ist_label()
    expiry_values = records.get("expiryDates", []) if isinstance(records, dict) else []
    expiries = sorted(parse_nse_expiry(value) for value in expiry_values)

    rows: list[OptionRow] = []
    raw_rows = records.get("data", []) if isinstance(records, dict) else []
    for item in raw_rows:
        if not isinstance(item, dict) or "expiryDate" not in item:
            continue
        try:
            expiry = parse_nse_expiry(str(item["expiryDate"]))
            strike = int(safe_number(item.get("strikePrice"), 0))
            ce = item.get("CE") if isinstance(item.get("CE"), dict) else {}
            pe = item.get("PE") if isinstance(item.get("PE"), dict) else {}
            rows.append(
                OptionRow(
                    expiry=expiry,
                    strike=strike,
                    ce_ltp=safe_number(ce.get("lastPrice")),
                    pe_ltp=safe_number(pe.get("lastPrice")),
                    ce_oi=int(safe_number(ce.get("openInterest"))),
                    pe_oi=int(safe_number(pe.get("openInterest"))),
                    ce_chg_oi=int(safe_number(ce.get("changeinOpenInterest"))),
                    pe_chg_oi=int(safe_number(pe.get("changeinOpenInterest"))),
                    ce_volume=int(safe_number(ce.get("totalTradedVolume"))),
                    pe_volume=int(safe_number(pe.get("totalTradedVolume"))),
                    ce_iv=safe_number(ce.get("impliedVolatility")),
                    pe_iv=safe_number(pe.get("impliedVolatility")),
                )
            )
        except (TypeError, ValueError):
            continue
    return spot, timestamp, expiries, rows


def choose_expiries(expiries: list[date], base: date) -> tuple[date, date]:
    future_expiries = sorted(expiry for expiry in expiries if expiry >= base)
    if not future_expiries:
        return next_tuesday(base), fallback_monthly_expiry(base)
    weekly = future_expiries[0]
    monthly_candidates = [expiry for expiry in last_expiry_by_month(future_expiries) if expiry >= base]
    monthly = monthly_candidates[0] if monthly_candidates else fallback_monthly_expiry(base)
    return weekly, monthly


def rows_for_expiry(rows: list[OptionRow], expiry: date) -> list[OptionRow]:
    return sorted((row for row in rows if row.expiry == expiry), key=lambda row: row.strike)


def row_index(rows: list[OptionRow]) -> dict[int, OptionRow]:
    return {row.strike: row for row in rows}


def nearest_strike_with_row(target: int, rows: list[OptionRow]) -> int:
    if not rows:
        return target
    return min((row.strike for row in rows), key=lambda strike: abs(strike - target))


def metric_max(candidates: list[dict[str, float]], key: str) -> float:
    value = max((candidate.get(key, 0.0) for candidate in candidates), default=0.0)
    return value if value > 0 else 1.0


def select_short_candidate(
    rows: list[OptionRow],
    option_type: str,
    spot: float,
    min_distance: int,
    max_distance: int,
    min_premium: float,
) -> dict[str, object]:
    candidates: list[dict[str, float]] = []
    for row in rows:
        distance = spot - row.strike if option_type == "PE" else row.strike - spot
        if distance < min_distance or distance > max_distance:
            continue
        ltp = row.pe_ltp if option_type == "PE" else row.ce_ltp
        oi = row.pe_oi if option_type == "PE" else row.ce_oi
        chg_oi = row.pe_chg_oi if option_type == "PE" else row.ce_chg_oi
        volume = row.pe_volume if option_type == "PE" else row.ce_volume
        iv = row.pe_iv if option_type == "PE" else row.ce_iv
        if ltp < min_premium or oi <= 0:
            continue
        candidates.append(
            {
                "strike": float(row.strike),
                "ltp": ltp,
                "oi": float(oi),
                "chg_oi": float(max(0, chg_oi)),
                "volume": float(volume),
                "iv": iv,
                "distance": float(distance),
            }
        )

    if not candidates:
        target = floor_to_step(spot - min_distance) if option_type == "PE" else ceil_to_step(spot + min_distance)
        nearest = nearest_strike_with_row(target, rows)
        row = row_index(rows).get(nearest)
        return {
            "strike": nearest,
            "ltp": row.pe_ltp if row and option_type == "PE" else (row.ce_ltp if row else 0.0),
            "oi": row.pe_oi if row and option_type == "PE" else (row.ce_oi if row else 0),
            "chg_oi": row.pe_chg_oi if row and option_type == "PE" else (row.ce_chg_oi if row else 0),
            "volume": row.pe_volume if row and option_type == "PE" else (row.ce_volume if row else 0),
            "iv": row.pe_iv if row and option_type == "PE" else (row.ce_iv if row else 0.0),
            "score": 0.0,
            "reason": "Fallback nearest strike. Check liquidity manually.",
        }

    max_oi = metric_max(candidates, "oi")
    max_chg = metric_max(candidates, "chg_oi")
    max_volume = metric_max(candidates, "volume")
    max_ltp = metric_max(candidates, "ltp")
    max_dist = metric_max(candidates, "distance")
    best = None
    best_score = -999.0
    for candidate in candidates:
        distance_penalty = candidate["distance"] / max_dist
        score = (
            0.42 * candidate["oi"] / max_oi
            + 0.22 * candidate["chg_oi"] / max_chg
            + 0.18 * candidate["volume"] / max_volume
            + 0.18 * candidate["ltp"] / max_ltp
            - 0.10 * distance_penalty
        )
        if score > best_score:
            best_score = score
            best = candidate
    assert best is not None
    best["strike"] = int(best["strike"])
    best["score"] = round(best_score, 3)
    best["reason"] = "Selected by OI, positive OI change, volume, premium, and distance buffer."
    return best


def build_leg(side: str, row: OptionRow | None, strike: int, option_type: str, expiry: date) -> dict[str, object]:
    if row is None:
        ltp = 0.0
        oi = 0
        volume = 0
        iv = 0.0
    elif option_type == "PE":
        ltp, oi, volume, iv = row.pe_ltp, row.pe_oi, row.pe_volume, row.pe_iv
    else:
        ltp, oi, volume, iv = row.ce_ltp, row.ce_oi, row.ce_volume, row.ce_iv
    return {
        "side": side,
        "strike": strike,
        "type": option_type,
        "expiry": expiry.isoformat(),
        "symbol": f"NIFTY {expiry.strftime('%d-%b-%Y').upper()} {strike}{option_type}",
        "ltp": round(float(ltp), 2),
        "oi": int(oi),
        "volume": int(volume),
        "iv": round(float(iv), 2),
    }


def estimate_values(plan: dict[str, object]) -> None:
    legs = plan["legs"]
    credit = sum(float(leg["ltp"]) for leg in legs if leg["side"] == "SELL") - sum(
        float(leg["ltp"]) for leg in legs if leg["side"] == "BUY"
    )
    wing = int(plan["wing_width"])
    lot_size = int(plan["lot_size"])
    max_risk_per_lot = max(0.0, (wing - credit) * lot_size)
    risk_budget = float(plan["capital"]) * float(plan["risk_pct"])
    if max_risk_per_lot <= 0:
        lots = 1
    else:
        lots = max(1, min(int(plan["max_lots"]), int(risk_budget // max_risk_per_lot)))
    plan["suggested_lots"] = lots
    plan["net_credit"] = round(credit, 2)
    plan["max_risk"] = round(max_risk_per_lot * lots, 0)
    plan["target_profit"] = round(credit * lot_size * lots * float(plan["target_capture"]), 0)
    plan["stop_loss"] = round(credit * lot_size * lots * float(plan["stop_multiple"]), 0)
    plan["risk_budget"] = round(risk_budget, 0)
    plan["credit_ok"] = credit >= float(plan["min_credit"])


def trade_line(plan: dict[str, object]) -> str:
    lots = plan.get("suggested_lots", 1)
    sell_legs = [leg for leg in plan["legs"] if leg["side"] == "SELL"]
    buy_legs = [leg for leg in plan["legs"] if leg["side"] == "BUY"]
    sell_text = " + ".join(f"SELL {lots} lot {leg['strike']} {leg['type']}" for leg in sell_legs)
    buy_text = " + ".join(f"BUY {lots} lot {leg['strike']} {leg['type']}" for leg in buy_legs)
    return f"{sell_text}; hedge with {buy_text}"


def build_action_plan(kind: str, spot: float, expiry: date, rows: list[OptionRow]) -> dict[str, object]:
    base = today_ist()
    if kind == "weekly":
        min_distance = max(300, ceil_to_step(spot * 0.018))
        max_distance = max(900, ceil_to_step(spot * 0.055))
        min_premium = 8.0
        wing = 300
        capital = 400000
        risk_pct = 0.06
        max_lots = 3
        target_capture = 0.55
        stop_multiple = 1.75
        title = "Weekly OI-Supported Iron Condor"
        entry_filter = "Use after the first 30-45 minutes. Enter only if spot remains between the short strikes and market breadth is not one-way trending."
    else:
        min_distance = max(650, ceil_to_step(spot * 0.04))
        max_distance = max(1900, ceil_to_step(spot * 0.10))
        min_premium = 15.0
        wing = 500
        capital = 400000
        risk_pct = 0.045
        max_lots = 2
        target_capture = 0.60
        stop_multiple = 1.90
        title = "Monthly OI-Supported Wide Iron Condor"
        entry_filter = "Prefer entry when VIX/IV is elevated but cooling. Avoid budget, RBI, election, CPI/Fed, or major event weeks."

    index = row_index(rows)
    pe_short = select_short_candidate(rows, "PE", spot, min_distance, max_distance, min_premium)
    ce_short = select_short_candidate(rows, "CE", spot, min_distance, max_distance, min_premium)
    pe_short_strike = int(pe_short["strike"])
    ce_short_strike = int(ce_short["strike"])
    pe_buy_strike = nearest_strike_with_row(pe_short_strike - wing, rows)
    ce_buy_strike = nearest_strike_with_row(ce_short_strike + wing, rows)

    plan = {
        "kind": kind,
        "title": title,
        "expiry": expiry.isoformat(),
        "dte": (expiry - base).days,
        "spot": round(spot, 2),
        "capital": capital,
        "risk_pct": risk_pct,
        "max_lots": max_lots,
        "lot_size": NIFTY_LOT_SIZE,
        "wing_width": wing,
        "target_capture": target_capture,
        "stop_multiple": stop_multiple,
        "min_credit": min_premium * 2 * 0.65,
        "legs": [
            build_leg("SELL", index.get(pe_short_strike), pe_short_strike, "PE", expiry),
            build_leg("SELL", index.get(ce_short_strike), ce_short_strike, "CE", expiry),
            build_leg("BUY", index.get(pe_buy_strike), pe_buy_strike, "PE", expiry),
            build_leg("BUY", index.get(ce_buy_strike), ce_buy_strike, "CE", expiry),
        ],
        "entry_filter": entry_filter,
        "invalid_if": "Skip if spot is within 150 points of either short strike, if the selected short side OI starts unwinding sharply, or if net credit is below the minimum threshold.",
        "exit_plan": "Book 50-60% credit capture. Exit if combined spread loss reaches the stop reference or if spot closes beyond a short strike buffer.",
        "selection_reason": f"PE: {pe_short['reason']} CE: {ce_short['reason']}",
        "suggested_lots": 1,
        "net_credit": None,
        "max_risk": None,
        "target_profit": None,
        "stop_loss": None,
        "credit_ok": False,
    }
    estimate_values(plan)
    plan["trade_line"] = trade_line(plan)
    return plan


def sample_payload() -> tuple[float, str, list[date], list[OptionRow]]:
    base = today_ist()
    weekly = next_tuesday(base)
    monthly = fallback_monthly_expiry(base)
    spot = 24500.0
    rows: list[OptionRow] = []
    for expiry in [weekly, monthly]:
        for strike in range(22000, 27050, 50):
            distance = abs(strike - spot)
            base_premium = max(4.0, 120.0 - distance * 0.11)
            ce_ltp = round(base_premium if strike >= spot else base_premium + (spot - strike), 2)
            pe_ltp = round(base_premium if strike <= spot else base_premium + (strike - spot), 2)
            ce_oi = max(50, int(9000 - abs(strike - 25500) * 8)) if strike >= spot else max(50, int(2200 - distance * 2))
            pe_oi = max(50, int(9000 - abs(strike - 23500) * 8)) if strike <= spot else max(50, int(2200 - distance * 2))
            rows.append(
                OptionRow(
                    expiry=expiry,
                    strike=strike,
                    ce_ltp=ce_ltp,
                    pe_ltp=pe_ltp,
                    ce_oi=ce_oi,
                    pe_oi=pe_oi,
                    ce_chg_oi=max(0, ce_oi // 8),
                    pe_chg_oi=max(0, pe_oi // 8),
                    ce_volume=max(10, ce_oi // 5),
                    pe_volume=max(10, pe_oi // 5),
                    ce_iv=13.5,
                    pe_iv=14.0,
                )
            )
    return spot, now_ist_label(), [weekly, monthly], rows


def load_action_board_uncached() -> dict[str, object]:
    base = today_ist()
    try:
        payload = fetch_nse_option_chain()
        spot, timestamp, expiries, rows = parse_chain(payload)
        if spot <= 0 or not rows:
            raise ValueError("NSE option chain returned no usable rows")
        weekly_expiry, monthly_expiry = choose_expiries(expiries, base)
        source = "NSE"
        status = "Public NSE option-chain data. No broker token required. Cached server-side."
    except Exception as exc:  # noqa: BLE001 - public NSE can rate-limit/cloud-block hosted apps
        stale = _CACHE.get("board")
        if isinstance(stale, dict) and stale.get("source") == "NSE":
            stale = dict(stale)
            stale["status"] = f"NSE refresh failed, showing last cached action board. Error: {exc}"
            return stale
        spot, timestamp, expiries, rows = sample_payload()
        weekly_expiry, monthly_expiry = choose_expiries(expiries, base)
        source = "SAMPLE"
        status = f"NSE refresh failed and no cache exists, showing sample plan. Error: {exc}"

    weekly_rows = rows_for_expiry(rows, weekly_expiry)
    monthly_rows = rows_for_expiry(rows, monthly_expiry)
    plans = [
        build_action_plan("weekly", spot, weekly_expiry, weekly_rows),
        build_action_plan("monthly", spot, monthly_expiry, monthly_rows),
    ]
    return {
        "source": source,
        "status": status,
        "spot": round(spot, 2),
        "as_of": timestamp,
        "server_refreshed_at": now_ist_label(),
        "cache_seconds": CACHE_SECONDS,
        "lot_size": NIFTY_LOT_SIZE,
        "plans": plans,
    }


def load_action_board() -> dict[str, object]:
    expires_at = _CACHE.get("expires_at")
    if isinstance(expires_at, datetime) and expires_at > now_ist() and isinstance(_CACHE.get("board"), dict):
        return _CACHE["board"]  # type: ignore[return-value]
    board = load_action_board_uncached()
    _CACHE["board"] = board
    _CACHE["expires_at"] = now_ist() + timedelta(seconds=CACHE_SECONDS)
    return board


PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>NIFTY Options Action Board</title>
  <style>
    :root { --ink:#172026; --muted:#59656f; --line:#d7dde2; --panel:#ffffff; --page:#f4f7f5; --green:#0f7a55; --amber:#b7791f; --red:#b42318; --teal:#0e7490; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--ink); background:var(--page); }
    header { border-bottom:1px solid var(--line); background:#fbfcfb; }
    .wrap { width:min(1180px, calc(100vw - 32px)); margin:0 auto; }
    .topbar { display:flex; align-items:center; justify-content:space-between; gap:20px; padding:18px 0; }
    .brand { display:flex; align-items:center; gap:12px; min-width:0; }
    .mark { width:38px; height:38px; border:2px solid var(--green); display:grid; place-items:center; font-weight:800; color:var(--green); }
    h1 { margin:0; font-size:20px; line-height:1.2; letter-spacing:0; }
    .brand span, .muted { color:var(--muted); font-size:13px; }
    .status { display:flex; align-items:center; gap:9px; padding:8px 11px; border:1px solid var(--line); background:#fff; font-size:13px; max-width:520px; }
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
    .ok { color:var(--green); font-weight:700; }
    .warn-text { color:var(--amber); font-weight:700; }
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
      <div class="brand"><div class="mark">N</div><div><h1>NIFTY Options Action Board</h1><span>Weekly and monthly option-writing plans from public NSE option-chain data</span></div></div>
      <div class="status"><span class="dot {% if board.source == 'SAMPLE' %}sample{% endif %}"></span>{{ board.status }}</div>
    </div>
  </header>

  <main class="wrap">
    <div class="hero">
      <div class="kpi"><b>{{ '%.2f'|format(board.spot) }}</b><span>NIFTY spot used for strike selection</span></div>
      <div class="kpi"><b>{{ board.source }}</b><span>Data source</span></div>
      <div class="kpi"><b>{{ board.as_of }}</b><span>NSE chain timestamp</span></div>
      <div class="kpi"><b>{{ board.cache_seconds // 60 }} min</b><span>Server cache interval</span></div>
    </div>

    <div class="grid">
      <div class="panel">
        <section class="section">
          <h2>Actionable Writing Plans</h2>
          <div class="actions">
            {% for plan in board.plans %}
            <article class="action">
              <div class="action-head">
                <div><h3>{{ plan.title }}</h3><div class="muted">Expiry {{ plan.expiry }} | DTE {{ plan.dte }} | Lot size {{ plan.lot_size }}</div></div>
                <div class="badge">{{ plan.suggested_lots }} lot{% if plan.suggested_lots != 1 %}s{% endif %}</div>
              </div>
              <div class="trade">{{ plan.trade_line }}</div>
              <div class="metrics">
                <div class="metric"><b>{{ plan.net_credit }}</b><span>Net credit / unit</span></div>
                <div class="metric"><b>INR {{ '{:,.0f}'.format(plan.max_risk) }}</b><span>Approx max risk</span></div>
                <div class="metric"><b>INR {{ '{:,.0f}'.format(plan.target_profit) }}</b><span>Target</span></div>
                <div class="metric"><b>INR {{ '{:,.0f}'.format(plan.stop_loss) }}</b><span>Stop reference</span></div>
              </div>
              <table class="legs">
                <tr><th>Side</th><th>Strike</th><th>Type</th><th>LTP</th><th>OI</th><th>Volume</th><th>IV</th></tr>
                {% for leg in plan.legs %}
                <tr><td class="{{ leg.side|lower }}">{{ leg.side }}</td><td>{{ leg.strike }}</td><td>{{ leg.type }}</td><td>{{ leg.ltp }}</td><td>{{ '{:,.0f}'.format(leg.oi) }}</td><td>{{ '{:,.0f}'.format(leg.volume) }}</td><td>{{ leg.iv }}</td></tr>
                {% endfor %}
              </table>
              <div class="notes">
                <div class="note">Credit check: <span class="{% if plan.credit_ok %}ok{% else %}warn-text{% endif %}">{% if plan.credit_ok %}OK{% else %}LOW CREDIT{% endif %}</span>. Minimum threshold {{ plan.min_credit }}.</div>
                <div class="note">Why these strikes: {{ plan.selection_reason }}</div>
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
            <div class="step"><i>1</i><div><strong>Check source</strong><span>Use only when source is NSE. SAMPLE mode is just a fallback.</span></div></div>
            <div class="step"><i>2</i><div><strong>Use defined risk</strong><span>Enter hedge legs with short legs. Do not run this naked unless you explicitly choose that risk.</span></div></div>
            <div class="step"><i>3</i><div><strong>Respect invalidation</strong><span>Skip trades near short strikes, during IV expansion, or during major event risk.</span></div></div>
            <div class="step"><i>4</i><div><strong>Verify in broker</strong><span>Check margin, liquidity, bid-ask spread, and freeze quantity before placing orders.</span></div></div>
          </div>
        </section>
        <section class="section">
          <h2>No Manual Token</h2>
          <code>Data source: NSE public option chain<br>Broker token: not required<br>Optional env: NIFTY_LOT_SIZE, NSE_CACHE_SECONDS</code>
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
