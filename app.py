from __future__ import annotations

import csv
import io
import json
import math
import os
import zipfile
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
NSE_ARCHIVE_BASE = "https://nsearchives.nseindia.com"
NSE_CHAIN_PAGE = f"{NSE_BASE_URL}/option-chain"
NSE_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-indices?symbol=NIFTY"
NSE_UDIFF_BHAVCOPY_URL = f"{NSE_ARCHIVE_BASE}/content/fo/BhavCopy_NSE_FO_0_0_0_{{stamp}}_F_0000.csv.zip"
NSE_LEGACY_BHAVCOPY_URLS = [
    f"{NSE_ARCHIVE_BASE}/content/historical/DERIVATIVES/{{year}}/{{month}}/fo{{stamp}}bhav.csv.zip",
    "https://archives.nseindia.com/content/historical/DERIVATIVES/{year}/{month}/fo{stamp}bhav.csv.zip",
]
NSE_PARTICIPANT_URLS = [
    f"{NSE_ARCHIVE_BASE}/content/nsccl/fao_participant_oi_{{stamp}}.csv",
    "https://archives.nseindia.com/content/nsccl/fao_participant_oi_{stamp}.csv",
]
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
    ce_change: float
    pe_change: float
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


def clean_cell(value: object) -> str:
    return str(value or "").strip().replace("\ufeff", "")


def safe_number(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, "-", ""):
            return default
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def safe_int(value: object, default: int = 0) -> int:
    return int(round(safe_number(value, float(default))))


def first_positive(item: dict[str, object], names: list[str]) -> float:
    for name in names:
        value = safe_number(item.get(name))
        if value > 0:
            return value
    return 0.0


def floor_to_step(value: float, step: int = 50) -> int:
    return int(math.floor(value / step) * step)


def ceil_to_step(value: float, step: int = 50) -> int:
    return int(math.ceil(value / step) * step)


def parse_any_date(value: object) -> date:
    raw = clean_cell(value)
    if not raw:
        raise ValueError("empty date")
    raw = raw[:10] if len(raw) > 10 and raw[4:5] == "-" else raw
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw.title(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unsupported date: {raw}")


def nse_headers(referer: str = NSE_CHAIN_PAGE) -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Connection": "keep-alive",
    }


def request_session(referer: str = NSE_CHAIN_PAGE) -> requests.Session:
    session = requests.Session()
    session.headers.update(nse_headers(referer))
    return session


def fetch_nse_option_chain() -> dict[str, object]:
    session = request_session()
    session.get(NSE_BASE_URL, timeout=12)
    session.get(NSE_CHAIN_PAGE, timeout=12)
    response = session.get(NSE_CHAIN_URL, timeout=20)
    response.raise_for_status()
    return response.json()


def parse_chain(payload: dict[str, object]) -> tuple[float, str, list[date], list[OptionRow]]:
    records = payload.get("records", {}) if isinstance(payload, dict) else {}
    filtered = payload.get("filtered", {}) if isinstance(payload, dict) else {}
    spot = safe_number(records.get("underlyingValue"), 0.0) if isinstance(records, dict) else 0.0
    if spot <= 0 and isinstance(filtered, dict):
        spot = safe_number(filtered.get("underlyingValue"), 0.0)
    timestamp = str(records.get("timestamp") or filtered.get("timestamp") or now_ist_label()) if isinstance(records, dict) else now_ist_label()

    expiries: list[date] = []
    for value in records.get("expiryDates", []) if isinstance(records, dict) else []:
        try:
            expiries.append(parse_any_date(value))
        except ValueError:
            continue

    raw_rows = records.get("data", []) if isinstance(records, dict) else []
    if not raw_rows and isinstance(filtered, dict):
        raw_rows = filtered.get("data", [])

    rows: list[OptionRow] = []
    for item in raw_rows:
        if not isinstance(item, dict) or "expiryDate" not in item:
            continue
        try:
            expiry = parse_any_date(item.get("expiryDate"))
            strike = safe_int(item.get("strikePrice"))
        except ValueError:
            continue
        ce = item.get("CE") if isinstance(item.get("CE"), dict) else {}
        pe = item.get("PE") if isinstance(item.get("PE"), dict) else {}
        rows.append(
            OptionRow(
                expiry=expiry,
                strike=strike,
                ce_ltp=safe_number(ce.get("lastPrice")),
                pe_ltp=safe_number(pe.get("lastPrice")),
                ce_change=safe_number(ce.get("change")),
                pe_change=safe_number(pe.get("change")),
                ce_oi=safe_int(ce.get("openInterest")),
                pe_oi=safe_int(pe.get("openInterest")),
                ce_chg_oi=safe_int(ce.get("changeinOpenInterest")),
                pe_chg_oi=safe_int(pe.get("changeinOpenInterest")),
                ce_volume=safe_int(ce.get("totalTradedVolume")),
                pe_volume=safe_int(pe.get("totalTradedVolume")),
                ce_iv=safe_number(ce.get("impliedVolatility")),
                pe_iv=safe_number(pe.get("impliedVolatility")),
            )
        )
    return spot, timestamp, sorted(set(expiries)), rows


def normalized_csv_rows(content: bytes) -> list[dict[str, object]]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        csv_name = next((name for name in archive.namelist() if name.lower().endswith(".csv")), None)
        if not csv_name:
            return []
        with archive.open(csv_name) as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8", errors="ignore")
            reader = csv.DictReader(text)
            return [{clean_cell(key): value for key, value in row.items()} for row in reader]


