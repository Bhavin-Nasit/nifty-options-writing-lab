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
CONFIG_DIR = ROOT / 'configs'
IST = ZoneInfo('Asia/Kolkata')
NSE_BASE_URL = 'https://www.nseindia.com'
NSE_CHAIN_URL = f'{NSE_BASE_URL}/api/option-chain-indices?symbol=NIFTY'
NSE_CHAIN_PAGE = f'{NSE_BASE_URL}/option-chain'
NSE_PARTICIPANT_URL = 'https://archives.nseindia.com/content/nsccl/fao_participant_oi_{stamp}.csv'
NSE_BHAVCOPY_URL = 'https://archives.nseindia.com/content/historical/DERIVATIVES/{year}/{month}/fo{stamp}bhav.csv.zip'
CACHE_SECONDS = int(os.getenv('NSE_CACHE_SECONDS', '900'))
NIFTY_LOT_SIZE = int(os.getenv('NIFTY_LOT_SIZE', '65'))

app = Flask(__name__)
_CACHE: dict[str, object] = {'expires_at': datetime.min.replace(tzinfo=IST), 'board': None}


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
    return now_ist().strftime('%d %b %Y, %I:%M %p IST')


def floor_to_step(value: float, step: int = 50) -> int:
    return int(math.floor(value / step) * step)


def ceil_to_step(value: float, step: int = 50) -> int:
    return int(math.ceil(value / step) * step)


def clean_cell(value: object) -> str:
    return str(value or '').strip().replace('\ufeff', '')


def parse_nse_expiry(value: str) -> date:
    return datetime.strptime(value, '%d-%b-%Y').date()


def parse_bhavcopy_expiry(value: object) -> date:
    return datetime.strptime(clean_cell(value).title(), '%d-%b-%Y').date()


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
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
        'Accept': 'application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': NSE_CHAIN_PAGE,
        'Connection': 'keep-alive',
    }


def safe_number(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, '-', ''):
            return default
        return float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return default


def safe_int(value: object, default: int = 0) -> int:
    return int(safe_number(value, float(default)))


def fetch_nse_option_chain() -> dict[str, object]:
    session = requests.Session()
    session.headers.update(nse_headers())
    session.get(NSE_BASE_URL, timeout=12)
    session.get(NSE_CHAIN_PAGE, timeout=12)
    response = session.get(NSE_CHAIN_URL, timeout=20)
    response.raise_for_status()
    return response.json()


def parse_chain(payload: dict[str, object]) -> tuple[float, str, list[date], list[OptionRow]]:
    records = payload.get('records', {}) if isinstance(payload, dict) else {}
    filtered = payload.get('filtered', {}) if isinstance(payload, dict) else {}
    spot = safe_number(records.get('underlyingValue'), 0.0) if isinstance(records, dict) else 0.0
    if spot <= 0 and isinstance(filtered, dict):
        spot = safe_number(filtered.get('underlyingValue'), 0.0)
    timestamp = str(records.get('timestamp') or filtered.get('timestamp') or now_ist_label()) if isinstance(records, dict) else now_ist_label()
    expiry_values = records.get('expiryDates', []) if isinstance(records, dict) else []
    expiries: list[date] = []
    for value in expiry_values:
        try:
            expiries.append(parse_nse_expiry(str(value)))
        except ValueError:
            continue
    expiries = sorted(expiries)

    raw_rows = records.get('data', []) if isinstance(records, dict) else []
    if not raw_rows and isinstance(filtered, dict):
        raw_rows = filtered.get('data', [])
    rows: list[OptionRow] = []
    for item in raw_rows:
        if not isinstance(item, dict) or 'expiryDate' not in item:
            continue
        try:
            expiry = parse_nse_expiry(str(item['expiryDate']))
            strike = safe_int(item.get('strikePrice'))
            ce = item.get('CE') if isinstance(item.get('CE'), dict) else {}
            pe = item.get('PE') if isinstance(item.get('PE'), dict) else {}
            rows.append(
                OptionRow(
                    expiry=expiry,
                    strike=strike,
                    ce_ltp=safe_number(ce.get('lastPrice')),
                    pe_ltp=safe_number(pe.get('lastPrice')),
                    ce_change=safe_number(ce.get('change')),
                    pe_change=safe_number(pe.get('change')),
                    ce_oi=safe_int(ce.get('openInterest')),
                    pe_oi=safe_int(pe.get('openInterest')),
                    ce_chg_oi=safe_int(ce.get('changeinOpenInterest')),
                    pe_chg_oi=safe_int(pe.get('changeinOpenInterest')),
                    ce_volume=safe_int(ce.get('totalTradedVolume')),
                    pe_volume=safe_int(pe.get('totalTradedVolume')),
                    ce_iv=safe_number(ce.get('impliedVolatility')),
                    pe_iv=safe_number(pe.get('impliedVolatility')),
                )
            )
        except (TypeError, ValueError):
            continue
    return spot, timestamp, expiries, rows


