#!/usr/bin/env python3
"""Standalone, dependency-free dashboard generator for the Cowork scheduled task."""
from __future__ import annotations

import html
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

AI_COMPANY_ROSTER = [
    "OpenAI", "Anthropic", "Google DeepMind", "Microsoft AI",
    "Meta AI", "NVIDIA", "Hugging Face",
]
QUOTES = [
    "The best way to predict the future is to create it. — Peter Drucker",
    "Success is not final, failure is not fatal: it is the courage to continue that counts. — Winston Churchill",
    "Do the best you can until you know better. Then when you know better, do better. — Maya Angelou",
    "It always seems impossible until it's done. — Nelson Mandela",
    "The only way to do great work is to love what you do. — Steve Jobs",
]


def esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def sparkline_svg(points, color="#059669", width=80, height=24) -> str:
    if not points or len(points) < 2:
        return ""
    low, high = min(points), max(points)
    span = (high - low) or 1.0
    step = width / (len(points) - 1)
    coords = [
        f"{i * step:.1f},{height - ((v - low) / span) * height:.1f}"
        for i, v in enumerate(points)
    ]
    poly = " ".join(coords)
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'preserveAspectRatio="none"><polyline fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
        f'points="{poly}" /></svg>'
    )


def tag_class(tag: str) -> str:
    return tag.lower().replace(" ", "-")


def news_card(item: dict, default_tag: str = "News") -> str:
    tag = item.get("tag", default_tag)
    published = item.get("published_at", "")
    time_display = "Date unavailable"
    if published:
        try:
            dt = datetime.fromisoformat(published)
            time_display = dt.strftime("%d %b %Y · %H:%M UTC")
        except ValueError:
            pass
    return f"""
        <div class="card">
          <div class="card-top">
            <h3>{esc(item['headline'])}</h3>
            <span class="tag tag-{tag_class(tag)}">{esc(tag)}</span>
          </div>
          <p class="summary">{esc(item['summary'])}</p>
          <div class="card-footer">
            <span>{esc(item['source'])} · {time_display}</span>
            <a href="{esc(item.get('link', '#'))}" target="_blank" rel="noopener">Read →</a>
          </div>
        </div>"""