def parse_bhavcopy_rows(raw_rows: list[dict[str, object]], trade_day: date, label: str) -> tuple[float, str, list[date], list[OptionRow]] | None:
    grouped: dict[tuple[date, int], dict[str, dict[str, float]]] = {}
    futures: list[tuple[date, float]] = []
    underlyings: list[float] = []

    for item in raw_rows:
        symbol = clean_cell(item.get("TckrSymb") or item.get("SYMBOL")).upper()
        if symbol != "NIFTY":
            continue

        try:
            expiry = parse_any_date(item.get("XpryDt") or item.get("EXPIRY_DT"))
        except ValueError:
            continue

        instrument = clean_cell(item.get("FinInstrmNm") or item.get("INSTRUMENT")).upper()
        option_type = clean_cell(item.get("OptnTp") or item.get("OPTION_TYP")).upper()
        strike = safe_int(item.get("StrkPric") or item.get("STRIKE_PR"))
        underlying = first_positive(item, ["UndrlygPric", "UNDERLYING_VALUE"])
        if underlying > 0:
            underlyings.append(underlying)

        close = first_positive(item, ["ClsPric", "CLOSE", "LastPric", "SttlmPric", "SETTLE_PR"])
        previous = first_positive(item, ["PrvsClsgPric", "PREV_CLOSE"])
        change = close - previous if close > 0 and previous > 0 else 0.0

        if option_type in ("CE", "PE") and strike > 0:
            grouped.setdefault((expiry, strike), {})[option_type] = {
                "ltp": close,
                "change": change,
                "oi": first_positive(item, ["OpnIntrst", "OPEN_INT"]),
                "chg_oi": safe_number(item.get("ChngInOpnIntrst") or item.get("CHG_IN_OI")),
                "volume": first_positive(item, ["TtlTradgVol", "CONTRACTS", "TOTTRDQTY"]),
            }
            continue

        if "FUT" in instrument and close > 0:
            futures.append((expiry, close))

    rows: list[OptionRow] = []
    for (expiry, strike), legs in grouped.items():
        ce = legs.get("CE", {})
        pe = legs.get("PE", {})
        rows.append(
            OptionRow(
                expiry=expiry,
                strike=strike,
                ce_ltp=safe_number(ce.get("ltp")),
                pe_ltp=safe_number(pe.get("ltp")),
                ce_change=safe_number(ce.get("change")),
                pe_change=safe_number(pe.get("change")),
                ce_oi=safe_int(ce.get("oi")),
                pe_oi=safe_int(pe.get("oi")),
                ce_chg_oi=safe_int(ce.get("chg_oi")),
                pe_chg_oi=safe_int(pe.get("chg_oi")),
                ce_volume=safe_int(ce.get("volume")),
                pe_volume=safe_int(pe.get("volume")),
                ce_iv=0.0,
                pe_iv=0.0,
            )
        )

    if not rows:
        return None

    future_candidates = sorted((item for item in futures if item[0] >= trade_day and item[1] > 0), key=lambda item: item[0])
    if underlyings:
        spot = underlyings[-1]
    elif future_candidates:
        spot = future_candidates[0][1]
    else:
        spot = float(max(rows, key=lambda row: row.ce_oi + row.pe_oi).strike)
    expiries = sorted({row.expiry for row in rows if row.expiry >= trade_day})
    timestamp = f"{trade_day.strftime('%d %b %Y')} EOD {label}"
    return spot, timestamp, expiries, rows


def fetch_bhavcopy_for_day(session: requests.Session, day: date) -> tuple[float, str, list[date], list[OptionRow]] | None:
    ymd = day.strftime("%Y%m%d")
    legacy_stamp = day.strftime("%d%b%Y").upper()
    urls = [(NSE_UDIFF_BHAVCOPY_URL.format(stamp=ymd), "NSE F&O UDiFF bhavcopy")]
    for pattern in NSE_LEGACY_BHAVCOPY_URLS:
        urls.append((pattern.format(year=day.year, month=day.strftime("%b").upper(), stamp=legacy_stamp), "NSE legacy F&O bhavcopy"))

    for url, label in urls:
        try:
            response = session.get(url, timeout=25)
            if response.status_code != 200 or not response.content:
                continue
            parsed = parse_bhavcopy_rows(normalized_csv_rows(response.content), day, label)
            if parsed:
                return parsed
        except (requests.RequestException, zipfile.BadZipFile, OSError, csv.Error):
            continue
    return None


def fetch_bhavcopy_option_chain() -> tuple[float, str, list[date], list[OptionRow]]:
    session = request_session("https://www.nseindia.com/all-reports-derivatives")
    for offset in range(30):
        day = today_ist() - timedelta(days=offset)
        result = fetch_bhavcopy_for_day(session, day)
        if result:
            return result
    raise ValueError("NSE F&O bhavcopy fallback returned no usable rows from UDiFF or legacy archives")


def last_expiry_by_month(expiries: list[date]) -> list[date]:
    by_month: dict[tuple[int, int], date] = {}
    for expiry in expiries:
        by_month[(expiry.year, expiry.month)] = max(expiry, by_month.get((expiry.year, expiry.month), expiry))
    return sorted(by_month.values())


def next_tuesday(base: date) -> date:
    return base + timedelta(days=(1 - base.weekday()) % 7)


def last_tuesday(year: int, month: int) -> date:
    cursor = date(year + 1, 1, 1) - timedelta(days=1) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != 1:
        cursor -= timedelta(days=1)
    return cursor


def fallback_monthly_expiry(base: date) -> date:
    expiry = last_tuesday(base.year, base.month)
    if expiry >= base:
        return expiry
    year = base.year + 1 if base.month == 12 else base.year
    month = 1 if base.month == 12 else base.month + 1
    return last_tuesday(year, month)


def rows_for_expiry(rows: list[OptionRow], expiry: date) -> list[OptionRow]:
    return sorted((row for row in rows if row.expiry == expiry), key=lambda row: row.strike)


def choose_expiries(expiries: list[date], rows: list[OptionRow], base: date) -> tuple[date, date]:
    future_expiries = sorted(expiry for expiry in expiries if expiry >= base)
    if not future_expiries:
        return next_tuesday(base), fallback_monthly_expiry(base)
    weekly = next((expiry for expiry in future_expiries if rows_for_expiry(rows, expiry)), future_expiries[0])
    monthly_candidates = [expiry for expiry in last_expiry_by_month(future_expiries) if expiry >= base]
    monthly = monthly_candidates[0] if monthly_candidates else fallback_monthly_expiry(base)
    return weekly, monthly


def row_index(rows: list[OptionRow]) -> dict[int, OptionRow]:
    return {row.strike: row for row in rows}


def nearest_strike(target: int, rows: list[OptionRow]) -> int:
    if not rows:
        return target
    return min((row.strike for row in rows), key=lambda strike: abs(strike - target))


