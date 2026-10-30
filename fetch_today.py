#!/usr/bin/env python3
"""Fetch live briefing inputs and regenerate the dashboard HTML."""
from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from itertools import zip_longest
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
USER_AGENT = "DailyIntelligenceDashboard/1.0 (local briefing refresh)"
TROY_OZ_GRAMS = 31.1034768
CTX = ssl.create_default_context()

WMO = {
    0: ("Clear", "☀️"),
    1: ("Mainly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"),
    48: ("Rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Drizzle", "🌦️"),
    55: ("Heavy drizzle", "🌧️"),
    61: ("Light rain", "🌧️"),
    63: ("Rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    71: ("Light snow", "🌨️"),
    73: ("Snow", "🌨️"),
    75: ("Heavy snow", "❄️"),
    80: ("Rain showers", "🌦️"),
    81: ("Rain showers", "🌧️"),
    82: ("Heavy showers", "🌧️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm with hail", "⛈️"),
    99: ("Severe thunderstorm", "⛈️"),
}


def get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
        return resp.read()


def get_json(url: str):
    return json.loads(get(url).decode("utf-8"))


def tidy_source(source: str, url: str) -> str:
    lower = f"{source} {url}".lower()
    if "techcrunch" in lower:
        return "TechCrunch"
    if "theverge" in lower or "the verge" in lower:
        return "The Verge"
    if "bbc.co.uk/sport" in lower or "bbc sport" in lower:
        return "BBC Sport"
    if "bbc" in lower:
        return "BBC News"
    if "zimbabwesituation" in lower:
        return "Zimbabwe Situation"
    if "newzimbabwe" in lower:
        return "NewZimbabwe.com"
    return source.split(" | ")[0].split(" - ")[0][:80]


def strip_html(text: str) -> str:
    text = unescape(text or "")
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def iso_pub(value: str | None) -> str:
    if not value:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def rss_items(url: str, limit: int = 8) -> list[dict]:
    try:
        raw = get(url)
    except Exception as exc:
        print(f"Feed failed {url}: {exc}", file=sys.stderr)
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        print(f"XML parse failed {url}: {exc}", file=sys.stderr)
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}
    items = []
    channel_title = ""
    channel = root.find("channel")
    if channel is not None:
        channel_title = (channel.findtext("title") or "").strip()
        nodes = channel.findall("item")
    else:
        channel_title = (root.findtext("atom:title", default="", namespaces=ns) or "").strip()
        nodes = root.findall("atom:entry", ns)

    for node in nodes:
        title = strip_html(
            node.findtext("title")
            or node.findtext("atom:title", default="", namespaces=ns)
            or ""
        )
        if not title:
            continue
        link = (
            node.findtext("link")
            or (node.find("atom:link", ns).get("href") if node.find("atom:link", ns) is not None else "")
            or ""
        ).strip()
        summary = strip_html(
            node.findtext("description")
            or node.findtext("atom:summary", default="", namespaces=ns)
            or node.findtext("atom:content", default="", namespaces=ns)
            or title
        )
        summary = re.sub(r"(?i)the post\s+.*", "", summary).strip()
        summary = re.sub(r"(?i)appeared first on.*", "", summary).strip(" —–-|")
        if len(summary) > 420:
            summary = summary[:417].rsplit(" ", 1)[0] + "…"
        pub = (
            node.findtext("pubDate")
            or node.findtext("dc:date", default="", namespaces=ns)
            or node.findtext("atom:updated", default="", namespaces=ns)
            or node.findtext("atom:published", default="", namespaces=ns)
        )
        source = strip_html(node.findtext("source") or "") or channel_title or url
        source = tidy_source(source, url)
        items.append(
            {
                "headline": title,
                "summary": summary,
                "source": source.split(" - ")[0][:80],
                "published_at": iso_pub(pub),
                "link": link or "#",
            }
        )
        if len(items) >= limit:
            break
    return items