def format_date(value) -> str:
    """Render a story date as e.g. '15 Aug 2026' from a date or datetime ISO string."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value)).strftime("%d %b %Y")
    except ValueError:
        return ""


def sports_card(story: dict) -> str:
    date_display = format_date(story.get("published_at"))
    source_line = esc(story["source"])
    if date_display:
        source_line = f"{source_line} · {date_display}"
    return f"""
          <div class="card" style="margin-bottom:10px;">
            <div class="card-top">
              <h3>{esc(story['headline'])}</h3>
              <span class="tag tag-sports">{esc(story.get('tag', 'Sports'))}</span>
            </div>
            <div class="sports-kind">{esc(story.get('kind', 'news'))}</div>
            <p class="summary">{esc(story['summary'])}</p>
            <div class="card-footer">
              <span>{source_line}</span>
              <a href="{esc(story.get('link', '#'))}" target="_blank" rel="noopener">Read →</a>
            </div>
          </div>"""


def fx_card(rate: dict, sparkline_color="#059669") -> str:
    direction = rate.get("direction", "flat")
    arrow = {"up": "▲", "down": "▼", "flat": "●"}.get(direction, "—")
    pct = rate.get("daily_change_pct")
    pct_display = f"{pct:.2f}%" if pct is not None else ("" if direction == "unavailable" else "n/a")
    rate_display = f"{rate['rate']:.4f}" if rate.get("available") and rate.get("rate") is not None else "Unavailable"
    spark = sparkline_svg(rate.get("sparkline_points", []), color=sparkline_color)
    provider_line = f"Source: {esc(rate['provider'])}" if rate.get("available") else esc(rate.get("note", "Unavailable"))
    return f"""
        <div class="card market-card">
          <div class="market-row">
            <span class="market-pair">{esc(rate['pair'])}</span>
            <span class="sparkline-wrap">{spark}</span>
          </div>
          <div class="market-row">
            <span class="market-rate">{rate_display}</span>
            <span class="market-change change-{direction}">{arrow} {pct_display}</span>
          </div>
          <div class="market-provider">{provider_line}</div>
        </div>"""


def resolve_data_date(data: dict):
    """Return the date the underlying data represents, as a `date`, or None.

    Prefers an explicit meta.data_date, then meta.generated_for, then falls
    back to the most recent `published_at` seen across the news sections.
    """
    meta = data.get("meta", {}) or {}
    for key in ("data_date", "generated_for"):
        raw = meta.get(key)
        if raw:
            try:
                return datetime.fromisoformat(str(raw)).date()
            except ValueError:
                pass
    latest = None
    sections = [data.get("uk_news", []), data.get("zimbabwe_news", []), data.get("ai_news", [])]
    for section in sections:
        for item in section:
            published = item.get("published_at")
            if not published:
                continue
            try:
                dt = datetime.fromisoformat(str(published)).date()
            except ValueError:
                continue
            if latest is None or dt > latest:
                latest = dt
    return latest


def freshness_banner(data: dict, today) -> tuple[str, str]:
    """Return (as_of_text, banner_html).

    The banner is only rendered when the data is more than one day stale,
    so a silently-frozen input file is called out on the dashboard itself
    instead of masquerading as fresh.
    """
    data_date = resolve_data_date(data)
    if data_date is None:
        return (
            "Data date unknown",
            '<div class="stale-banner">⚠️ This dashboard could not determine how recent its data is — '
            'the input file is missing a <code>meta.data_date</code> and dated headlines. Re-run the data refresh.</div>',
        )
    as_of = data_date.strftime("%A, %d %B %Y")
    age_days = (today - data_date).days
    if age_days <= 1:
        return f"Data as of {as_of}", ""
    plural = "day" if age_days == 1 else "days"
    return (
        f"Data as of {as_of}",
        f'<div class="stale-banner">⚠️ <strong>Stale data:</strong> this briefing is built from data that is '
        f'{age_days} {plural} old (as of {esc(as_of)}). Re-run the data refresh so the dashboard reflects today.</div>',
    )


def compute_sentiment(changes):
    valid = [c for c in changes if c is not None]
    if not valid:
        return "Data Unavailable", "⚪", None
    avg = sum(valid) / len(valid)
    if avg > 0.15:
        return "Risk-On", "🟢", avg
    if avg < -0.15:
        return "Risk-Off", "🔴", avg
    return "Neutral", "🟡", avg


def top_ai_company(ai_news):
    counts = Counter()
    for item in ai_news:
        combined = f"{item['headline']} {item['summary']}".lower()
        for company in AI_COMPANY_ROSTER:
            if company.lower() in combined:
                counts[company] += 1
    return counts.most_common(1)[0][0] if counts else "No single company led today's coverage"


CSS = """
:root {
  --navy: #f4f6fb; --navy-2: #eef1f8; --slate: #ffffff; --slate-2: #f8fafc;
  --slate-border: #e2e8f0; --emerald: #059669; --emerald-dim: #ecfdf5;
  --gold: #b45309; --gold-dim: #fef3c7; --text-primary: #101828;
  --text-secondary: #475569; --text-muted: #8a94a6; --red: #dc2626;
  --red-dim: #fef2f2; --purple: #9333ea; --purple-dim: #faf5ff;
  --blue: #2563eb; --blue-dim: #eff6ff;
}
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: linear-gradient(180deg, var(--navy) 0%, var(--navy-2) 100%); color: var(--text-primary); line-height: 1.5; }
a { color: inherit; text-decoration: none; }
.header { position: sticky; top: 0; z-index: 100; background: rgba(255,255,255,0.92); backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--slate-border); padding: 18px 28px; display: flex; align-items: center;
  justify-content: space-between; flex-wrap: wrap; gap: 10px; }