def option_values(row: OptionRow, option_type: str) -> tuple[float, float, int, int, int, float]:
    if option_type == "PE":
        return row.pe_ltp, row.pe_change, row.pe_oi, row.pe_chg_oi, row.pe_volume, row.pe_iv
    return row.ce_ltp, row.ce_change, row.ce_oi, row.ce_chg_oi, row.ce_volume, row.ce_iv


def metric_max(items: list[dict[str, object]], key: str) -> float:
    value = max((safe_number(item.get(key)) for item in items), default=0.0)
    return value if value > 0 else 1.0


def build_writing_zones(rows: list[OptionRow], option_type: str, spot: float) -> list[dict[str, object]]:
    raw: list[dict[str, object]] = []
    for row in rows:
        distance = spot - row.strike if option_type == "PE" else row.strike - spot
        if distance < 0:
            continue
        ltp, change, oi, chg_oi, volume, iv = option_values(row, option_type)
        if oi <= 0 or ltp <= 0:
            continue
        raw.append(
            {
                "strike": row.strike,
                "type": option_type,
                "ltp": round(ltp, 2),
                "change": round(change, 2),
                "oi": oi,
                "chg_oi": chg_oi,
                "volume": volume,
                "iv": round(iv, 2),
                "distance": round(distance, 0),
            }
        )
    if not raw:
        return []

    max_oi = metric_max(raw, "oi")
    max_chg = metric_max(raw, "chg_oi")
    max_volume = metric_max(raw, "volume")
    max_ltp = metric_max(raw, "ltp")
    max_distance = metric_max(raw, "distance")
    zones: list[dict[str, object]] = []
    for item in raw:
        chg_oi = safe_number(item.get("chg_oi"))
        price_change = safe_number(item.get("change"))
        if chg_oi > 0 and price_change <= 0:
            signal = "Fresh writing likely"
            writing_boost = 1.0
        elif chg_oi > 0:
            signal = "Fresh OI buildup"
            writing_boost = 0.65
        else:
            signal = "Existing OI wall"
            writing_boost = 0.25
        distance_score = 1.0 - min(safe_number(item.get("distance")) / max_distance, 1.0)
        score = 100 * (
            0.34 * safe_number(item.get("oi")) / max_oi
            + 0.24 * max(chg_oi, 0.0) / max_chg
            + 0.13 * safe_number(item.get("volume")) / max_volume
            + 0.11 * safe_number(item.get("ltp")) / max_ltp
            + 0.12 * writing_boost
            + 0.06 * distance_score
        )
        item["score"] = round(score, 1)
        item["signal"] = signal
        zones.append(item)
    return sorted(zones, key=lambda item: (safe_number(item.get("score")), safe_number(item.get("chg_oi"))), reverse=True)


def build_writer_map(rows: list[OptionRow], spot: float, expiry: date) -> dict[str, object]:
    puts = build_writing_zones(rows, "PE", spot)
    calls = build_writing_zones(rows, "CE", spot)
    total_pe_oi = sum(row.pe_oi for row in rows)
    total_ce_oi = sum(row.ce_oi for row in rows)
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0.0
    support = puts[0]["strike"] if puts else None
    resistance = calls[0]["strike"] if calls else None
    writer_range = f"{support} to {resistance}" if support and resistance else "Unavailable"
    if pcr >= 1.18:
        bias = "Put writers stronger"
    elif 0 < pcr <= 0.84:
        bias = "Call writers stronger"
    else:
        bias = "Balanced writers"
    return {
        "expiry": expiry.isoformat(),
        "puts": puts[:10],
        "calls": calls[:10],
        "support": support,
        "resistance": resistance,
        "writer_range": writer_range,
        "pcr": pcr,
        "bias": bias,
        "total_pe_oi": total_pe_oi,
        "total_ce_oi": total_ce_oi,
    }


def parse_participant_oi(text: str, day: date) -> dict[str, object] | None:
    rows = list(csv.reader(io.StringIO(text)))
    header_index = None
    for index, row in enumerate(rows):
        if row and clean_cell(row[0]).lower() == "client type":
            header_index = index
            break
    if header_index is None:
        return None

    headers = [clean_cell(cell) for cell in rows[header_index]]
    lookup = {name: pos for pos, name in enumerate(headers)}

    def value(row: list[str], name: str) -> int:
        pos = lookup.get(name)
        return safe_int(row[pos]) if pos is not None and pos < len(row) else 0

    parsed: dict[str, dict[str, int]] = {}
    for row in rows[header_index + 1 :]:
        label = clean_cell(row[0]).upper() if row else ""
        if label in ("CLIENT", "DII", "FII", "PRO"):
            parsed[label] = {
                "future_long": value(row, "Future Index Long"),
                "future_short": value(row, "Future Index Short"),
                "call_long": value(row, "Option Index Call Long"),
                "call_short": value(row, "Option Index Call Short"),
                "put_long": value(row, "Option Index Put Long"),
                "put_short": value(row, "Option Index Put Short"),
            }
    if not parsed:
        return None

    rows_out: list[dict[str, object]] = []
    for label in ("FII", "PRO", "CLIENT", "DII"):
        item = parsed.get(label)
        if item:
            rows_out.append(
                {
                    "client_type": label,
                    "future_net": item["future_long"] - item["future_short"],
                    "call_short": item["call_short"],
                    "put_short": item["put_short"],
                    "call_net": item["call_long"] - item["call_short"],
                    "put_net": item["put_long"] - item["put_short"],
                }
            )
    smart_call_short = sum(parsed.get(label, {}).get("call_short", 0) for label in ("FII", "PRO"))
    smart_put_short = sum(parsed.get(label, {}).get("put_short", 0) for label in ("FII", "PRO"))
    smart_future_net = sum(parsed.get(label, {}).get("future_long", 0) - parsed.get(label, {}).get("future_short", 0) for label in ("FII", "PRO"))
    if smart_call_short > smart_put_short * 1.15:
        bias = "FII + PRO call shorts heavier"
    elif smart_put_short > smart_call_short * 1.15:
        bias = "FII + PRO put shorts heavier"
    else:
        bias = "FII + PRO shorts balanced"
    return {
        "date": day.isoformat(),
        "rows": rows_out,
        "summary": {
            "smart_call_short": smart_call_short,
            "smart_put_short": smart_put_short,
            "smart_future_net": smart_future_net,
            "bias": bias,
        },
        "note": "Participant OI is EOD and category-level. It is not strike-specific.",
    }


