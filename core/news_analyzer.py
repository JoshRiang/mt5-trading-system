"""
News analyzer.
Scrapes news from ForexFactory, Investing.com, Reuters.
Maps news impact to 6 variables per asset.
"""

import re
import json
from typing import Dict, List, Optional
from datetime import datetime
import requests
from bs4 import BeautifulSoup

try:
    from core.symbols import resolve_symbol
except ImportError:  # run as a bare script / different sys.path
    from symbols import resolve_symbol  # type: ignore


class NewsAnalyzer:
    """Analyzes news and maps impact to asset states."""
    
    # Asset-specific news impact mapping
    # Format: {keyword: {symbol: impact_score}}
    NEWS_RULES = {
        # BTC
        "bitcoin": {"BTCUSD": 0.7},
        "btc": {"BTCUSD": 0.7},
        "crypto": {"BTCUSD": 0.5},
        "ethereum": {"BTCUSD": 0.4},
        "etf": {"BTCUSD": 0.6},
        "regulation": {"BTCUSD": -0.5},
        "ban": {"BTCUSD": -0.8},
        "adopt": {"BTCUSD": 0.6},
        
        # Gold
        "gold": {"XAUUSD": 0.8},
        "xau": {"XAUUSD": 0.8},
        "fed": {"XAUUSD": -0.5},  # rate hike = bearish gold
        "rate hike": {"XAUUSD": -0.7},
        "rate cut": {"XAUUSD": 0.7},
        "inflation": {"XAUUSD": 0.5},
        "recession": {"XAUUSD": 0.8},
        "war": {"XAUUSD": 0.7},
        "geopolitical": {"XAUUSD": 0.5},
        
        # Oil — keyed on the BOT-side name (OILUSD). resolve_symbol() maps this
        # to the broker symbol (USOIL) at the analyze_batch boundary, so oil
        # news is never dropped just because the broker spells it differently.
        "oil": {"OILUSD": 0.8},
        "opec": {"OILUSD": 0.9},
        "opec+": {"OILUSD": 0.9},
        "crude": {"OILUSD": 0.7},
        "supply": {"OILUSD": 0.5},
        "demand": {"OILUSD": 0.5},
        "production": {"OILUSD": -0.6},  # increase = bearish
        
        # Silver
        "silver": {"XAGUSD": 0.7},
        "xag": {"XAGUSD": 0.7},
        "industrial": {"XAGUSD": 0.4},
    }
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
    
    def analyze_headline(self, headline: str, source: str = "unknown") -> Dict[str, float]:
        """
        Analyze a news headline and return impact scores per asset.
        
        Returns:
            {"BTCUSD": 0.7, "XAUUSD": -0.3, ...}
        """
        headline_lower = headline.lower()
        impacts = {}

        for keyword, asset_impacts in self.NEWS_RULES.items():
            # Word-ish match: keyword must not be embedded inside a longer
            # word (e.g. "ban" must not fire on "bank holiday").
            if re.search(r"(?<![a-z])" + re.escape(keyword) + r"(?![a-z])",
                         headline_lower):
                for asset, impact in asset_impacts.items():
                    # Accumulate impact (capped at ±1.0)
                    current = impacts.get(asset, 0.0)
                    impacts[asset] = max(-1.0, min(1.0, current + impact))
        
        return impacts
    
    def fetch_forexfactory_rss(self) -> List[Dict]:
        """Fetch ForexFactory calendar/news (RSS deprecated 403/404 — use ff calendar XML)."""
        news_items = []
        try:
            url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
            response = self.session.get(url, timeout=15)
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, "xml")
                for ev in soup.find_all("event")[:30]:
                    title = ev.title.text if ev.title else ""
                    country = ev.country.text if ev.country else ""
                    date = ev.date.text if ev.date else ""
                    impact = ev.impact.text if ev.impact else ""
                    news_items.append({
                        "source": "forexfactory",
                        "title": f"[{country}/{impact}] {title}",
                        "link": "",
                        "published": date,
                        "headline": f"{title} ({country} {impact})",
                    })
        except Exception as e:
            print(f"Error fetching ForexFactory calendar: {e}")
        
        return news_items
    
    def fetch_investingcom_rss(self) -> List[Dict]:
        """Fetch news from Investing.com RSS."""
        news_items = []
        try:
            url = "https://www.investing.com/rss/news.rss"
            response = self.session.get(url, timeout=10)
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, "xml")
                for item in soup.find_all("item"):
                    news_items.append({
                        "source": "investingcom",
                        "title": item.title.text if item.title else "",
                        "link": item.link.text if item.link else "",
                        "published": item.pubDate.text if item.pubDate else "",
                        "headline": item.title.text if item.title else "",
                    })
        except Exception as e:
            print(f"Error fetching Investing.com RSS: {e}")
        
        return news_items
    
    def fetch_reuters_headlines(self) -> List[Dict]:
        """Fetch top headlines from Reuters."""
        news_items = []
        try:
            url = "https://www.reuters.com/world/"
            response = self.session.get(url, timeout=10)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                for headline in soup.find_all("h3")[:10]:
                    text = headline.get_text(strip=True)
                    if text:
                        news_items.append({
                            "source": "reuters",
                            "title": text,
                            "link": "",
                            "published": datetime.now().isoformat(),
                            "headline": text,
                        })
        except Exception as e:
            print(f"Error fetching Reuters: {e}")
        
        return news_items
    
    def fetch_all_news(self) -> List[Dict]:
        """Fetch news from all sources."""
        news = []
        news.extend(self.fetch_forexfactory_rss())
        news.extend(self.fetch_investingcom_rss())
        news.extend(self.fetch_reuters_headlines())
        return news
    
    def analyze_batch(self, news_items: List[Dict]) -> Dict[str, Dict]:
        """
        Analyze a batch of news items.
        
        Returns:
            {symbol: {"total_impact": float, "news_count": int, "items": [...]}}
        """
        results = {}
        
        for item in news_items:
            impacts = self.analyze_headline(item.get("headline", ""), item.get("source", ""))
            
            for symbol, impact in impacts.items():
                if symbol not in results:
                    # Keep the CANONICAL bot-side name as the key (state machine,
                    # news_log and the dashboard are all keyed on OILUSD), and
                    # carry the resolved BROKER symbol alongside so any MT5
                    # handoff trades the real symbol instead of dropping the
                    # news. Resolving here is the boundary: news never silently
                    # loses its oil leg to an OILUSD/USOIL mismatch.
                    results[symbol] = {"total_impact": 0.0, "news_count": 0,
                                       "items": [], "mt5_symbol": resolve_symbol(symbol)}
                
                results[symbol]["total_impact"] += impact
                results[symbol]["news_count"] += 1
                results[symbol]["items"].append({
                    "headline": item.get("headline", ""),
                    "source": item.get("source", ""),
                    "impact": impact,
                    "timestamp": datetime.now().isoformat(),
                })
        
        return results


if __name__ == "__main__":
    analyzer = NewsAnalyzer()
    
    # Test
    test_headlines = [
        "Fed raises interest rates by 25bps",
        "Bitcoin ETF approved by SEC",
        "OPEC+ announces production cuts",
        "Gold surges on recession fears",
    ]
    
    for headline in test_headlines:
        impacts = analyzer.analyze_headline(headline)
        print(f"{headline}: {impacts}")
