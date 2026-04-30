# NIFTY Option-Writing Research Notes

## How Larger Players Usually Think About This

Institutional option writers are usually not looking for a magical high-win-rate setup. They are selling volatility when implied volatility is rich versus expected realized movement, hedging tail risk, and managing inventory around expiry, events, and flows.

Common inputs:

- Option chain: strike-wise OI, OI change, volume, bid-ask spread, IV, IV rank, skew, put-call ratio.
- NIFTY spot/futures: trend, realized volatility, gap, intraday range, futures basis.
- Participant data: FII, DII, proprietary, and client long/short positioning in index futures and index options.
- Volatility regime: India VIX level, VIX change, VIX percentile, event risk.
- Expiry mechanics: DTE, max-pain-like OI clusters, dealer gamma zones, settlement-day pinning risk.
- Risk controls: fixed max loss, stop based on combined spread value, no-trade filters around major events, and aggressive sizing reduction when volatility expands.

## Weekly Expiry

Weekly writers usually care about gamma and intraday path. The edge often comes from selling defined-risk premium after the first move is known, then exiting quickly when decay is captured. A high win rate is possible, but the danger is one large trending expiry day wiping out many small wins.

Good filters to test:

- Avoid if first 15-30 minute NIFTY range is too large.
- Avoid if VIX is up sharply versus the previous close.
- Avoid if FII/pro futures shorts increased materially and spot opens below the previous day low.
- Prefer defined-risk iron fly/iron condor with pre-declared max loss.

## Monthly Expiry

Monthly writing has more vega risk and event risk. Institutions often use wider condors, calendars, or partially hedged short strangles rather than simple naked shorts. The key is whether IV is expensive enough to compensate for overnight risk.

Good filters to test:

- Enter only when IV/VIX percentile is high but falling.
- Avoid budget, RBI, election, CPI/Fed, and major global event windows.
- Exit before the last expiry-day gamma window unless explicitly running an intraday strategy.

## Yearly / Long-Dated

Long-dated NIFTY options behave more like vega and rate products than short-term theta trades. Pure yearly option writing is capital heavy and can be painful if volatility reprices. Defined-risk calendars or diagonals are more realistic than naked long-dated shorts.

## Backtest Acceptance Rules

Use these before trusting any strategy:

- At least 5 years of data, or a clearly marked shorter intraday period.
- No lookahead: select strikes using only data available at entry time.
- Include brokerage, STT, transaction charges, SEBI charges, stamp duty, GST, and slippage.
- Track max drawdown, worst trade, profit factor, average win/loss, and tail loss.
- Run separate reports for COVID-style crash, calm bull market, gap-down expiry, and high-VIX regimes.
- Prefer strategies that survive parameter shifts, not strategies that work only at one exact wing width or stop value.

The current starter configs are conservative:

- `expiry_intraday_iron_fly.json`: defined-risk expiry-day iron fly using 18L capital.
- `weekly_positional_iron_condor.json`: defined-risk weekly carry using 4L capital.