def fetch_participant_oi() -> dict[str, object] | None:
    session = request_session("https://www.nseindia.com/all-reports-derivatives")
    for offset in range(14):
        day = today_ist() - timedelta(days=offset)
        for pattern in NSE_PARTICIPANT_URLS:
            url = pattern.format(stamp=day.strftime("%d%m%Y"))
            try:
                response = session.get(url, timeout=12)
                if response.status_code == 200 and "Client Type" in response.text:
                    parsed = parse_participant_oi(response.text, day)
                    if parsed:
                        return parsed
            except requests.RequestException:
                continue
    return None


def participant_bias(participant: dict[str, object] | None) -> str:
    if not participant:
        return "Participant OI unavailable"
    summary = participant.get("summary", {}) if isinstance(participant, dict) else {}
    return str(summary.get("bias") or "Participant OI unavailable")


def infer_regime(spot: float, zones: dict[str, object], participant: dict[str, object] | None, dte: int) -> dict[str, object]:
    puts = zones.get("puts", []) if isinstance(zones, dict) else []
    calls = zones.get("calls", []) if isinstance(zones, dict) else []
    if not puts or not calls:
        return {
            "name": "NO TRADE",
            "bias": "No reliable writer map",
            "score": 0,
            "reason": "Both PE and CE writer zones are required before selecting a strategy.",
        }

    top_put = puts[0]
    top_call = calls[0]
    support = safe_number(top_put.get("strike"))
    resistance = safe_number(top_call.get("strike"))
    put_score = safe_number(top_put.get("score"))
    call_score = safe_number(top_call.get("score"))
    pcr = safe_number(zones.get("pcr"))
    width = resistance - support
    lower_room = spot - support
    upper_room = resistance - spot
    part_bias = participant_bias(participant)

    if width <= 0 or lower_room < 120 or upper_room < 120:
        return {
            "name": "NO TRADE",
            "bias": "Spot too close to writer wall",
            "score": 35,
            "reason": "The nearest writer wall is too close to spot, so short premium can expand too quickly.",
        }
    if dte <= 0 and min(lower_room, upper_room) <= 180:
        return {
            "name": "EXPIRY PIN RISK",
            "bias": "Only intraday iron fly after movement dies",
            "score": 55,
            "reason": "Expiry-day writing needs late confirmation. Avoid early short gamma if spot is near a writer wall.",
        }
    if width >= 550 and abs(put_score - call_score) <= 14 and 0.85 <= pcr <= 1.25:
        return {
            "name": "RANGE",
            "bias": "Balanced writer range",
            "score": 78,
            "reason": f"PE and CE writer scores are balanced, PCR is {pcr}, and spot has room on both sides. {part_bias}.",
        }
    if pcr >= 1.15 and put_score >= call_score * 0.85:
        return {
            "name": "BULLISH RANGE",
            "bias": "Put writers in control",
            "score": 72,
            "reason": f"Put-side writing is stronger than call-side pressure, PCR is {pcr}, and support sits below spot. {part_bias}.",
        }
    if 0 < pcr <= 0.88 and call_score >= put_score * 0.85:
        return {
            "name": "BEARISH RANGE",
            "bias": "Call writers in control",
            "score": 72,
            "reason": f"Call-side writing is stronger than put-side support, PCR is {pcr}, and resistance sits above spot. {part_bias}.",
        }
    return {
        "name": "MIXED",
        "bias": "Writer map is not clean",
        "score": 58,
        "reason": f"Writer zones exist but direction is not clean enough. PCR is {pcr}. {part_bias}.",
    }


def strategy_settings(strategy: str, dte: int) -> dict[str, object]:
    intraday = dte <= 0
    capital = 1_800_000 if intraday else 400_000
    if strategy == "Bull Put Spread":
        return {"wing": 300, "capital": capital, "risk_pct": 0.06, "max_lots": 3, "min_credit": 35, "min_target": 1200, "target_capture": 0.55, "stop_multiple": 1.65, "min_rr": 0.07, "buffer": 180}
    if strategy == "Bear Call Spread":
        return {"wing": 300, "capital": capital, "risk_pct": 0.06, "max_lots": 3, "min_credit": 35, "min_target": 1200, "target_capture": 0.55, "stop_multiple": 1.65, "min_rr": 0.07, "buffer": 180}
    if strategy == "Expiry Intraday Iron Fly":
        return {"wing": 250, "capital": capital, "risk_pct": 0.04, "max_lots": 8, "min_credit": 85, "min_target": 2500, "target_capture": 0.38, "stop_multiple": 1.4, "min_rr": 0.09, "buffer": 0}
    if strategy == "Monthly Wide Iron Condor":
        return {"wing": 500, "capital": capital, "risk_pct": 0.05, "max_lots": 2, "min_credit": 65, "min_target": 1800, "target_capture": 0.45, "stop_multiple": 1.7, "min_rr": 0.075, "buffer": 350}
    return {"wing": 300, "capital": capital, "risk_pct": 0.06, "max_lots": 3, "min_credit": 45, "min_target": 1400, "target_capture": 0.50, "stop_multiple": 1.6, "min_rr": 0.08, "buffer": 220}


def pick_zone(zones: list[dict[str, object]], min_distance: int, min_ltp: float = 8.0) -> dict[str, object] | None:
    for zone in zones:
        if safe_number(zone.get("distance")) >= min_distance and safe_number(zone.get("ltp")) >= min_ltp:
            return zone
    for zone in zones:
        if safe_number(zone.get("distance")) >= min_distance:
            return zone
    return zones[0] if zones else None