.header h1 { margin: 0; font-size: 1.35rem; font-weight: 700; display: flex; align-items: center; gap: 10px; }
.header h1 .brand-dot { color: var(--gold); }
.header .meta { text-align: right; color: var(--text-secondary); font-size: 0.85rem; }
.header .meta strong { color: var(--text-primary); }
.container { max-width: 1180px; margin: 0 auto; padding: 24px 20px 60px; }
.section { margin-bottom: 34px; }
.section-title { display: flex; align-items: center; gap: 10px; font-size: 1.05rem; font-weight: 700; margin: 0 0 14px 2px; }
.section-title .accent-bar { width: 5px; height: 20px; border-radius: 3px; background: var(--emerald); display: inline-block; }
.section-title.gold .accent-bar { background: var(--gold); }
.grid { display: flex; flex-wrap: wrap; gap: 14px; }
.grid > * { flex: 1 1 260px; min-width: 260px; }
.grid.two-col > * { flex: 1 1 320px; min-width: 320px; }
.card { background: linear-gradient(155deg, var(--slate) 0%, var(--slate-2) 100%); border: 1px solid var(--slate-border);
  border-radius: 14px; padding: 16px 18px; box-shadow: 0 1px 3px rgba(16,24,40,0.06), 0 4px 14px rgba(16,24,40,0.06); }
.card-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; margin-bottom: 8px; }
.card h3 { margin: 0; font-size: 0.98rem; font-weight: 650; }
.card p.summary { margin: 6px 0 10px; color: var(--text-secondary); font-size: 0.87rem; }
.card .card-footer { display: flex; justify-content: space-between; align-items: center; font-size: 0.78rem; color: var(--text-muted); }
.card .card-footer a { color: var(--emerald); font-weight: 600; }
.tag { font-size: 0.68rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.4px; padding: 3px 9px; border-radius: 999px; white-space: nowrap; }
.tag-breaking { background: var(--red-dim); color: var(--red); }
.tag-important { background: var(--gold-dim); color: var(--gold); }
.tag-trending { background: var(--purple-dim); color: var(--purple); }
.tag-markets { background: var(--gold-dim); color: var(--gold); }
.tag-ai { background: var(--emerald-dim); color: var(--emerald); }
.tag-sports, .tag-uk { background: var(--blue-dim); color: var(--blue); }
.tag-zimbabwe { background: var(--emerald-dim); color: var(--emerald); }
.tag-default, .tag-news { background: var(--slate-2); color: var(--text-secondary); }
.sub-heading { font-size: 0.85rem; font-weight: 700; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; margin: 18px 0 10px; }
.sports-block { border-left: 3px solid var(--emerald); padding-left: 12px; margin-bottom: 18px; }
.sports-block h4 { margin: 0 0 10px; font-size: 0.95rem; }
.sports-kind { font-size: 0.68rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.4px; }
.market-card { display: flex; flex-direction: column; gap: 6px; }
.market-row { display: flex; justify-content: space-between; align-items: center; }
.market-pair { font-weight: 650; font-size: 0.92rem; }
.market-rate { font-size: 1.15rem; font-weight: 700; }
.market-change { font-size: 0.8rem; font-weight: 650; }
.change-up { color: var(--emerald); } .change-down { color: var(--red); }
.change-flat { color: var(--text-muted); } .change-unavailable { color: var(--text-muted); font-style: italic; }
.market-provider { font-size: 0.72rem; color: var(--text-muted); }
.bonus-grid { display: flex; flex-wrap: wrap; gap: 14px; }
.bonus-card { text-align: center; flex: 1 1 200px; min-width: 200px; }
.bonus-card .big-emoji { font-size: 1.6rem; }
.bonus-card .label { font-size: 0.72rem; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.5px; margin-top: 4px; }
.bonus-card .value { font-size: 1.05rem; font-weight: 700; margin-top: 2px; }
.quote-card { flex-basis: 100%; font-style: italic; color: var(--text-secondary); text-align: center; padding: 20px; }
.footer { text-align: center; padding: 26px 20px 40px; color: var(--text-muted); font-size: 0.8rem; border-top: 1px solid var(--slate-border); margin-top: 20px; }
.footer .gold-accent { color: var(--gold); }
.stale-banner { background: var(--red-dim); border: 1px solid var(--red); color: var(--red); border-radius: 12px;
  padding: 12px 16px; margin: 0 0 22px; font-size: 0.88rem; font-weight: 600; }