def fetch_bhavcopy_for_day(session: requests.Session, day: date) -> tuple[float, str, list[date], list[OptionRow]] | None:
    stamp = day.strftime('%d%b%Y').upper()
    url = NSE_BHAVCOPY_URL.format(year=day.year, month=day.strftime('%b').upper(), stamp=stamp)
    response = session.get(url, timeout=20)
    if response.status_code != 200 or not response.content:
        return None

    grouped: dict[tuple[date, int], dict[str, dict[str, float]]] = {}
    futures: list[tuple[date, float]] = []
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        csv_name = next((name for name in archive.namelist() if name.lower().endswith('.csv')), None)
        if not csv_name:
            return None
        with archive.open(csv_name) as handle:
            reader = csv.DictReader(io.TextIOWrapper(handle, encoding='utf-8', errors='ignore'))
            for item in reader:
                if clean_cell(item.get('SYMBOL')).upper() != 'NIFTY':
                    continue
                instrument = clean_cell(item.get('INSTRUMENT')).upper()
                try:
                    expiry = parse_bhavcopy_expiry(item.get('EXPIRY_DT'))
                except ValueError:
                    continue
                if instrument == 'FUTIDX':
                    close = safe_number(item.get('CLOSE'))
                    if close > 0:
                        futures.append((expiry, close))
                    continue
                if instrument != 'OPTIDX':
                    continue
                option_type = clean_cell(item.get('OPTION_TYP')).upper()
                if option_type not in ('CE', 'PE'):
                    continue
                strike = safe_int(item.get('STRIKE_PR'))
                if strike <= 0:
                    continue
                key = (expiry, strike)
                grouped.setdefault(key, {})[option_type] = {
                    'ltp': safe_number(item.get('CLOSE')),
                    'oi': safe_number(item.get('OPEN_INT')),
                    'chg_oi': safe_number(item.get('CHG_IN_OI')),
                    'volume': safe_number(item.get('CONTRACTS')),
                }

    rows: list[OptionRow] = []
    for key, legs in grouped.items():
        expiry, strike = key
        ce = legs.get('CE', {})
        pe = legs.get('PE', {})
        rows.append(
            OptionRow(
                expiry=expiry,
                strike=strike,
                ce_ltp=safe_number(ce.get('ltp')),
                pe_ltp=safe_number(pe.get('ltp')),
                ce_change=0.0,
                pe_change=0.0,
                ce_oi=safe_int(ce.get('oi')),
                pe_oi=safe_int(pe.get('oi')),
                ce_chg_oi=safe_int(ce.get('chg_oi')),
                pe_chg_oi=safe_int(pe.get('chg_oi')),
                ce_volume=safe_int(ce.get('volume')),
                pe_volume=safe_int(pe.get('volume')),
                ce_iv=0.0,
                pe_iv=0.0,
            )
        )
    if not rows:
        return None

    future_candidates = sorted((item for item in futures if item[0] >= day and item[1] > 0), key=lambda item: item[0])
    spot = future_candidates[0][1] if future_candidates else 0.0
    if spot <= 0:
        active = max(rows, key=lambda row: row.ce_oi + row.pe_oi)
        spot = float(active.strike)
    expiries = sorted({row.expiry for row in rows if row.expiry >= day})
    day_label = day.strftime('%d %b %Y')
    timestamp = f'{day_label} EOD F&O bhavcopy'
    return spot, timestamp, expiries, rows


def fetch_bhavcopy_option_chain() -> tuple[float, str, list[date], list[OptionRow]]:
    session = requests.Session()
    session.headers.update(nse_headers())
    for offset in range(0, 14):
        day = today_ist() - timedelta(days=offset)
        try:
            result = fetch_bhavcopy_for_day(session, day)
            if result:
                return result
        except (requests.RequestException, zipfile.BadZipFile, OSError):
            continue
    raise ValueError('NSE F&O bhavcopy fallback returned no usable rows')


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


def zone_values(row: OptionRow, option_type: str) -> tuple[float, float, int, int, int, float]:
    if option_type == 'PE':
        return row.pe_ltp, row.pe_change, row.pe_oi, row.pe_chg_oi, row.pe_volume, row.pe_iv
    return row.ce_ltp, row.ce_change, row.ce_oi, row.ce_chg_oi, row.ce_volume, row.ce_iv


def metric_max(values: list[dict[str, object]], key: str) -> float:
    value = max((safe_number(item.get(key)) for item in values), default=0.0)
    return value if value > 0 else 1.0


def build_writing_zones(rows: list[OptionRow], option_type: str, spot: float) -> list[dict[str, object]]:
    raw: list[dict[str, object]] = []
    for row in rows:
        distance = spot - row.strike if option_type == 'PE' else row.strike - spot
        if distance < 0:
            continue
        ltp, change, oi, chg_oi, volume, iv = zone_values(row, option_type)
        if oi <= 0 or ltp <= 0:
            continue
        raw.append(
            {
                'strike': row.strike,
                'option_type': option_type,
                'ltp': round(ltp, 2),
                'change': round(change, 2),
                'oi': oi,
                'chg_oi': chg_oi,
                'volume': volume,
                'iv': round(iv, 2),
                'distance': round(distance, 0),
            }
        )

    if not raw:
        return []

    max_oi = metric_max(raw, 'oi')
    max_chg = metric_max(raw, 'chg_oi')
    max_volume = metric_max(raw, 'volume')
    max_ltp = metric_max(raw, 'ltp')
    max_distance = metric_max(raw, 'distance')
    zones: list[dict[str, object]] = []
    for item in raw:
        chg_oi = safe_number(item.get('chg_oi'))
        change = safe_number(item.get('change'))
        if chg_oi > 0 and change <= 0:
            signal = 'Fresh short buildup likely'
            confidence = 'High'
            writing_boost = 1.0
        elif chg_oi > 0:
            signal = 'Fresh OI buildup'
            confidence = 'Medium'
            writing_boost = 0.65
        else:
            signal = 'Existing OI wall'
            confidence = 'Lower'
            writing_boost = 0.25
        distance_score = 1.0 - min(safe_number(item.get('distance')) / max_distance, 1.0)
        score = 100 * (
            0.34 * safe_number(item.get('oi')) / max_oi
            + 0.24 * max(chg_oi, 0.0) / max_chg
            + 0.14 * safe_number(item.get('volume')) / max_volume
            + 0.10 * safe_number(item.get('ltp')) / max_ltp
            + 0.12 * writing_boost
            + 0.06 * distance_score
        )
        item['score'] = round(score, 1)
        item['signal'] = signal
        item['confidence'] = confidence
        zones.append(item)
    return sorted(zones, key=lambda item: (safe_number(item.get('score')), safe_number(item.get('chg_oi')), safe_number(item.get('oi'))), reverse=True)