def leg_dict(side: str, row: OptionRow | None, strike: int, option_type: str, expiry: date) -> dict[str, object]:
    if row:
        ltp, change, oi, chg_oi, volume, iv = option_values(row, option_type)
    else:
        ltp, change, oi, chg_oi, volume, iv = 0.0, 0.0, 0, 0, 0, 0.0
    return {
        "side": side,
        "strike": strike,
        "type": option_type,
        "expiry": expiry.isoformat(),
        "ltp": round(ltp, 2),
        "change": round(change, 2),
        "oi": oi,
        "chg_oi": chg_oi,
        "volume": volume,
        "iv": round(iv, 2),
    }


def build_legs(strategy: str, spot: float, expiry: date, rows: list[OptionRow], zones: dict[str, object], settings: dict[str, object]) -> tuple[list[dict[str, object]], str]:
    if not rows:
        return [], "No option rows available for selected expiry."
    index = row_index(rows)
    wing = safe_int(settings.get("wing"), 300)
    buffer = safe_int(settings.get("buffer"), 180)
    puts = zones.get("puts", []) if isinstance(zones.get("puts"), list) else []
    calls = zones.get("calls", []) if isinstance(zones.get("calls"), list) else []

    if strategy == "Bull Put Spread":
        sell = pick_zone(puts, buffer, safe_number(settings.get("min_credit")) * 0.45)
        if not sell:
            return [], "No put writer zone qualified."
        sell_pe = safe_int(sell.get("strike"))
        buy_pe = nearest_strike(sell_pe - wing, rows)
        return [
            leg_dict("SELL", index.get(sell_pe), sell_pe, "PE", expiry),
            leg_dict("BUY", index.get(buy_pe), buy_pe, "PE", expiry),
        ], f"Put writers dominate near {sell_pe}; defined-risk bullish spread selected."

    if strategy == "Bear Call Spread":
        sell = pick_zone(calls, buffer, safe_number(settings.get("min_credit")) * 0.45)
        if not sell:
            return [], "No call writer zone qualified."
        sell_ce = safe_int(sell.get("strike"))
        buy_ce = nearest_strike(sell_ce + wing, rows)
        return [
            leg_dict("SELL", index.get(sell_ce), sell_ce, "CE", expiry),
            leg_dict("BUY", index.get(buy_ce), buy_ce, "CE", expiry),
        ], f"Call writers dominate near {sell_ce}; defined-risk bearish spread selected."

    if strategy == "Expiry Intraday Iron Fly":
        atm = nearest_strike(round(spot / 50) * 50, rows)
        buy_pe = nearest_strike(atm - wing, rows)
        buy_ce = nearest_strike(atm + wing, rows)
        return [
            leg_dict("SELL", index.get(atm), atm, "PE", expiry),
            leg_dict("SELL", index.get(atm), atm, "CE", expiry),
            leg_dict("BUY", index.get(buy_pe), buy_pe, "PE", expiry),
            leg_dict("BUY", index.get(buy_ce), buy_ce, "CE", expiry),
        ], f"Expiry pin setup around ATM {atm}; only valid after intraday movement compresses."

    sell_put = pick_zone(puts, buffer, 10.0)
    sell_call = pick_zone(calls, buffer, 10.0)
    if not sell_put or not sell_call:
        return [], "Both put and call writer zones are required for an iron condor."
    sell_pe = safe_int(sell_put.get("strike"))
    sell_ce = safe_int(sell_call.get("strike"))
    buy_pe = nearest_strike(sell_pe - wing, rows)
    buy_ce = nearest_strike(sell_ce + wing, rows)
    return [
        leg_dict("SELL", index.get(sell_pe), sell_pe, "PE", expiry),
        leg_dict("SELL", index.get(sell_ce), sell_ce, "CE", expiry),
        leg_dict("BUY", index.get(buy_pe), buy_pe, "PE", expiry),
        leg_dict("BUY", index.get(buy_ce), buy_ce, "CE", expiry),
    ], f"Balanced writer range selected from {sell_pe} to {sell_ce}."


