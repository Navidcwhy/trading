"""News + earnings sources.

Crypto: public RSS headlines (no key).  Stocks: Finnhub (free key, FINNHUB_API_KEY).
Gold/silver: Finnhub general market news, matched on the metal's names.
Known events the user has marked as handled (config news_acknowledged) are excused, see NewsAck.
Finnhub files articles about other companies under a ticker too (an IPO next door, a peer's
downgrade), so a stock article only counts when its headline names the company, see company_names.
All lookups return None when the source is unavailable, so the Filter can fail closed.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import re
import time
import xml.etree.ElementTree as ET

from ..util import HttpError, http_json, http_text

CRYPTO_NAMES = {
    "BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "SOL": ["solana"], "XRP": ["xrp", "ripple"],
    "BNB": ["bnb", "bnb chain"], "DOGE": ["dogecoin"], "ADA": ["cardano"], "TRX": ["tron"],
    "AVAX": ["avalanche"], "LINK": ["chainlink"], "DOT": ["polkadot"], "TON": ["toncoin", "ton"],
    "SUI": ["sui"], "LTC": ["litecoin"], "BCH": ["bitcoin cash"], "SHIB": ["shiba inu"],
    "PEPE": ["pepe"], "NEAR": ["near protocol"], "APT": ["aptos"], "ARB": ["arbitrum"],
    "OP": ["optimism"], "UNI": ["uniswap"], "AAVE": ["aave"], "HBAR": ["hedera"], "XLM": ["stellar"],
    "ENA": ["ethena"], "WLD": ["worldcoin", "world network"], "FIL": ["filecoin"], "ICP": ["internet computer"],
    "ETC": ["ethereum classic"], "TAO": ["bittensor"], "INJ": ["injective"], "SEI": ["sei"],
    "TIA": ["celestia"], "HYPE": ["hyperliquid"], "ONDO": ["ondo"], "TRUMP": ["trump coin", "$trump"],
    "WIF": ["dogwifhat"], "BONK": ["bonk"], "FET": ["fetch.ai", "artificial superintelligence alliance"],
    "RENDER": ["render network"], "XMR": ["monero"], "ATOM": ["cosmos"], "ALGO": ["algorand"],
    # gold tokens (scouted as gold, not as crypto): trouble at the token itself, not at its issuer's
    # other business, which would block gold on every Tether or Paxos headline
    "XAUT": ["tether gold"], "PAXG": ["pax gold", "paxos gold"],
}


def keyword_regex(words: list[str]) -> re.Pattern:
    alts = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(alts) + r")\b", re.IGNORECASE)


# first words too common to stand for a company on their own ("Advanced" Micro Devices,
# "Space" Exploration Technologies) and fund sponsors (a "Direxion" headline is not about every Direxion fund)
GENERIC_FIRST_WORDS = {
    "advanced", "american", "applied", "united", "general", "first", "global", "international", "national",
    "new", "state", "the", "space", "super", "digital", "energy", "health", "financial", "capital",
    "technology", "technologies", "systems", "data", "power", "bank", "royal", "standard", "strategy",
    "rocket", "life", "smart", "real", "open", "micro", "quantum", "nuclear", "solar", "clean", "green",
    "gold", "silver", "core", "prime", "alpha", "union", "liberty", "select", "daily", "north", "south",
    "east", "west", "pacific", "great", "world", "universal", "ultra", "china", "direxion", "proshares",
    "ishares", "spdr", "invesco", "vanguard", "schwab", "fidelity", "wisdomtree", "vaneck", "tradr",
    "defiance", "roundhill", "graniteshares",
}
_SHARE_CLASS = re.compile(r"\s+(?:class\s+[a-z]\b|common\s+stock|ordinary\s+shares|american\s+depositary|"
                          r"depositary\s+shares|series\s+\d|units?\b|warrants?\b|rights?\b).*$", re.IGNORECASE)
_CORP_SUFFIX = re.compile(r"[\s,]+(?:inc|incorporated|corp|corporation|co|company|ltd|limited|plc|n\.?v|s\.?a|"
                          r"ag|se|holdings?|group|pbc|llc|l\.?p|trust|etf|etn|fund|shares)\.?$", re.IGNORECASE)
_FUND = re.compile(r"\b(?:etf|etn|trust|fund)\b", re.IGNORECASE)


def company_names(full_name: str | None) -> list[str]:
    """The names headlines use for a listed company or fund, from its listed name:
    'NVIDIA Corporation Common Stock' -> ['NVIDIA'], 'Palo Alto Networks, Inc.' -> ['Palo Alto Networks',
    'Palo'], 'Advanced Micro Devices, Inc.' -> ['Advanced Micro Devices', 'Advanced Micro'],
    'Invesco QQQ Trust, Series 1' -> ['Invesco QQQ']. Empty when there is nothing usable."""
    if not full_name:
        return []
    fund = bool(_FUND.search(full_name)) and not re.search(r"common\s+stock", full_name, re.IGNORECASE)
    n = _SHARE_CLASS.sub("", full_name.split(",")[0].strip()).strip()
    while True:
        cut = _CORP_SUFFIX.sub("", n).strip()
        if cut == n:
            break
        n = cut
    n = re.sub(r"\.com$", "", n, flags=re.IGNORECASE).strip()
    if len(n) < 3:
        return []
    names, words = [n], n.split()
    if not fund and len(words) >= 2:
        if len(words[0]) >= 4 and words[0].lower() not in GENERIC_FIRST_WORDS:
            names.append(words[0])
        elif len(words) >= 3:
            names.append(" ".join(words[:2]))
    return names


def about_regex(symbol: str, names: list[str]) -> re.Pattern:
    """Matches a headline that names the stock: its ticker as written (NVDA, $NVDA, NASDAQ:NVDA,
    case-sensitive, two letters or more) or one of its names in any case."""
    parts = []
    if len(symbol) >= 2:
        parts.append(rf"(?-i:(?<![\w.$])\$?{re.escape(symbol)}\b)")
    alts = sorted({re.escape(n) for n in names if n}, key=len, reverse=True)
    if alts:
        parts.append(r"\b(?:" + "|".join(alts) + r")\b")
    return re.compile("|".join(parts) or r"(?!x)x", re.IGNORECASE)


class NewsAck:
    """Known events the user has marked as handled (config filter.news_acknowledged).

    In a headline that matches every regex in an entry's `match_all` and none in its
    `unless` (signs of a new incident, e.g. "hacked again"), the entry's `ignore_words`
    stop counting as risk. Any other risk word still counts, so "Binance suspends
    withdrawals after $6.3B hack" still blocks. An entry applies through its `until`
    date (local time). Each excused headline is queued once for the Filter to log."""

    def __init__(self, entries: list[dict] | None = None):
        self.entries = []
        for e in entries or []:
            until = dt.datetime.fromisoformat(e["until"])
            if len(e["until"]) == 10:                      # a bare date covers that whole day
                until += dt.timedelta(days=1)
            self.entries.append({"match": [re.compile(p, re.IGNORECASE) for p in e["match_all"]],
                                 "unless": [re.compile(p, re.IGNORECASE) for p in e.get("unless", [])],
                                 "ignore": {w.lower() for w in e["ignore_words"]},
                                 "until": until.timestamp(), "until_str": e["until"],
                                 "note": e.get("note", "")})
        self._seen: set[str] = set()
        self._new: list[dict] = []

    def active(self) -> list[dict]:
        return [e for e in self.entries if time.time() < e["until"]]

    def keyword(self, text: str, risk_re: re.Pattern, title: str = "") -> str | None:
        """The first risk word in `text` that still counts after known events, or None."""
        found = [m.group(1).lower() for m in risk_re.finditer(text)]
        if not found:
            return None
        known = [e for e in self.active() if all(p.search(text) for p in e["match"])
                 and not any(p.search(text) for p in e["unless"])]
        ignore = set().union(*(e["ignore"] for e in known))
        left = [w for w in found if w not in ignore]
        if left:
            return left[0]
        title = (title or text)[:160]
        if title not in self._seen:
            self._seen.add(title)
            self._new.append({"title": title, "ignored": sorted(set(found)),
                              "note": "; ".join(e["note"] for e in known)})
        return None

    def take_new(self) -> list[dict]:
        """Headlines excused since the last call."""
        out, self._new = self._new, []
        return out


class CryptoNews:
    def __init__(self, feeds: list[str], risk_words: list[str], venue_names: dict, acks: NewsAck | None = None):
        self.feeds = feeds
        self.risk_re = keyword_regex(risk_words)
        self.venue_names = venue_names
        self.acks = acks or NewsAck()
        self._items: list[dict] = []
        self._fetched = 0.0
        self._ok_feeds = 0

    def refresh(self, ttl: int = 600):
        if time.time() - self._fetched < (ttl if self._ok_feeds else 120):
            return
        items, ok = [], 0
        for url in self.feeds:
            try:
                root = ET.fromstring(http_text(url, timeout=15))
                ok += 1
            except (HttpError, ET.ParseError):
                continue
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                pub = it.findtext("pubDate")
                try:
                    ts = email.utils.parsedate_to_datetime(pub).timestamp() if pub else None
                except (TypeError, ValueError):
                    ts = None
                if title and ts:
                    items.append({"title": title, "ts": ts, "source": url.split("/")[2]})
        self._items, self._ok_feeds, self._fetched = items, ok, time.time()

    def risk_for(self, base: str, venue: str, lookback_h: float) -> list[dict] | None:
        """Risk headlines mentioning the asset (or its venue). None = no source reachable."""
        self.refresh()
        if not self._ok_feeds:
            return None
        cutoff = time.time() - lookback_h * 3600
        names = [n for n in CRYPTO_NAMES.get(base, [])]
        name_re = keyword_regex(names) if names else None
        ticker_re = re.compile(rf"\b\$?{re.escape(base)}\b") if len(base) >= 3 else None  # case-sensitive
        venue_re = keyword_regex(self.venue_names.get(venue.split("_")[0], [])) \
            if self.venue_names.get(venue.split("_")[0]) else None
        hits = []
        for it in self._items:
            if it["ts"] < cutoff:
                continue
            t = it["title"]
            about = (name_re and name_re.search(t)) or (ticker_re and ticker_re.search(t))
            about_venue = venue_re and venue_re.search(t)
            if not (about or about_venue):
                continue
            kw = self.acks.keyword(t, self.risk_re, t)
            if kw:
                hits.append({"title": t, "keyword": kw, "source": it["source"],
                             "age_h": round((time.time() - it["ts"]) / 3600, 1),
                             "scope": "asset" if about else "venue"})
        return hits


class Finnhub:
    """Finnhub articles have a headline and a summary. Risk words count in either, except the
    `headline_only` ones (config news_headline_only_keywords): words like 'offering' or 'downgrade'
    turn up in ordinary summaries ("offering an entry point", a peer's downgrade), so they only count
    in the headline itself."""
    BASE = "https://finnhub.io/api/v1"

    def __init__(self, key: str | None, risk_words: list[str], acks: NewsAck | None = None,
                 headline_only: list[str] | tuple = ()):
        self.key = key
        self.risk_re = keyword_regex(risk_words)
        only = {w.lower() for w in headline_only}
        body = [w for w in risk_words if w.lower() not in only]
        self.body_re = keyword_regex(body) if body else None
        self.acks = acks or NewsAck()
        self._earn: tuple[float, str, dict] | None = None
        self._market: tuple[float, list] | None = None
        self._other: list[dict] = []
        self._other_seen: set[tuple[str, str]] = set()

    def _risk_word(self, headline: str, summary: str) -> str | None:
        """The first risk word that counts: any in the headline, the others in the summary too."""
        kw = self.acks.keyword(headline, self.risk_re, headline)
        if kw or not self.body_re or not summary:
            return kw
        return self.acks.keyword(f"{headline} {summary}", self.body_re, headline)

    def take_other(self) -> list[dict]:
        """Risk articles skipped since the last call because their headline is about another company."""
        out, self._other = self._other, []
        return out

    @property
    def available(self) -> bool:
        return bool(self.key)

    def _get(self, path, params):
        return http_json(self.BASE + path, params, headers={"X-Finnhub-Token": self.key})

    def earnings_dates(self, start: dt.date, end: dt.date) -> dict | None:
        """symbol -> list of (date, hour) between start..end. One call covers all symbols."""
        if not self.key:
            return None
        k = f"{start}:{end}"
        if self._earn and self._earn[1] == k and time.time() - self._earn[0] < 6 * 3600:
            return self._earn[2]
        try:
            data = self._get("/calendar/earnings", {"from": str(start), "to": str(end)})
        except HttpError:
            return None
        out: dict[str, list] = {}
        for e in data.get("earningsCalendar") or []:
            out.setdefault(e.get("symbol", ""), []).append((e.get("date"), e.get("hour")))
        self._earn = (time.time(), k, out)
        return out

    def news_risk(self, symbol: str, lookback_h: float, names: list[str] | None = None) -> list[dict] | None:
        """Risk articles about `symbol`. With `names` (the company's names, see company_names) an
        article only counts when its headline names the company or its ticker; without them every
        article Finnhub files under the ticker counts."""
        if not self.key:
            return None
        today = dt.date.today()
        try:
            rows = self._get("/company-news", {"symbol": symbol, "from": str(today - dt.timedelta(days=2)),
                                               "to": str(today)})
        except HttpError:
            return None
        about = about_regex(symbol, names) if names else None
        cutoff = time.time() - lookback_h * 3600
        hits = []
        for r in rows or []:
            if (r.get("datetime") or 0) < cutoff:
                continue
            headline, summary = r.get("headline") or "", r.get("summary") or ""
            if about and not about.search(headline):
                m = self.risk_re.search(f"{headline} {summary}")
                if m and (symbol, headline) not in self._other_seen:
                    self._other_seen.add((symbol, headline))
                    self._other.append({"symbol": symbol, "keyword": m.group(1).lower(), "title": headline[:160]})
                continue
            kw = self._risk_word(headline, summary)
            if kw:
                hits.append({"title": headline[:160], "keyword": kw,
                             "source": r.get("source"), "age_h": round((time.time() - r["datetime"]) / 3600, 1)})
        return hits

    def market_news_risk(self, names: list[str], lookback_h: float) -> list[dict] | None:
        """Risk headlines in Finnhub's general market news that mention one of `names`
        (e.g. gold/XAU). Fetched at most every 10 min. None = source unavailable."""
        if not self.key or not names:
            return None
        if not self._market or time.time() - self._market[0] > 600:
            try:
                rows = self._get("/news", {"category": "general"})
            except HttpError:
                return None
            if not isinstance(rows, list):
                return None
            self._market = (time.time(), rows)
        name_re = keyword_regex(names)
        cutoff = time.time() - lookback_h * 3600
        hits = []
        for r in self._market[1]:
            if (r.get("datetime") or 0) < cutoff:
                continue
            headline, summary = r.get("headline") or "", r.get("summary") or ""
            if not name_re.search(f"{headline} {summary}"):
                continue
            kw = self._risk_word(headline, summary)
            if kw:
                hits.append({"title": r.get("headline", "")[:160], "keyword": kw,
                             "source": r.get("source"), "scope": "asset",
                             "age_h": round((time.time() - r["datetime"]) / 3600, 1)})
        return hits
