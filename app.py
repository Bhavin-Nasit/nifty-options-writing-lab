from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, jsonify, render_template_string


ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "configs"

app = Flask(__name__)


PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NIFTY Options Writing Lab</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #172026;
      --muted: #59656f;
      --line: #d7dde2;
      --panel: #ffffff;
      --page: #f4f7f5;
      --green: #0f7a55;
      --amber: #b7791f;
      --red: #b42318;
      --teal: #0e7490;
      --graph: #243746;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--page);
    }
    header {
      border-bottom: 1px solid var(--line);
      background: #fbfcfb;
    }
    .wrap { width: min(1180px, calc(100vw - 32px)); margin: 0 auto; }
    .topbar { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 18px 0; }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .mark { width: 38px; height: 38px; border: 2px solid var(--green); display: grid; place-items: center; font-weight: 800; color: var(--green); }
    .brand h1 { margin: 0; font-size: 19px; line-height: 1.2; letter-spacing: 0; }
    .brand span { display: block; color: var(--muted); font-size: 13px; margin-top: 2px; }
    .status { display: flex; align-items: center; gap: 9px; padding: 8px 11px; border: 1px solid var(--line); background: #fff; font-size: 13px; white-space: nowrap; }
    .dot { width: 9px; height: 9px; border-radius: 99px; background: var(--green); }
    main { padding: 26px 0 40px; }
    .grid { display: grid; grid-template-columns: 1.4fr .9fr; gap: 18px; align-items: start; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .section { padding: 18px; }
    .section + .section { border-top: 1px solid var(--line); }
    h2 { margin: 0 0 14px; font-size: 16px; letter-spacing: 0; }
    .kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .kpi { border: 1px solid var(--line); border-radius: 8px; padding: 13px; min-height: 92px; background: #fff; }
    .kpi b { display: block; font-size: 22px; margin-bottom: 6px; }
    .kpi span { color: var(--muted); font-size: 12px; line-height: 1.35; }
    .strategy-list { display: grid; gap: 10px; }
    .strategy { border: 1px solid var(--line); border-radius: 8px; padding: 14px; display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; }
    .strategy h3 { margin: 0 0 6px; font-size: 15px; letter-spacing: 0; }
    .strategy p { margin: 0; color: var(--muted); font-size: 13px; }
    .badge { border: 1px solid var(--line); padding: 7px 9px; font-size: 12px; background: #fafafa; white-space: nowrap; }
    .flow { display: grid; gap: 10px; }
    .step { display: grid; grid-template-columns: 28px 1fr; gap: 10px; align-items: start; }
    .step i { display: grid; place-items: center; width: 28px; height: 28px; border: 1px solid var(--line); color: var(--teal); font-style: normal; font-weight: 700; }
    .step strong { display: block; font-size: 13px; margin-bottom: 3px; }
    .step span { display: block; color: var(--muted); font-size: 12px; line-height: 1.35; }
    .risk-table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .risk-table th, .risk-table td { padding: 10px 8px; border-bottom: 1px solid var(--line); text-align: left; }
    .risk-table th { color: var(--muted); font-weight: 650; }
    .pill { display: inline-block; padding: 4px 7px; border-radius: 999px; font-size: 12px; background: #eef7f1; color: var(--green); }
    .warn { background: #fff7e8; color: var(--amber); }
    .chart { height: 210px; border: 1px solid var(--line); border-radius: 8px; padding: 16px; display: grid; align-items: end; grid-template-columns: repeat(12, 1fr); gap: 6px; background: linear-gradient(#fff, #f9fbfa); }
    .bar { background: var(--graph); min-height: 18px; border-radius: 4px 4px 0 0; opacity: .88; }
    .bar:nth-child(3n) { background: var(--green); }
    .bar:nth-child(4n) { background: var(--teal); }
    code { display: block; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: #111820; color: #e9f2ef; overflow-x: auto; font-size: 12px; line-height: 1.6; }
    footer { padding: 18px 0 30px; color: var(--muted); font-size: 12px; }
    @media (max-width: 860px) {
      .topbar { align-items: flex-start; flex-direction: column; }
      .grid, .kpis { grid-template-columns: 1fr; }
      .strategy { grid-template-columns: 1fr; }
      .status { white-space: normal; }
    }
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <div class="brand">
        <div class="mark">N</div>
        <div>
          <h1>NIFTY Options Writing Lab</h1>
          <span>Defined-risk research console for expiry and positional option writing</span>
        </div>
      </div>
      <div class="status"><span class="dot"></span> Render web service ready</div>
    </div>
  </header>

  <main class="wrap">
    <div class="grid">
      <div class="panel">
        <section class="section">
          <h2>Research Snapshot</h2>
          <div class="kpis">
            <div class="kpi"><b>18L</b><span>Expiry-day intraday capital model</span></div>
            <div class="kpi"><b>4L</b><span>Weekly and monthly positional capital model</span></div>
            <div class="kpi"><b>2</b><span>Starter defined-risk strategies</span></div>
            <div class="kpi"><b>5Y</b><span>Designed for historical chain backtests</span></div>
          </div>
        </section>

        <section class="section">
          <h2>Strategy Queue</h2>
          <div class="strategy-list">
            {% for cfg in configs %}
            <div class="strategy">
              <div>
                <h3>{{ cfg.name.replace('_', ' ').title() }}</h3>
                <p>{{ cfg.strategy.replace('_', ' ').title() }} | Entry DTE {{ cfg.entry_dte }} | Wing width {{ cfg.wing_width }} | Max lots {{ cfg.max_lots }}</p>
              </div>
              <span class="badge">Risk {{ '%.1f'|format(cfg.risk_per_trade_pct * 100) }}%</span>
            </div>
            {% endfor %}
          </div>
        </section>

        <section class="section">
          <h2>Equity Curve Preview</h2>
          <div class="chart" aria-label="Sample equity curve bars">
            <div class="bar" style="height:34%"></div><div class="bar" style="height:39%"></div><div class="bar" style="height:44%"></div><div class="bar" style="height:42%"></div>
            <div class="bar" style="height:53%"></div><div class="bar" style="height:58%"></div><div class="bar" style="height:49%"></div><div class="bar" style="height:66%"></div>
            <div class="bar" style="height:72%"></div><div class="bar" style="height:68%"></div><div class="bar" style="height:80%"></div><div class="bar" style="height:88%"></div>
          </div>
        </section>
      </div>

      <aside class="panel">
        <section class="section">
          <h2>Data Pipeline</h2>
          <div class="flow">
            <div class="step"><i>1</i><div><strong>Kite capture</strong><span>Spot candles and active option contracts.</span></div></div>
            <div class="step"><i>2</i><div><strong>Historical chain</strong><span>Normalized expired option data for five-year tests.</span></div></div>
            <div class="step"><i>3</i><div><strong>Institutional filters</strong><span>Participant OI, FII derivatives, India VIX, realized volatility.</span></div></div>
            <div class="step"><i>4</i><div><strong>Backtest report</strong><span>Win rate, drawdown, worst trade, charges, and tail risk.</span></div></div>
          </div>
        </section>

        <section class="section">
          <h2>Risk Controls</h2>
          <table class="risk-table">
            <tr><th>Control</th><th>Status</th></tr>
            <tr><td>Defined-risk spreads</td><td><span class="pill">On</span></td></tr>
            <tr><td>Zerodha charges</td><td><span class="pill">On</span></td></tr>
            <tr><td>Data availability</td><td><span class="pill warn">Needs feed</span></td></tr>
            <tr><td>Live trade routing</td><td><span class="pill warn">Off</span></td></tr>
          </table>
        </section>

        <section class="section">
          <h2>Render Start</h2>
          <code>gunicorn app:app</code>
        </section>
      </aside>
    </div>
  </main>

  <footer class="wrap">Research software only. It is not investment advice or a trade recommendation.</footer>
</body>
</html>
"""


def load_strategy_configs() -> list[dict[str, object]]:
    configs: list[dict[str, object]] = []
    for path in sorted(CONFIG_DIR.glob("*.json")):
        try:
            configs.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return configs


@app.get("/")
def index():
    return render_template_string(PAGE, configs=load_strategy_configs())


@app.get("/api/strategy-configs")
def strategy_configs():
    return jsonify(load_strategy_configs())


@app.get("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8050, debug=True)