.stale-banner code { background: rgba(220,38,38,0.12); padding: 1px 5px; border-radius: 5px; font-size: 0.82rem; }
.header .meta .as-of { color: var(--text-muted); font-size: 0.78rem; }
"""


def build_html(data: dict) -> str:
    try:
        now = datetime.now(ZoneInfo("Europe/London"))
    except Exception:
        now = datetime.now(timezone.utc)
    generated_date = now.strftime("%A, %d %B %Y")
    generated_time = now.strftime("%H:%M %Z") or now.strftime("%H:%M")
    as_of_text, stale_banner = freshness_banner(data, now.date())

    uk_cards = "".join(news_card(i, "UK") for i in data.get("uk_news", [])) or '<div class="card"><p class="summary">UK headlines unavailable this run.</p></div>'
    zw_cards = "".join(news_card(i, "Zimbabwe") for i in data.get("zimbabwe_news", [])) or '<div class="card"><p class="summary">Zimbabwe headlines unavailable this run.</p></div>'
    ai_cards = "".join(news_card(i, "AI") for i in data.get("ai_news", [])) or '<div class="card"><p class="summary">AI news unavailable this run.</p></div>'

    sports_watch = data.get("sports_watch", {})
    sports_blocks = []
    for discipline, emoji in (("Rugby", "🏉"), ("Cricket", "🏏"), ("Football World Cup", "⚽")):
        stories = sports_watch.get(discipline, [])
        cards = "".join(sports_card(s) for s in stories) or f'<p class="summary">No stories matching your {esc(discipline)} interests today.</p>'
        sports_blocks.append(f'<div class="card sports-block"><h4>{emoji} {esc(discipline)}</h4>{cards}</div>')
    sports_html = "".join(sports_blocks)

    markets = data.get("markets", {})
    fx_html = "".join(fx_card(r) for r in markets.get("fx", []))
    gold = markets.get("gold", {})
    gold_usd = f"${gold['price_per_gram_usd']:.2f}" if gold.get("available") and gold.get("price_per_gram_usd") else "—"
    gold_gbp = f"£{gold['price_per_gram_gbp']:.2f}" if gold.get("available") and gold.get("price_per_gram_gbp") else "—"
    gold_spark = sparkline_svg(gold.get("sparkline_points", []), color="#b45309")
    gold_note = esc(gold.get("note", "")) or f"Source: {esc(gold.get('provider', 'unknown'))}"

    weather_cards = "".join(
        f'''<div class="card bonus-card"><div class="big-emoji">{esc(w.get('emoji','⚪'))}</div>
        <div class="value">{f"{w['temperature_c']:.0f}°C" if w.get('available') and w.get('temperature_c') is not None else "—"}</div>
        <div class="label">{esc(w['city'])} · {esc(w.get('description',''))}</div></div>'''
        for w in data.get("weather", [])
    )

    sentiment_label, sentiment_emoji, sentiment_avg = compute_sentiment(
        [gold.get("daily_change_pct")] + [r.get("daily_change_pct") for r in markets.get("fx", [])]
    )
    sentiment_sub = f"Market Sentiment ({sentiment_avg:.2f}% avg)" if sentiment_avg is not None else "Market Sentiment"
    top_ai = esc(top_ai_company(data.get("ai_news", [])))
    quote = esc(QUOTES[now.timetuple().tm_yday % len(QUOTES)])

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Intelligence Dashboard — {generated_date}</title>
<style>{CSS}</style></head><body>
<header class="header">
  <h1><span class="brand-dot">◆</span> Daily Intelligence Dashboard</h1>
  <div class="meta"><div><strong>{generated_date}</strong></div><div>Generated at {generated_time}</div><div class="as-of">{esc(as_of_text)}</div></div>
</header>
<div class="container">
  {stale_banner}
  <section class="section"><div class="section-title"><span class="accent-bar"></span>📰 UK Headlines</div><div class="grid">{uk_cards}</div></section>
  <section class="section"><div class="section-title"><span class="accent-bar"></span>🇿🇼 Zimbabwe Headlines</div><div class="grid">{zw_cards}</div></section>
  <section class="section"><div class="section-title"><span class="accent-bar"></span>🏉 Sports Watch</div><div class="grid two-col">{sports_html}</div></section>
  <section class="section"><div class="section-title"><span class="accent-bar"></span>🤖 AI Intelligence</div><div class="grid">{ai_cards}</div></section>
  <section class="section">
    <div class="section-title gold"><span class="accent-bar"></span>📈 Markets</div>
    <div class="sub-heading">Gold</div>
    <div class="grid two-col">
      <div class="card market-card"><div class="market-row"><span class="market-pair">🥇 Gold — price per gram (USD)</span><span class="sparkline-wrap">{gold_spark}</span></div>
        <div class="market-row"><span class="market-rate">{gold_usd}</span></div><div class="market-provider">{gold_note}</div></div>
      <div class="card market-card"><div class="market-row"><span class="market-pair">🥇 Gold — price per gram (GBP)</span></div>
        <div class="market-row"><span class="market-rate">{gold_gbp}</span></div><div class="market-provider">Converted at current GBP/USD rate</div></div>
    </div>
    <div class="sub-heading">Exchange Rates</div>
    <div class="grid">{fx_html}</div>
  </section>
  <section class="section">
    <div class="section-title"><span class="accent-bar"></span>✨ At a Glance</div>
    <div class="bonus-grid">
      {weather_cards}
      <div class="card bonus-card"><div class="big-emoji">{sentiment_emoji}</div><div class="value">{esc(sentiment_label)}</div><div class="label">{esc(sentiment_sub)}</div></div>
      <div class="card bonus-card"><div class="big-emoji">🏆</div><div class="value">{top_ai}</div><div class="label">Top AI Company Today</div></div>
      <div class="card bonus-card quote-card">"{quote}"<div class="label" style="margin-top:8px;">Quote of the Day</div></div>
    </div>
  </section>
</div>
<div class="footer">Generated automatically by <span class="gold-accent">Codex Daily Intelligence Dashboard</span> · {generated_date} at {generated_time} · {esc(as_of_text)}</div>
</body></html>"""