def build_writer_map(rows: list[OptionRow], spot: float, expiry: date) -> dict[str, object]:
    puts = build_writing_zones(rows, 'PE', spot)
    calls = build_writing_zones(rows, 'CE', spot)
    total_pe_oi = sum(row.pe_oi for row in rows)
    total_ce_oi = sum(row.ce_oi for row in rows)
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0.0
    support = puts[0]['strike'] if puts else None
    resistance = calls[0]['strike'] if calls else None
    if support and resistance:
        writer_range = f'{support} to {resistance}'
    else:
        writer_range = 'Not enough live OI'
    if pcr >= 1.2:
        bias = 'Put writers heavier'
    elif pcr <= 0.8 and pcr > 0:
        bias = 'Call writers heavier'
    else:
        bias = 'Balanced writers'
    return {
        'expiry': expiry.isoformat(),
        'puts': puts[:8],
        'calls': calls[:8],
        'support': support,
        'resistance': resistance,
        'writer_range': writer_range,
        'pcr': pcr,
        'bias': bias,
        'total_pe_oi': total_pe_oi,
        'total_ce_oi': total_ce_oi,
    }


def parse_participant_oi(text: str, day: date) -> dict[str, object] | None:
    rows = list(csv.reader(io.StringIO(text)))
    header_index = None
    for index, row in enumerate(rows):
        if row and clean_cell(row[0]).lower() == 'client type':
            header_index = index
            break
    if header_index is None:
        return None
    headers = [clean_cell(cell) for cell in rows[header_index]]
    lookup = {name: pos for pos, name in enumerate(headers)}

    def get_value(row: list[str], name: str) -> int:
        pos = lookup.get(name)
        if pos is None or pos >= len(row):
            return 0
        return safe_int(row[pos])

    parsed: dict[str, dict[str, int]] = {}
    for row in rows[header_index + 1:]:
        if not row:
            continue
        label = clean_cell(row[0]).upper()
        if label in ('CLIENT', 'DII', 'FII', 'PRO'):
            parsed[label] = {
                'future_index_long': get_value(row, 'Future Index Long'),
                'future_index_short': get_value(row, 'Future Index Short'),
                'call_long': get_value(row, 'Option Index Call Long'),
                'call_short': get_value(row, 'Option Index Call Short'),
                'put_long': get_value(row, 'Option Index Put Long'),
                'put_short': get_value(row, 'Option Index Put Short'),
            }
    if not parsed:
        return None

    rows_out: list[dict[str, object]] = []
    for label in ('FII', 'PRO', 'CLIENT', 'DII'):
        item = parsed.get(label)
        if not item:
            continue
        rows_out.append(
            {
                'client_type': label,
                'future_net': item['future_index_long'] - item['future_index_short'],
                'call_short': item['call_short'],
                'put_short': item['put_short'],
                'call_net': item['call_long'] - item['call_short'],
                'put_net': item['put_long'] - item['put_short'],
            }
        )

    smart_call_short = sum(parsed.get(label, {}).get('call_short', 0) for label in ('FII', 'PRO'))
    smart_put_short = sum(parsed.get(label, {}).get('put_short', 0) for label in ('FII', 'PRO'))
    smart_future_net = sum(parsed.get(label, {}).get('future_index_long', 0) - parsed.get(label, {}).get('future_index_short', 0) for label in ('FII', 'PRO'))
    if smart_call_short > smart_put_short * 1.15:
        bias = 'FII + PRO index call shorts heavier'
    elif smart_put_short > smart_call_short * 1.15:
        bias = 'FII + PRO index put shorts heavier'
    else:
        bias = 'FII + PRO index option shorts balanced'
    return {
        'date': day.isoformat(),
        'rows': rows_out,
        'summary': {
            'smart_call_short': smart_call_short,
            'smart_put_short': smart_put_short,
            'smart_future_net': smart_future_net,
            'bias': bias,
        },
        'note': 'Participant OI is EOD and category-level across index derivatives. It is not strike-specific.',
    }


def fetch_participant_oi() -> dict[str, object] | None:
    session = requests.Session()
    session.headers.update(nse_headers())
    for offset in range(0, 12):
        day = today_ist() - timedelta(days=offset)
        stamp = day.strftime('%d%m%Y')
        url = NSE_PARTICIPANT_URL.format(stamp=stamp)
        try:
            response = session.get(url, timeout=12)
            if response.status_code != 200 or 'Client Type' not in response.text:
                continue
            parsed = parse_participant_oi(response.text, day)
            if parsed:
                return parsed
        except requests.RequestException:
            continue
    return None


def build_leg(side: str, row: OptionRow | None, strike: int, option_type: str, expiry: date) -> dict[str, object]:
    if row is None:
        ltp = 0.0
        oi = 0
        volume = 0
        iv = 0.0
    elif option_type == 'PE':
        ltp, oi, volume, iv = row.pe_ltp, row.pe_oi, row.pe_volume, row.pe_iv
    else:
        ltp, oi, volume, iv = row.ce_ltp, row.ce_oi, row.ce_volume, row.ce_iv
    expiry_label = expiry.strftime('%d-%b-%Y').upper()
    return {
        'side': side,
        'strike': strike,
        'type': option_type,
        'expiry': expiry.isoformat(),
        'symbol': f'NIFTY {expiry_label} {strike}{option_type}',
        'ltp': round(float(ltp), 2),
        'oi': int(oi),
        'volume': int(volume),
        'iv': round(float(iv), 2),
    }