def evaluate_trade(strategy: str, spot: float, expiry: date, rows: list[OptionRow], zones: dict[str, object], source: str, regime: dict[str, object]) -> dict[str, object]:
    settings = strategy_settings(strategy, (expiry - today_ist()).days)
    legs, basis = build_legs(strategy, spot, expiry, rows, zones, settings)
    credit = sum(safe_number(item.get("ltp")) for item in legs if item.get("side") == "SELL") - sum(safe_number(item.get("ltp")) for item in legs if item.get("side") == "BUY")
    wing = safe_int(settings.get("wing"), 300)
    lot_size = NIFTY_LOT_SIZE
    max_risk_per_lot = max(0.0, (wing - credit) * lot_size)
    target_per_lot = credit * lot_size * safe_number(settings.get("target_capture"))
    stop_per_lot = credit * lot_size * safe_number(settings.get("stop_multiple"))
    reward_to_risk = target_per_lot / max_risk_per_lot if max_risk_per_lot > 0 else 0.0
    risk_budget = safe_number(settings.get("capital")) * safe_number(settings.get("risk_pct"))
    possible_lots = 0 if max_risk_per_lot <= 0 else min(safe_int(settings.get("max_lots")), int(risk_budget // max_risk_per_lot))

    reasons: list[str] = []
    if not legs:
        reasons.append(basis)
    if any(safe_number(item.get("ltp")) <= 0 for item in legs):
        reasons.append("One or more leg prices are missing.")
    if credit < safe_number(settings.get("min_credit")):
        reasons.append(f"Credit {credit:.2f} is below required {safe_number(settings.get('min_credit')):.2f}.")
    if target_per_lot < safe_number(settings.get("min_target")):
        reasons.append(f"Target per lot INR {target_per_lot:.0f} is below required INR {safe_number(settings.get('min_target')):.0f}.")
    if reward_to_risk < safe_number(settings.get("min_rr")):
        reasons.append(f"Target/risk {reward_to_risk * 100:.1f}% is below required {safe_number(settings.get('min_rr')) * 100:.1f}%.")
    if possible_lots <= 0:
        reasons.append("Risk budget does not support one lot.")
    if regime.get("name") == "NO TRADE":
        reasons.append(str(regime.get("reason")))

    trade_ok = not reasons and source in ("NSE", "NSE_EOD")
    shown_lots = possible_lots if trade_ok else 1
    if trade_ok and source == "NSE":
        action_prefix = "TRADE CANDIDATE"
    elif trade_ok:
        action_prefix = "VERIFY LIVE PLAN"
    else:
        action_prefix = "NO TRADE"

    trade_text = "No executable trade."
    if legs:
        sell_text = " + ".join(f"SELL {shown_lots} lot {item['strike']} {item['type']}" for item in legs if item.get("side") == "SELL")
        buy_text = " + ".join(f"BUY {shown_lots} lot {item['strike']} {item['type']}" for item in legs if item.get("side") == "BUY")
        trade_text = f"{sell_text}; hedge with {buy_text}"
        if source == "NSE_EOD" and trade_ok:
            trade_text = f"Plan from EOD data - verify Zerodha live LTP first: {trade_text}"

    confidence = max(0, min(95, int(safe_number(regime.get("score")) + min(credit / max(wing, 1), 0.40) * 35 - len(reasons) * 13)))
    return {
        "strategy": strategy,
        "decision": action_prefix,
        "trade_ok": trade_ok,
        "trade_text": trade_text,
        "legs": legs,
        "basis": basis,
        "reason": "; ".join(reasons) if reasons else f"{strategy} fits the current regime. {regime.get('reason')}",
        "net_credit": round(credit, 2),
        "max_risk": round(max_risk_per_lot * shown_lots, 0),
        "target": round(target_per_lot * shown_lots, 0),
        "stop": round(stop_per_lot * shown_lots, 0),
        "reward_to_risk_pct": round(reward_to_risk * 100, 1),
        "suggested_lots": possible_lots if trade_ok else 0,
        "shown_lots": shown_lots,
        "confidence": confidence,
        "source": source,
        "invalidation": invalidation_text(strategy),
        "entry": entry_text(strategy, source),
    }


def invalidation_text(strategy: str) -> str:
    if strategy == "Bull Put Spread":
        return "Invalid if spot trades within 120-150 points of short PE, PE OI unwinds, or market breadth turns sharply negative."
    if strategy == "Bear Call Spread":
        return "Invalid if spot trades within 120-150 points of short CE, CE OI unwinds, or market breadth turns sharply positive."
    if strategy == "Expiry Intraday Iron Fly":
        return "Invalid if first hour range expands, spot trends away from ATM, or combined premium expands past stop."
    return "Invalid if spot nears either short strike, either writer wall unwinds, or net credit falls below the hard gate."


def entry_text(strategy: str, source: str) -> str:
    prefix = "For EOD source, verify live LTP in Zerodha first. " if source == "NSE_EOD" else ""
    if strategy == "Expiry Intraday Iron Fly":
        return prefix + "Enter only after movement compresses, typically after the first 45-75 minutes on expiry day."
    return prefix + "Enter after the first 20-45 minutes only if spot remains away from the short strike and OI buildup continues."


def preferred_strategy(regime: dict[str, object], dte: int) -> str:
    name = str(regime.get("name"))
    if dte <= 0 and name == "EXPIRY PIN RISK":
        return "Expiry Intraday Iron Fly"
    if name == "BULLISH RANGE":
        return "Bull Put Spread"
    if name == "BEARISH RANGE":
        return "Bear Call Spread"
    if name == "RANGE":
        return "Weekly Iron Condor"
    return "Weekly Iron Condor"


def build_strategy_engine(spot: float, expiry: date, rows: list[OptionRow], source: str, participant: dict[str, object] | None) -> dict[str, object]:
    zones = build_writer_map(rows, spot, expiry)
    dte = (expiry - today_ist()).days
    regime = infer_regime(spot, zones, participant, dte)
    strategies = ["Bull Put Spread", "Bear Call Spread", "Weekly Iron Condor"]
    if dte <= 1:
        strategies.append("Expiry Intraday Iron Fly")
    if dte >= 10:
        strategies.append("Monthly Wide Iron Condor")

    preferred = preferred_strategy(regime, dte)
    ordered = [preferred] + [item for item in strategies if item != preferred]
    candidates = [evaluate_trade(item, spot, expiry, rows, zones, source, regime) for item in ordered]
    executable = [item for item in candidates if item.get("trade_ok")]
    recommended = executable[0] if executable else candidates[0]
    if not executable:
        recommended = dict(recommended)
        recommended["decision"] = "NO TRADE"
        recommended["reason"] = recommended.get("reason") or "No strategy passed hard gates."
    return {
        "expiry": expiry.isoformat(),
        "dte": dte,
        "regime": regime,
        "zones": zones,
        "recommended": recommended,
        "candidates": candidates,
    }


def unavailable_board(error: str) -> dict[str, object]:
    engine = {
        "expiry": "-",
        "dte": 0,
        "regime": {"name": "NO DATA", "bias": "No real NSE source", "score": 0, "reason": error},
        "zones": {"puts": [], "calls": [], "writer_range": "Unavailable", "pcr": 0, "bias": "Unavailable"},
        "recommended": {"decision": "NO TRADE", "strategy": "No Trade", "trade_text": "No real NSE source is available.", "legs": [], "reason": error, "confidence": 0, "net_credit": 0, "max_risk": 0, "target": 0, "stop": 0, "reward_to_risk_pct": 0, "entry": "Wait for real data.", "invalidation": "No data.", "trade_ok": False},
        "candidates": [],
    }
    return {
        "source": "UNAVAILABLE",
        "data_ok": False,
        "status": f"NSE live and bhavcopy sources failed. {error}",
        "spot": 0,
        "as_of": "-",
        "server_refreshed_at": now_ist_label(),
        "cache_seconds": CACHE_SECONDS,
        "lot_size": NIFTY_LOT_SIZE,
        "participant": None,
        "engine": engine,
        "stale": False,
    }


def load_market_data() -> tuple[str, str, float, list[date], list[OptionRow], str]:
    try:
        payload = fetch_nse_option_chain()
        spot, timestamp, expiries, rows = parse_chain(payload)
        if spot <= 0 or not rows:
            raise ValueError("NSE option chain returned no usable rows")
        return "NSE", "Live NSE option-chain data.", spot, expiries, rows, timestamp
    except Exception as live_error:  # noqa: BLE001 - NSE frequently blocks hosted traffic
        spot, timestamp, expiries, rows = fetch_bhavcopy_option_chain()
        status = f"Live option-chain unavailable; using latest real NSE F&O bhavcopy. Verify live Zerodha prices before entry. Live error: {live_error}"
        return "NSE_EOD", status, spot, expiries, rows, timestamp


def load_action_board_uncached() -> dict[str, object]:
    try:
        source, status, spot, expiries, rows, timestamp = load_market_data()
    except Exception as error:  # noqa: BLE001
        stale = _CACHE.get("board")
        if isinstance(stale, dict) and stale.get("data_ok"):
            copy = dict(stale)
            copy["stale"] = True
            copy["status"] = f"Refresh failed, showing last cached real board. Error: {error}"
            copy["server_refreshed_at"] = now_ist_label()
            return copy
        return unavailable_board(str(error))

    weekly_expiry, _monthly_expiry = choose_expiries(expiries, rows, today_ist())
    weekly_rows = rows_for_expiry(rows, weekly_expiry)
    participant = fetch_participant_oi()
    engine = build_strategy_engine(spot, weekly_expiry, weekly_rows, source, participant)
    return {
        "source": source,
        "data_ok": True,
        "status": status,
        "spot": round(spot, 2),
        "as_of": timestamp,
        "server_refreshed_at": now_ist_label(),
        "cache_seconds": CACHE_SECONDS,
        "lot_size": NIFTY_LOT_SIZE,
        "participant": participant,
        "engine": engine,
        "stale": False,
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
  <title>NIFTY Strategy Engine</title>
  <style>
    :root { --ink:#172026; --muted:#5c6670; --line:#d7dee4; --page:#f4f7f5; --panel:#fff; --green:#08744f; --red:#b42318; --amber:#a15c07; --blue:#0e7490; --dark:#101820; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; color:var(--ink); background:var(--page); }
    header { background:#fbfcfb; border-bottom:1px solid var(--line); }
    .wrap { width:min(1240px, calc(100vw - 32px)); margin:0 auto; }
    .topbar { display:flex; justify-content:space-between; align-items:center; gap:18px; padding:16px 0; }
    .brand { display:flex; align-items:center; gap:12px; min-width:0; }
    .mark { width:38px; height:38px; border:2px solid var(--green); display:grid; place-items:center; font-weight:800; color:var(--green); }
    h1 { margin:0; font-size:20px; line-height:1.2; }
    h2 { margin:0 0 12px; font-size:16px; }
    h3 { margin:0 0 5px; font-size:15px; }
    .sub, .muted { color:var(--muted); font-size:13px; }
    .status { display:flex; align-items:center; gap:9px; max-width:680px; padding:8px 11px; border:1px solid var(--line); background:#fff; font-size:13px; line-height:1.35; }
    .dot { width:9px; height:9px; border-radius:99px; background:var(--green); flex:0 0 auto; }
    .dot.bad { background:var(--red); }
    main { padding:24px 0 40px; }
    .kpis { display:grid; grid-template-columns:repeat(5, minmax(0, 1fr)); gap:10px; margin-bottom:16px; }
    .kpi, .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; }
    .kpi { padding:13px; min-height:86px; }
    .kpi b { display:block; font-size:20px; margin-bottom:5px; }
    .grid { display:grid; grid-template-columns:1.1fr .9fr; gap:16px; align-items:start; }
    .section { padding:17px; }
    .section + .section { border-top:1px solid var(--line); }
    .decision { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:12px; }
    .tile, .metric { border:1px solid var(--line); border-radius:8px; padding:12px; background:#fff; }
    .tile b { display:block; font-size:18px; margin-bottom:4px; }
    .trade { padding:13px; border-radius:8px; background:var(--dark); color:#eef7f3; line-height:1.55; font-family:Consolas, ui-monospace, monospace; font-size:13px; margin-bottom:12px; }
    .badge { border:1px solid var(--line); padding:7px 9px; font-size:12px; font-weight:750; white-space:nowrap; color:var(--green); background:#edf8f2; }
    .badge.no { color:var(--amber); background:#fff7e6; }
    .head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px; }
    .metrics { display:grid; grid-template-columns:repeat(5, minmax(0, 1fr)); gap:8px; margin-bottom:12px; }
    .metric { min-height:66px; }
    .metric b { display:block; font-size:15px; margin-bottom:3px; }
    .metric span { color:var(--muted); font-size:12px; }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th, td { text-align:left; padding:8px 7px; border-bottom:1px solid var(--line); vertical-align:top; }
    th { color:var(--muted); font-weight:700; }
    .sell { color:var(--red); font-weight:800; }
    .buy { color:var(--green); font-weight:800; }
    .notes { display:grid; gap:8px; margin-top:10px; }
    .note { border-left:3px solid var(--blue); background:#f8fbfb; padding:8px 10px; font-size:13px; line-height:1.45; color:#34424d; }
    .note.warn { border-color:var(--amber); background:#fff9ee; }
    .zone-wrap { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
    footer { padding:18px 0 30px; color:var(--muted); font-size:12px; }
    @media (max-width:1050px) { .grid, .kpis, .zone-wrap, .metrics, .decision { grid-template-columns:1fr; } .topbar, .head { flex-direction:column; align-items:flex-start; } .status { max-width:none; } }
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <div class="brand"><div class="mark">N</div><div><h1>NIFTY Strategy Engine</h1><div class="sub">Writer map first, defined-risk trade second</div></div></div>
      <div class="status"><span class="dot {% if not board.data_ok %}bad{% endif %}"></span>{{ board.status }}</div>
    </div>
  </header>

  <main class="wrap">
    <div class="kpis">
      <div class="kpi"><b>{{ "%.2f"|format(board.spot) }}</b><span class="muted">NIFTY spot/future proxy</span></div>
      <div class="kpi"><b>{{ board.engine.regime.name }}</b><span class="muted">Market regime</span></div>
      <div class="kpi"><b>{{ board.engine.recommended.strategy }}</b><span class="muted">Selected strategy</span></div>
      <div class="kpi"><b>{{ board.engine.zones.writer_range }}</b><span class="muted">Writer range</span></div>
      <div class="kpi"><b>{{ board.source }}</b><span class="muted">Data source</span></div>
    </div>

    <div class="grid">
      <div class="panel">
        <section class="section">
          <h2>Strategy Decision</h2>
          <div class="decision">
            <div class="tile"><b>{{ board.engine.regime.bias }}</b><span class="muted">Bias</span></div>
            <div class="tile"><b>{{ board.engine.regime.score }}</b><span class="muted">Regime score</span></div>
          </div>
          <div class="note">{{ board.engine.regime.reason }}</div>
        </section>

        <section class="section">
          <div class="head">
            <div><h2>Recommended Trade</h2><div class="muted">Expiry {{ board.engine.expiry }} | DTE {{ board.engine.dte }} | Lot size {{ board.lot_size }}</div></div>
            <div class="badge {% if not board.engine.recommended.trade_ok %}no{% endif %}">{{ board.engine.recommended.decision }}</div>
          </div>
          <div class="trade">{{ board.engine.recommended.trade_text }}</div>
          <div class="metrics">
            <div class="metric"><b>{{ board.engine.recommended.net_credit }}</b><span>Credit / unit</span></div>
            <div class="metric"><b>INR {{ "{:,.0f}".format(board.engine.recommended.max_risk) }}</b><span>Max risk shown</span></div>
            <div class="metric"><b>INR {{ "{:,.0f}".format(board.engine.recommended.target) }}</b><span>Target shown</span></div>
            <div class="metric"><b>{{ board.engine.recommended.reward_to_risk_pct }}%</b><span>Target / risk</span></div>
            <div class="metric"><b>{{ board.engine.recommended.confidence }}</b><span>Confidence</span></div>
          </div>
          {% if board.engine.recommended.legs %}
          <table>
            <tr><th>Side</th><th>Strike</th><th>Type</th><th>LTP</th><th>OI</th><th>Chg OI</th><th>Volume</th></tr>
            {% for leg in board.engine.recommended.legs %}
            <tr><td class="{{ leg.side|lower }}">{{ leg.side }}</td><td>{{ leg.strike }}</td><td>{{ leg.type }}</td><td>{{ leg.ltp }}</td><td>{{ "{:,.0f}".format(leg.oi) }}</td><td>{{ "{:,.0f}".format(leg.chg_oi) }}</td><td>{{ "{:,.0f}".format(leg.volume) }}</td></tr>
            {% endfor %}
          </table>
          {% endif %}
          <div class="notes">
            <div class="note">Why: {{ board.engine.recommended.reason }}</div>
            <div class="note">Entry: {{ board.engine.recommended.entry }}</div>
            <div class="note warn">Invalidation: {{ board.engine.recommended.invalidation }}</div>
          </div>
        </section>

        <section class="section">
          <h2>Alternative Strategies</h2>
          <table>
            <tr><th>Strategy</th><th>Decision</th><th>Credit</th><th>Risk</th><th>Target</th><th>Target/Risk</th></tr>
            {% for item in board.engine.candidates %}
            <tr><td>{{ item.strategy }}</td><td>{{ item.decision }}</td><td>{{ item.net_credit }}</td><td>INR {{ "{:,.0f}".format(item.max_risk) }}</td><td>INR {{ "{:,.0f}".format(item.target) }}</td><td>{{ item.reward_to_risk_pct }}%</td></tr>
            {% else %}
            <tr><td colspan="6">No alternatives without data.</td></tr>
            {% endfor %}
          </table>
        </section>
      </div>

      <aside class="panel">
        <section class="section">
          <h2>Where Writers Are Short</h2>
          <div class="zone-wrap">
            <div>
              <h3>Put Writers / Support</h3>
              <table>
                <tr><th>Strike</th><th>Signal</th><th>OI</th><th>Chg OI</th><th>LTP</th><th>Score</th></tr>
                {% for zone in board.engine.zones.puts %}
                <tr><td>{{ zone.strike }}</td><td>{{ zone.signal }}</td><td>{{ "{:,.0f}".format(zone.oi) }}</td><td>{{ "{:,.0f}".format(zone.chg_oi) }}</td><td>{{ zone.ltp }}</td><td>{{ zone.score }}</td></tr>
                {% else %}
                <tr><td colspan="6">No PE writer zones.</td></tr>
                {% endfor %}
              </table>
            </div>
            <div>
              <h3>Call Writers / Resistance</h3>
              <table>
                <tr><th>Strike</th><th>Signal</th><th>OI</th><th>Chg OI</th><th>LTP</th><th>Score</th></tr>
                {% for zone in board.engine.zones.calls %}
                <tr><td>{{ zone.strike }}</td><td>{{ zone.signal }}</td><td>{{ "{:,.0f}".format(zone.oi) }}</td><td>{{ "{:,.0f}".format(zone.chg_oi) }}</td><td>{{ zone.ltp }}</td><td>{{ zone.score }}</td></tr>
                {% else %}
                <tr><td colspan="6">No CE writer zones.</td></tr>
                {% endfor %}
              </table>
            </div>
          </div>
        </section>

        <section class="section">
          <h2>Participant Bias</h2>
          {% if board.participant %}
            <div class="note">{{ board.participant.summary.bias }} | Date {{ board.participant.date }}. {{ board.participant.note }}</div>
            <table>
              <tr><th>Type</th><th>Fut Net</th><th>Call Short</th><th>Put Short</th></tr>
              {% for row in board.participant.rows %}
              <tr><td>{{ row.client_type }}</td><td>{{ "{:,.0f}".format(row.future_net) }}</td><td>{{ "{:,.0f}".format(row.call_short) }}</td><td>{{ "{:,.0f}".format(row.put_short) }}</td></tr>
              {% endfor %}
            </table>
          {% else %}
            <div class="note warn">Participant OI archive was not available during this refresh.</div>
          {% endif %}
        </section>
      </aside>
    </div>
  </main>

  <footer class="wrap">Research software only. This is not investment advice or an order instruction. Verify live Zerodha prices, margin, spreads, event risk, and execution before placing any trade.</footer>
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