def unique_headlines(feeds: list[list[dict]], limit: int) -> list[dict]:
    # Round-robin across feeds so every source gets a share of the slots.
    items = [item for row in zip_longest(*feeds) for item in row if item is not None]
    seen = set()
    out = []
    for item in items:
        key = re.sub(r"\W+", " ", item["headline"].lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def tagged(items: list[dict], default_tag: str, important_first: bool = True) -> list[dict]:
    tagged_items = []
    for i, item in enumerate(items):
        row = dict(item)
        row["tag"] = "Important" if important_first and i == 0 else default_tag
        tagged_items.append(row)
    return tagged_items


def sports_kind(headline: str, summary: str) -> str:
    text = f"{headline} {summary}".lower()
    if any(w in text for w in ("beat", "win", "won", "defeat", "loss", "lost", "draw", "score")):
        return "result"
    if any(w in text for w in ("preview", "fixture", "faces", "vs", "v ", "to play", "kick-off", "kick off")):
        return "fixture"
    return "news"


def sports_from_feeds(urls: list[str], discipline: str, limit: int = 3) -> list[dict]:
    bundled = []
    for url in urls:
        bundled.extend(rss_items(url, limit=6))
    stories = []
    # Feeds are in priority order (later ones are fallbacks), so keep them as one list.
    for item in unique_headlines([bundled], limit):
        stories.append(
            {
                "discipline": discipline,
                "headline": item["headline"],
                "summary": item["summary"],
                "source": item["source"],
                "link": item["link"],
                "kind": sports_kind(item["headline"], item["summary"]),
                "tag": "Sports",
            }
        )
    return stories


def pct_change(current: float | None, previous: float | None):
    if current is None or previous in (None, 0):
        return None
    return round(((current - previous) / previous) * 100, 2)


def direction_from_pct(pct):
    if pct is None:
        return "flat"
    if pct > 0.05:
        return "up"
    if pct < -0.05:
        return "down"
    return "flat"


def fx_series(base: str, quote: str, days: int = 8) -> list[tuple[str, float]]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days + 4)
    url = (
        f"https://api.frankfurter.dev/v1/{start.isoformat()}..{end.isoformat()}"
        f"?base={base}&symbols={quote}"
    )
    payload = get_json(url)
    rates = payload.get("rates") or {}
    points = []
    for day in sorted(rates):
        val = rates[day].get(quote)
        if val is not None:
            points.append((day, float(val)))
    return points[-7:]


def fx_card(pair: str, base: str, quote: str, invert: bool = False, provider: str = "Frankfurter (ECB reference rates)") -> dict:
    try:
        series = fx_series(base, quote)
        if invert:
            series = [(d, (1.0 / v) if v else None) for d, v in series]
            series = [(d, v) for d, v in series if v]
        values = [v for _, v in series]
        latest = values[-1] if values else None
        prev = values[-2] if len(values) > 1 else None
        pct = pct_change(latest, prev)
        return {
            "pair": pair,
            "rate": round(latest, 4) if latest is not None else None,
            "daily_change_pct": pct,
            "direction": direction_from_pct(pct) if latest is not None else "unavailable",
            "provider": provider,
            "sparkline_points": [round(v, 4) for v in values],
            "available": latest is not None,
            "note": "" if latest is not None else "Unavailable this run",
        }
    except Exception as exc:
        return {
            "pair": pair,
            "rate": None,
            "daily_change_pct": None,
            "direction": "unavailable",
            "provider": provider,
            "sparkline_points": [],
            "available": False,
            "note": str(exc),
        }


def fetch_gold(gbp_usd: float | None) -> dict:
    try:
        payload = get_json("https://api.gold-api.com/price/XAU")
        oz = float(payload["price"])
        usd_g = oz / TROY_OZ_GRAMS
        gbp_g = (usd_g / gbp_usd) if gbp_usd else None
        updated = payload.get("updatedAt", "")
        return {
            "price_per_gram_usd": round(usd_g, 2),
            "price_per_gram_gbp": round(gbp_g, 2) if gbp_g else None,
            "daily_change_pct": None,
            "direction": "flat",
            "provider": "gold-api.com (live spot)",
            "sparkline_points": [round(usd_g, 2)],
            "available": True,
            "note": f"Spot ${oz:,.2f}/oz · updated {updated}",
        }
    except Exception as exc:
        return {
            "price_per_gram_usd": None,
            "price_per_gram_gbp": None,
            "daily_change_pct": None,
            "direction": "unavailable",
            "provider": "gold-api.com",
            "sparkline_points": [],
            "available": False,
            "note": str(exc),
        }