def try_export_pdf(html_path: Path, pdf_path: Path) -> bool:
    try:
        from weasyprint import HTML  # type: ignore
        HTML(filename=str(html_path)).write_pdf(str(pdf_path))
        return True
    except ImportError:
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--break-system-packages", "-q", "weasyprint"],
                timeout=35, check=True, capture_output=True,
            )
            from weasyprint import HTML  # type: ignore
            HTML(filename=str(html_path)).write_pdf(str(pdf_path))
            return True
        except Exception:
            return False
    except Exception:
        return False


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python3 cowork_standalone_generator.py <input.json> <output_dir>")
        return 1
    input_path, output_dir = Path(sys.argv[1]), Path(sys.argv[2])
    output_dir.mkdir(parents=True, exist_ok=True)

    data = json.loads(input_path.read_text(encoding="utf-8"))
    html_content = build_html(data)

    html_path = output_dir / "daily_dashboard.html"
    html_path.write_text(html_content, encoding="utf-8")
    print(f"HTML written: {html_path}")

    pdf_path = output_dir / "daily_dashboard.pdf"
    if try_export_pdf(html_path, pdf_path):
        print(f"PDF written: {pdf_path}")
    else:
        print("PDF export skipped (WeasyPrint unavailable) — HTML dashboard is still complete.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