def estimate_values(plan: dict[str, object]) -> None:
    legs = plan.get('legs', [])
    if not isinstance(legs, list):
        legs = []
    credit = sum(safe_number(leg.get('ltp')) for leg in legs if isinstance(leg, dict) and leg.get('side') == 'SELL') - sum(
        safe_number(leg.get('ltp')) for leg in legs if isinstance(leg, dict) and leg.get('side') == 'BUY'
    )
    wing = safe_int(plan.get('wing_width'))
    lot_size = safe_int(plan.get('lot_size'), NIFTY_LOT_SIZE)
    max_risk_per_lot = max(0.0, (wing - credit) * lot_size)
    target_per_lot = credit * lot_size * safe_number(plan.get('target_capture'))
    stop_per_lot = credit * lot_size * safe_number(plan.get('stop_multiple'))
    reward_to_risk = 0.0 if max_risk_per_lot <= 0 else target_per_lot / max_risk_per_lot
    risk_budget = safe_number(plan.get('capital')) * safe_number(plan.get('risk_pct'))
    lot_capacity = 0 if max_risk_per_lot <= 0 else min(safe_int(plan.get('max_lots')), int(risk_budget // max_risk_per_lot))

    min_credit = safe_number(plan.get('min_credit'))
    min_target = safe_number(plan.get('min_target_per_lot'))
    min_rr = safe_number(plan.get('min_reward_to_risk'))
    source_ok = plan.get('source') in ('NSE', 'NSE_EOD')
    prices_ok = bool(legs) and all(isinstance(leg, dict) and safe_number(leg.get('ltp')) > 0 for leg in legs)
    credit_ok = credit >= min_credit
    target_ok = target_per_lot >= min_target
    rr_ok = reward_to_risk >= min_rr
    trade_ok = source_ok and prices_ok and credit_ok and target_ok and rr_ok and lot_capacity > 0

    reasons: list[str] = []
    if not source_ok:
        reasons.append('real NSE data is unavailable')
    if not prices_ok:
        reasons.append('one or more leg prices are missing')
    if not credit_ok:
        reasons.append(f'credit {credit:.2f} is below required {min_credit:.2f}')
    if not target_ok:
        reasons.append(f'target per lot INR {target_per_lot:.0f} is below required INR {min_target:.0f}')
    if not rr_ok:
        reasons.append(f'target to max-risk {reward_to_risk * 100:.1f}% is below required {min_rr * 100:.1f}%')
    if lot_capacity <= 0:
        reasons.append('risk budget does not support one lot')

    shown_lots = lot_capacity if trade_ok else 1
    plan['suggested_lots'] = lot_capacity if trade_ok else 0
    plan['net_credit'] = round(credit, 2)
    plan['max_risk'] = round(max_risk_per_lot * shown_lots, 0)
    plan['target_profit'] = round(target_per_lot * shown_lots, 0)
    plan['stop_loss'] = round(stop_per_lot * shown_lots, 0)
    plan['risk_budget'] = round(risk_budget, 0)
    plan['credit_ok'] = credit_ok
    plan['target_ok'] = target_ok
    plan['reward_to_risk'] = round(reward_to_risk, 4)
    plan['reward_to_risk_pct'] = round(reward_to_risk * 100, 1)
    plan['trade_ok'] = trade_ok
    if trade_ok and plan.get('source') == 'NSE_EOD':
        plan['decision'] = 'EOD CANDIDATE'
        plan['decision_reason'] = 'Real NSE EOD bhavcopy candidate. Verify live broker LTP, bid-ask spread, and margin before placing any order.'
    else:
        plan['decision'] = 'TRADE CANDIDATE' if trade_ok else 'NO TRADE'
        plan['decision_reason'] = '; '.join(reasons) if reasons else 'All hard gates passed. Verify broker margin, spreads, and event risk before order entry.'
    plan['metric_scope'] = 'selected lots' if trade_ok else '1-lot rejection view'


def leg_command(prefix: str, leg: dict[str, object], lots: int) -> str:
    strike = leg.get('strike')
    option_type = leg.get('type')
    return f'{prefix} {lots} lot {strike} {option_type}'


def trade_line(plan: dict[str, object]) -> str:
    legs = plan.get('legs', [])
    if not isinstance(legs, list) or not legs:
        return 'NO TRADE. Option rows unavailable for this expiry.'
    lots = safe_int(plan.get('suggested_lots'))
    shown_lots = lots if lots > 0 else 1
    sell_legs = [leg for leg in legs if isinstance(leg, dict) and leg.get('side') == 'SELL']
    buy_legs = [leg for leg in legs if isinstance(leg, dict) and leg.get('side') == 'BUY']
    sell_text = ' + '.join(leg_command('SELL', leg, shown_lots) for leg in sell_legs)
    buy_text = ' + '.join(leg_command('BUY', leg, shown_lots) for leg in buy_legs)
    if lots > 0 and plan.get('source') == 'NSE_EOD':
        return f'EOD CANDIDATE. Verify live broker prices first: {sell_text}; hedge with {buy_text}'
    if lots > 0:
        return f'{sell_text}; hedge with {buy_text}'
    return f'NO TRADE. Watch zones only: {sell_text}; hedge with {buy_text}'


def zone_pool(zones: list[dict[str, object]], min_distance: int, max_distance: int, min_premium: float) -> list[dict[str, object]]:
    pool = [
        zone
        for zone in zones
        if min_distance <= safe_number(zone.get('distance')) <= max_distance and safe_number(zone.get('ltp')) >= min_premium
    ]
    if len(pool) >= 4:
        return pool[:12]
    seen = {safe_int(zone.get('strike')) for zone in pool}
    for zone in zones:
        strike = safe_int(zone.get('strike'))
        if strike in seen:
            continue
        if safe_number(zone.get('distance')) >= min_distance:
            pool.append(zone)
            seen.add(strike)
        if len(pool) >= 12:
            break
    return pool[:12]


def no_trade_plan(kind: str, title: str, expiry: date, source: str, reason: str, params: dict[str, object]) -> dict[str, object]:
    return {
        'kind': kind,
        'title': title,
        'source': source,
        'expiry': expiry.isoformat(),
        'dte': (expiry - today_ist()).days,
        'spot': 0,
        'capital': params['capital'],
        'risk_pct': params['risk_pct'],
        'max_lots': params['max_lots'],
        'lot_size': NIFTY_LOT_SIZE,
        'wing_width': params['wing'],
        'target_capture': params['target_capture'],
        'stop_multiple': params['stop_multiple'],
        'min_credit': params['min_credit'],
        'min_target_per_lot': params['min_target_per_lot'],
        'min_reward_to_risk': params['min_reward_to_risk'],
        'legs': [],
        'entry_filter': params['entry_filter'],
        'invalid_if': params['invalid_if'],
        'exit_plan': params['exit_plan'],
        'selection_reason': reason,
        'suggested_lots': 0,
        'net_credit': 0,
        'max_risk': 0,
        'target_profit': 0,
        'stop_loss': 0,
        'reward_to_risk_pct': 0,
        'metric_scope': 'no live candidate',
        'trade_ok': False,
        'decision': 'NO TRADE',
        'decision_reason': reason,
        'trade_line': 'NO TRADE. No candidate cleared the data screen.',
        'rank_score': 0,
    }


def build_pair_plan(kind: str, title: str, spot: float, expiry: date, rows: list[OptionRow], source: str, pe_zone: dict[str, object], ce_zone: dict[str, object], params: dict[str, object]) -> dict[str, object]:
    index = row_index(rows)
    wing = safe_int(params['wing'])
    pe_short = safe_int(pe_zone.get('strike'))
    ce_short = safe_int(ce_zone.get('strike'))
    pe_buy = nearest_strike_with_row(pe_short - wing, rows)
    ce_buy = nearest_strike_with_row(ce_short + wing, rows)
    pe_signal = pe_zone.get('signal')
    pe_score = pe_zone.get('score')
    ce_signal = ce_zone.get('signal')
    ce_score = ce_zone.get('score')
    selection_reason = f'PE writer zone {pe_short}: {pe_signal} score {pe_score}. CE writer zone {ce_short}: {ce_signal} score {ce_score}.'
    plan = {
        'kind': kind,
        'title': title,
        'source': source,
        'expiry': expiry.isoformat(),
        'dte': (expiry - today_ist()).days,
        'spot': round(spot, 2),
        'capital': params['capital'],
        'risk_pct': params['risk_pct'],
        'max_lots': params['max_lots'],
        'lot_size': NIFTY_LOT_SIZE,
        'wing_width': wing,
        'target_capture': params['target_capture'],
        'stop_multiple': params['stop_multiple'],
        'min_credit': params['min_credit'],
        'min_target_per_lot': params['min_target_per_lot'],
        'min_reward_to_risk': params['min_reward_to_risk'],
        'legs': [
            build_leg('SELL', index.get(pe_short), pe_short, 'PE', expiry),
            build_leg('SELL', index.get(ce_short), ce_short, 'CE', expiry),
            build_leg('BUY', index.get(pe_buy), pe_buy, 'PE', expiry),
            build_leg('BUY', index.get(ce_buy), ce_buy, 'CE', expiry),
        ],
        'entry_filter': params['entry_filter'],
        'invalid_if': params['invalid_if'],
        'exit_plan': params['exit_plan'],
        'selection_reason': selection_reason,
        'short_pe_score': pe_score,
        'short_ce_score': ce_score,
    }
    estimate_values(plan)
    credit_ratio = min(safe_number(plan.get('net_credit')) / max(safe_number(plan.get('min_credit')), 1.0), 2.0)
    rr_ratio = min(safe_number(plan.get('reward_to_risk')) / max(safe_number(plan.get('min_reward_to_risk')), 0.01), 2.0)
    zone_score = (safe_number(pe_zone.get('score')) + safe_number(ce_zone.get('score'))) / 200
    distance_balance = min(safe_number(pe_zone.get('distance')), safe_number(ce_zone.get('distance'))) / max(safe_number(pe_zone.get('distance')), safe_number(ce_zone.get('distance')), 1.0)
    pass_bonus = 2.5 if plan.get('trade_ok') else 0.0
    plan['rank_score'] = round(pass_bonus + 0.9 * credit_ratio + 0.7 * rr_ratio + 1.1 * zone_score + 0.4 * distance_balance, 3)
    plan['trade_line'] = trade_line(plan)
    return plan


def strategy_params(kind: str) -> tuple[str, dict[str, object]]:
    if kind == 'weekly':
        wing = 300
        target_capture = 0.55
        min_target = 1500
        title = 'Weekly Writer-Zone Iron Condor'
        params = {
            'capital': 400000,
            'risk_pct': 0.06,
            'max_lots': 3,
            'wing': wing,
            'target_capture': target_capture,
            'stop_multiple': 1.75,
            'min_credit_ratio': 0.14,
            'min_target_per_lot': min_target,
            'min_reward_to_risk': 0.08,
            'min_distance_floor': 250,
            'max_distance_floor': 1000,
            'distance_pct': 0.011,
            'max_distance_pct': 0.05,
            'min_premium': 8.0,
            'entry_filter': 'Use after first 30-45 minutes only if spot stays between writer zones and breadth is not one-way trending.',
            'invalid_if': 'Skip if spot is within 150 points of either short strike, OI starts unwinding, spreads are wide, or any hard gate fails.',
            'exit_plan': 'Book 50-60% credit capture. Cut if combined spread loss hits stop reference or spot closes beyond short-strike buffer.',
        }
    else:
        wing = 500
        target_capture = 0.60
        min_target = 2500
        title = 'Monthly Writer-Zone Wide Iron Condor'
        params = {
            'capital': 400000,
            'risk_pct': 0.045,
            'max_lots': 2,
            'wing': wing,
            'target_capture': target_capture,
            'stop_multiple': 1.9,
            'min_credit_ratio': 0.15,
            'min_target_per_lot': min_target,
            'min_reward_to_risk': 0.07,
            'min_distance_floor': 600,
            'max_distance_floor': 2200,
            'distance_pct': 0.03,
            'max_distance_pct': 0.1,
            'min_premium': 15.0,
            'entry_filter': 'Prefer entry when IV is elevated but cooling. Avoid budget, RBI, election, CPI/Fed, or major event windows.',
            'invalid_if': 'Skip if one side is too close to spot, net credit is thin, OI wall is not fresh, or hedge liquidity is poor.',
            'exit_plan': 'Book 50-60% credit capture or reduce when one short side loses writer support.',
        }
    min_credit_by_target = min_target / max(NIFTY_LOT_SIZE * target_capture, 1)
    params['min_credit'] = round(max(wing * safe_number(params['min_credit_ratio']), min_credit_by_target), 2)
    return title, params


def build_action_plan(kind: str, spot: float, expiry: date, rows: list[OptionRow], source: str) -> dict[str, object]:
    title, params = strategy_params(kind)
    if not rows:
        return no_trade_plan(kind, title, expiry, source, 'No option rows available for this expiry.', params)
    pe_zones = build_writing_zones(rows, 'PE', spot)
    ce_zones = build_writing_zones(rows, 'CE', spot)
    min_distance = max(safe_int(params['min_distance_floor']), ceil_to_step(spot * safe_number(params['distance_pct'])))
    max_distance = max(safe_int(params['max_distance_floor']), ceil_to_step(spot * safe_number(params['max_distance_pct'])))
    pe_pool = zone_pool(pe_zones, min_distance, max_distance, safe_number(params['min_premium']))
    ce_pool = zone_pool(ce_zones, min_distance, max_distance, safe_number(params['min_premium']))
    if not pe_pool or not ce_pool:
        return no_trade_plan(kind, title, expiry, source, 'No put/call writer-zone pair has enough distance and premium.', params)

    candidates: list[dict[str, object]] = []
    for pe_zone in pe_pool:
        for ce_zone in ce_pool:
            if safe_int(pe_zone.get('strike')) >= safe_int(ce_zone.get('strike')):
                continue
            candidates.append(build_pair_plan(kind, title, spot, expiry, rows, source, pe_zone, ce_zone, params))
    if not candidates:
        return no_trade_plan(kind, title, expiry, source, 'No valid support/resistance pair could be formed from writer zones.', params)
    passing = [plan for plan in candidates if plan.get('trade_ok')]
    if passing:
        return max(passing, key=lambda plan: safe_number(plan.get('rank_score')))
    return max(candidates, key=lambda plan: safe_number(plan.get('rank_score')))


def unavailable_board(error: str) -> dict[str, object]:
    return {
        'source': 'UNAVAILABLE',
        'data_ok': False,
        'status': f'NSE live and bhavcopy sources failed. Error: {error}',
        'spot': 0,
        'as_of': '-',
        'server_refreshed_at': now_ist_label(),
        'cache_seconds': CACHE_SECONDS,
        'lot_size': NIFTY_LOT_SIZE,
        'zones': {
            'expiry': '-',
            'puts': [],
            'calls': [],
            'support': None,
            'resistance': None,
            'writer_range': 'Unavailable',
            'pcr': 0,
            'bias': 'Unavailable',
            'total_pe_oi': 0,
            'total_ce_oi': 0,
        },
        'participant': None,
        'plans': [],
        'stale': False,
    }


def load_action_board_uncached() -> dict[str, object]:
    base = today_ist()
    status = 'Live NSE option-chain writer map. Broker token not required.'
    source = 'NSE'
    try:
        payload = fetch_nse_option_chain()
        spot, timestamp, expiries, rows = parse_chain(payload)
        if spot <= 0 or not rows:
            raise ValueError('NSE option chain returned no usable rows')
    except Exception as live_exc:  # noqa: BLE001 - NSE can block hosted traffic intermittently
        try:
            spot, timestamp, expiries, rows = fetch_bhavcopy_option_chain()
            source = 'NSE_EOD'
            status = f'Live NSE option-chain unavailable, using latest real NSE F&O bhavcopy. Verify broker live LTP before trading. Live error: {live_exc}'
        except Exception as fallback_exc:  # noqa: BLE001
            stale = _CACHE.get('board')
            if isinstance(stale, dict) and stale.get('source') in ('NSE', 'NSE_EOD'):
                copy = dict(stale)
                copy['stale'] = True
                copy['status'] = f'Refresh failed, showing last cached real board. Live error: {live_exc}; fallback error: {fallback_exc}'
                copy['server_refreshed_at'] = now_ist_label()
                return copy
            return unavailable_board(f'live error: {live_exc}; fallback error: {fallback_exc}')

    weekly_expiry, monthly_expiry = choose_expiries(expiries, base)
    weekly_rows = rows_for_expiry(rows, weekly_expiry)
    monthly_rows = rows_for_expiry(rows, monthly_expiry)
    zones = build_writer_map(weekly_rows, spot, weekly_expiry)
    participant = fetch_participant_oi()
    plans = [
        build_action_plan('weekly', spot, weekly_expiry, weekly_rows, source),
        build_action_plan('monthly', spot, monthly_expiry, monthly_rows, source),
    ]
    return {
        'source': source,
        'data_ok': True,
        'status': status,
        'spot': round(spot, 2),
        'as_of': timestamp,
        'server_refreshed_at': now_ist_label(),
        'cache_seconds': CACHE_SECONDS,
        'lot_size': NIFTY_LOT_SIZE,
        'zones': zones,
        'participant': participant,
        'plans': plans,
        'stale': False,
    }


def load_action_board() -> dict[str, object]:
    expires_at = _CACHE.get('expires_at')
    if isinstance(expires_at, datetime) and expires_at > now_ist() and isinstance(_CACHE.get('board'), dict):
        return _CACHE['board']  # type: ignore[return-value]
    board = load_action_board_uncached()
    _CACHE['board'] = board
    _CACHE['expires_at'] = now_ist() + timedelta(seconds=CACHE_SECONDS)
    return board


PAGE = '''
<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <meta http-equiv='refresh' content='300'>
  <title>NIFTY Writer Map</title>
  <style>
    :root { --ink:#18212a; --muted:#5e6a74; --line:#d7dee4; --page:#f4f7f5; --panel:#ffffff; --green:#08744f; --red:#b42318; --amber:#b7791f; --blue:#0e7490; --dark:#101820; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; color:var(--ink); background:var(--page); }
    header { background:#fbfcfb; border-bottom:1px solid var(--line); }
    .wrap { width:min(1240px, calc(100vw - 32px)); margin:0 auto; }
    .topbar { display:flex; align-items:center; justify-content:space-between; gap:18px; padding:16px 0; }
    .brand { display:flex; align-items:center; gap:12px; min-width:0; }
    .mark { width:38px; height:38px; border:2px solid var(--green); display:grid; place-items:center; font-weight:800; color:var(--green); }
    h1 { margin:0; font-size:20px; line-height:1.2; letter-spacing:0; }
    h2 { margin:0 0 12px; font-size:16px; letter-spacing:0; }
    h3 { margin:0 0 5px; font-size:15px; letter-spacing:0; }
    .sub, .muted { color:var(--muted); font-size:13px; }
    .status { display:flex; align-items:center; gap:9px; padding:8px 11px; border:1px solid var(--line); background:#fff; font-size:13px; max-width:660px; }
    .dot { width:9px; height:9px; border-radius:99px; background:var(--green); flex:0 0 auto; }
    .dot.bad { background:var(--red); }
    main { padding:24px 0 40px; }
    .kpis { display:grid; grid-template-columns:repeat(5, minmax(0, 1fr)); gap:10px; margin-bottom:16px; }
    .kpi, .panel, .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; }
    .kpi { padding:13px; min-height:84px; }
    .kpi b { display:block; font-size:21px; margin-bottom:5px; }
    .grid { display:grid; grid-template-columns:1.1fr .9fr; gap:16px; align-items:start; }
    .section { padding:17px; }
    .section + .section { border-top:1px solid var(--line); }
    .cards { display:grid; gap:12px; }
    .card { padding:15px; }
    .head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:12px; }
    .badge { border:1px solid var(--line); padding:7px 9px; font-size:12px; font-weight:750; white-space:nowrap; color:var(--green); background:#edf8f2; }
    .badge.no { color:var(--amber); background:#fff7e6; }
    .trade { padding:13px; border-radius:8px; background:var(--dark); color:#eef7f3; line-height:1.55; font-family:Consolas, ui-monospace, monospace; font-size:13px; margin-bottom:12px; }
    .metrics { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:8px; margin-bottom:12px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fff; min-height:66px; }
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
    .mini-title { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:8px; }
    .pill { color:var(--muted); border:1px solid var(--line); padding:5px 7px; font-size:12px; background:#fff; }
    .empty { padding:16px; border:1px solid var(--line); background:#fff9ee; color:#724c12; border-radius:8px; line-height:1.45; }
    footer { padding:18px 0 30px; color:var(--muted); font-size:12px; }
    @media (max-width:1050px) { .grid, .kpis, .zone-wrap, .metrics { grid-template-columns:1fr; } .topbar { flex-direction:column; align-items:flex-start; } .status { max-width:none; } .head { flex-direction:column; } }
  </style>
</head>
<body>
  <header>
    <div class='wrap topbar'>
      <div class='brand'><div class='mark'>N</div><div><h1>NIFTY Writer Map & Trade Board</h1><div class='sub'>Live chain first, real NSE bhavcopy fallback second</div></div></div>
      <div class='status'><span class='dot {% if not board.data_ok %}bad{% endif %}'></span>{{ board.status }}</div>
    </div>
  </header>

  <main class='wrap'>
    <div class='kpis'>
      <div class='kpi'><b>{{ '%.2f'|format(board.spot) }}</b><span class='muted'>NIFTY spot/future proxy</span></div>
      <div class='kpi'><b>{{ board.zones.writer_range }}</b><span class='muted'>Current writer range</span></div>
      <div class='kpi'><b>{{ board.zones.pcr }}</b><span class='muted'>PE/CE OI ratio</span></div>
      <div class='kpi'><b>{{ board.source }}</b><span class='muted'>Data source</span></div>
      <div class='kpi'><b>{{ board.as_of }}</b><span class='muted'>Data timestamp</span></div>
    </div>

    {% if not board.data_ok %}
      <div class='empty'>Both live option-chain and real bhavcopy fallback are unavailable right now, so the board is intentionally not showing trades.</div>
    {% endif %}

    <div class='grid'>
      <div class='panel'>
        <section class='section'>
          <h2>Actionable Trade Candidates</h2>
          <div class='cards'>
            {% for plan in board.plans %}
            <article class='card'>
              <div class='head'>
                <div><h3>{{ plan.title }}</h3><div class='muted'>Expiry {{ plan.expiry }} | DTE {{ plan.dte }} | Lot size {{ plan.lot_size }} | Metrics: {{ plan.metric_scope }}</div></div>
                <div class='badge {% if not plan.trade_ok %}no{% endif %}'>{{ plan.decision }}</div>
              </div>
              <div class='trade'>{{ plan.trade_line }}</div>
              <div class='metrics'>
                <div class='metric'><b>{{ plan.net_credit }}</b><span>Net credit / unit</span></div>
                <div class='metric'><b>INR {{ '{:,.0f}'.format(plan.max_risk) }}</b><span>Max risk shown</span></div>
                <div class='metric'><b>INR {{ '{:,.0f}'.format(plan.target_profit) }}</b><span>Target shown</span></div>
                <div class='metric'><b>{{ plan.reward_to_risk_pct }}%</b><span>Target / max risk</span></div>
              </div>
              {% if plan.legs %}
              <table>
                <tr><th>Side</th><th>Strike</th><th>Type</th><th>LTP</th><th>OI</th><th>Volume</th><th>IV</th></tr>
                {% for leg in plan.legs %}
                <tr><td class='{{ leg.side|lower }}'>{{ leg.side }}</td><td>{{ leg.strike }}</td><td>{{ leg.type }}</td><td>{{ leg.ltp }}</td><td>{{ '{:,.0f}'.format(leg.oi) }}</td><td>{{ '{:,.0f}'.format(leg.volume) }}</td><td>{{ leg.iv }}</td></tr>
                {% endfor %}
              </table>
              {% endif %}
              <div class='notes'>
                <div class='note {% if not plan.trade_ok %}warn{% endif %}'>Decision: {{ plan.decision_reason }}</div>
                <div class='note'>Hard gates: credit >= {{ plan.min_credit }}, target/lot >= INR {{ '{:,.0f}'.format(plan.min_target_per_lot) }}, target/risk >= {{ (plan.min_reward_to_risk * 100)|round(1) }}%.</div>
                <div class='note'>Writer basis: {{ plan.selection_reason }}</div>
                <div class='note'>Entry: {{ plan.entry_filter }}</div>
                <div class='note warn'>Invalid if: {{ plan.invalid_if }}</div>
                <div class='note'>Exit: {{ plan.exit_plan }}</div>
              </div>
            </article>
            {% else %}
            <div class='empty'>No trade candidate can be generated until a real NSE source is available.</div>
            {% endfor %}
          </div>
        </section>
      </div>

      <aside class='panel'>
        <section class='section'>
          <h2>Where Writers Are Short</h2>
          <div class='zone-wrap'>
            <div>
              <div class='mini-title'><h3>Put Writers / Support</h3><span class='pill'>PE shorts proxy</span></div>
              <table>
                <tr><th>Strike</th><th>Signal</th><th>OI</th><th>Chg OI</th><th>LTP</th><th>Score</th></tr>
                {% for zone in board.zones.puts %}
                <tr><td>{{ zone.strike }}</td><td>{{ zone.signal }}</td><td>{{ '{:,.0f}'.format(zone.oi) }}</td><td>{{ '{:,.0f}'.format(zone.chg_oi) }}</td><td>{{ zone.ltp }}</td><td>{{ zone.score }}</td></tr>
                {% else %}
                <tr><td colspan='6'>No PE writer zones.</td></tr>
                {% endfor %}
              </table>
            </div>
            <div>
              <div class='mini-title'><h3>Call Writers / Resistance</h3><span class='pill'>CE shorts proxy</span></div>
              <table>
                <tr><th>Strike</th><th>Signal</th><th>OI</th><th>Chg OI</th><th>LTP</th><th>Score</th></tr>
                {% for zone in board.zones.calls %}
                <tr><td>{{ zone.strike }}</td><td>{{ zone.signal }}</td><td>{{ '{:,.0f}'.format(zone.oi) }}</td><td>{{ '{:,.0f}'.format(zone.chg_oi) }}</td><td>{{ zone.ltp }}</td><td>{{ zone.score }}</td></tr>
                {% else %}
                <tr><td colspan='6'>No CE writer zones.</td></tr>
                {% endfor %}
              </table>
            </div>
          </div>
        </section>

        <section class='section'>
          <h2>Participant Bias</h2>
          {% if board.participant %}
            <div class='note'>{{ board.participant.summary.bias }} | Date {{ board.participant.date }}. {{ board.participant.note }}</div>
            <table>
              <tr><th>Type</th><th>Fut Net</th><th>Call Short</th><th>Put Short</th><th>Call Net</th><th>Put Net</th></tr>
              {% for row in board.participant.rows %}
              <tr><td>{{ row.client_type }}</td><td>{{ '{:,.0f}'.format(row.future_net) }}</td><td>{{ '{:,.0f}'.format(row.call_short) }}</td><td>{{ '{:,.0f}'.format(row.put_short) }}</td><td>{{ '{:,.0f}'.format(row.call_net) }}</td><td>{{ '{:,.0f}'.format(row.put_net) }}</td></tr>
              {% endfor %}
            </table>
          {% else %}
            <div class='empty'>Participant OI archive was not available during this refresh. Strike map still uses option-chain or bhavcopy OI.</div>
          {% endif %}
        </section>
      </aside>
    </div>
  </main>

  <footer class='wrap'>Research software only. Strike-level FII identity is not public, so writer zones are OI/OI-change proxies. Verify live broker prices, spreads, margin, event risk, and order execution before trading.</footer>
</body>
</html>
'''


@app.get('/')
def index():
    return render_template_string(PAGE, board=load_action_board())


@app.get('/api/action-plan')
def action_plan():
    return jsonify(load_action_board())


@app.get('/api/strategy-configs')
def strategy_configs():
    configs: list[dict[str, object]] = []
    for path in sorted(CONFIG_DIR.glob('*.json')):
        try:
            configs.append(json.loads(path.read_text(encoding='utf-8')))
        except json.JSONDecodeError:
            continue
    return jsonify(configs)


@app.get('/healthz')
def healthz():
    return 'ok', 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8050, debug=True)
