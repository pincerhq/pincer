"""Stock and crypto price skill - fetch prices via Yahoo Finance and CoinGecko."""

import json
import urllib.request


def get_stock_price(ticker: str) -> dict:
    """Get stock price from Yahoo Finance. Returns ticker, price, previous_close, change, change_percent, currency, name."""
    if not ticker or not str(ticker).strip():
        return {"error": "ticker cannot be empty"}

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.strip()}?interval=1d&range=1d"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Pincer/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP error {e.code}: invalid ticker or unavailable"}
    except urllib.error.URLError as e:
        return {"error": str(e.reason) if e.reason else "network error"}
    except (json.JSONDecodeError, TimeoutError) as e:
        return {"error": str(e)}

    try:
        chart = data.get("chart", {})
        result = chart.get("result")
        if not result:
            return {"error": "invalid ticker or no data"}
        r = result[0]
        meta = r.get("meta", {})
        currency = meta.get("currency", "USD")
        name = meta.get("shortName", ticker)
        reg_price = meta.get("regularMarketPrice")
        prev_close = meta.get("previousClose")
        if reg_price is None:
            return {"error": "no price data for ticker"}
        price = float(reg_price)
        prev = float(prev_close) if prev_close is not None else price
        change = round(price - prev, 4)
        change_percent = round((change / prev * 100), 2) if prev else 0
        return {
            "ticker": ticker.upper(),
            "price": price,
            "previous_close": prev,
            "change": change,
            "change_percent": change_percent,
            "currency": currency,
            "name": name,
        }
    except (TypeError, KeyError, ValueError) as e:
        return {"error": f"failed to parse response: {e}"}


def get_crypto_price(coin: str = "bitcoin", currency: str = "usd") -> dict:
    """Get crypto price from CoinGecko. Returns coin, price, change_24h_percent, market_cap, currency."""
    if not coin or not str(coin).strip():
        return {"error": "coin cannot be empty"}

    coin_id = str(coin).strip().lower()
    curr = str(currency).strip().lower()
    url = (
        f"https://api.coingecko.com/api/v3/simple/price"
        f"?ids={coin_id}&vs_currencies={curr}"
        f"&include_24hr_change=true&include_market_cap=true"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Pincer/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP error {e.code}: invalid coin or unavailable"}
    except urllib.error.URLError as e:
        return {"error": str(e.reason) if e.reason else "network error"}
    except (json.JSONDecodeError, TimeoutError) as e:
        return {"error": str(e)}

    try:
        if coin_id not in data:
            return {"error": f"invalid coin '{coin}'"}
        c = data[coin_id]
        price = c.get(curr)
        if price is None:
            return {"error": f"no price for currency '{currency}'"}
        price = float(price)
        change_24h = c.get(f"{curr}_24h_change")
        change_24h = float(change_24h) if change_24h is not None else 0
        market_cap = c.get(f"{curr}_market_cap")
        market_cap = float(market_cap) if market_cap is not None else None
        return {
            "coin": coin_id,
            "price": price,
            "change_24h_percent": round(change_24h, 2),
            "market_cap": market_cap,
            "currency": curr,
        }
    except (TypeError, KeyError, ValueError) as e:
        return {"error": f"failed to parse response: {e}"}