def fetch_weather(city: str, lat: float, lon: float) -> dict:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code"
    )
    try:
        payload = get_json(url)
        current = payload.get("current") or {}
        code = int(current.get("weather_code") or 0)
        desc, emoji = WMO.get(code, ("Conditions unavailable", "⚪"))
        temp = current.get("temperature_2m")
        return {
            "city": city,
            "temperature_c": round(float(temp), 0) if temp is not None else None,
            "description": desc,
            "emoji": emoji,
            "available": temp is not None,
        }
    except Exception:
        return {
            "city": city,
            "temperature_c": None,
            "description": "Unavailable",
            "emoji": "⚪",
            "available": False,
        }


def build_payload() -> dict:
    uk = tagged(
        unique_headlines(
            [
                rss_items("https://feeds.bbci.co.uk/news/uk/rss.xml", 8),
                rss_items("https://feeds.bbci.co.uk/news/politics/rss.xml", 6),
            ],
            4,
        ),
        "UK",
    )
    zw = tagged(
        unique_headlines(
            [
                rss_items("https://www.zimbabwesituation.com/feed/", 8),
                rss_items("https://www.newzimbabwe.com/feed/", 8),
            ],
            4,
        ),
        "Zimbabwe",
    )
    ai = tagged(
        unique_headlines(
            [
                rss_items("https://techcrunch.com/category/artificial-intelligence/feed/", 8),
                rss_items("https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", 6),
            ],
            6,
        ),
        "AI",
        important_first=False,
    )
    sports = {
        "Rugby": sports_from_feeds(
            ["https://feeds.bbci.co.uk/sport/rugby-union/rss.xml"],
            "Rugby",
        ),
        "Cricket": sports_from_feeds(
            ["https://feeds.bbci.co.uk/sport/cricket/rss.xml"],
            "Cricket",
        ),
        "Football World Cup": sports_from_feeds(
            [
                "https://feeds.bbci.co.uk/sport/football/world-cup/rss.xml",
                "https://feeds.bbci.co.uk/sport/football/rss.xml",
            ],
            "Football World Cup",
            limit=4,
        ),
    }
    gbp_usd = fx_card("GBP → USD", "GBP", "USD")
    usd_gbp = fx_card("USD → GBP", "USD", "GBP")
    usd_zar = fx_card("USD → ZAR", "USD", "ZAR", provider="Frankfurter (ECB reference rates)")
    zar_usd = fx_card("ZAR → USD", "USD", "ZAR", invert=True, provider="Frankfurter (USD/ZAR inverse)")
    gold = fetch_gold(gbp_usd.get("rate"))
    return {
        "uk_news": uk,
        "zimbabwe_news": zw,
        "ai_news": ai,
        "sports_watch": sports,
        "markets": {
            "fx": [
                gbp_usd,
                usd_gbp,
                zar_usd,
                usd_zar,
                {
                    "pair": "ZIG → ZAR",
                    "rate": None,
                    "daily_change_pct": None,
                    "direction": "unavailable",
                    "provider": "none",
                    "sparkline_points": [],
                    "available": False,
                    "note": "No public machine-readable ZiG/ZAR cross rate on this run",
                },
            ],
            "gold": gold,
        },
        "weather": [
            fetch_weather("London", 51.5074, -0.1278),
            fetch_weather("Harare", -17.8252, 31.0335),
        ],
        "fetched_at": datetime.now(ZoneInfo("Europe/London")).isoformat(timespec="seconds"),
    }


def main() -> int:
    output_json = ROOT / "today_input.json"
    print("Fetching live briefing data…")
    payload = build_payload()
    output_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {output_json}")
    from cowork_standalone_generator import main as generate

    sys.argv = ["cowork_standalone_generator.py", str(output_json), str(ROOT)]
    return generate()


if __name__ == "__main__":
    sys.exit(main())
