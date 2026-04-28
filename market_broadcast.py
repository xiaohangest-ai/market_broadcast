#!/usr/bin/env python3
"""
市场播报 Agent (GitHub Actions 版)
数据源:
  - BTC AHR999: CryptoCompare
  - 美股/港股:  Stooq CSV API (直接返回日涨跌幅,无需 key,云端 IP 友好)
推送:  飞书自定义机器人 Webhook

v3 变更:
  - 抛弃 Yahoo Finance(v7 要 crumb 在 GitHub Actions 全部 401)
  - 改用 Stooq /q/l 接口,字段 p 直接是日涨跌幅百分比
  - 11 只股票一次 CSV 请求拿完
注意:Stooq 数据延迟约 15 分钟,但 4 次/天的播报不敏感
"""
import os, time, math, hmac, hashlib, base64, json
from datetime import datetime, timezone, timedelta
import urllib.request, urllib.error, urllib.parse
import csv, io

TZ_CST = timezone(timedelta(hours=8))
now_cst = datetime.now(TZ_CST)
TIME_STR = now_cst.strftime("%Y-%m-%d %H:%M")


def fetch_text(url, timeout=20):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_json(url, timeout=20):
    return json.loads(fetch_text(url, timeout))


# ── 1. BTC AHR999 ─────────────────────────────────────────────────────────────
def get_ahr999():
    try:
        data = fetch_json(
            "https://min-api.cryptocompare.com/data/v2/histoday"
            "?fsym=BTC&tsym=USD&limit=200"
        )
        closes = [d["close"] for d in data["Data"]["Data"] if d["close"] > 0]
        closes = closes[-200:]
        if len(closes) < 200:
            raise ValueError(f"仅 {len(closes)} 条，需要 200 条")

        current_price = closes[-1]
        avg_200 = sum(closes) / len(closes)

        genesis = datetime(2009, 1, 3, tzinfo=TZ_CST)
        days = (now_cst - genesis).days
        fitting_price = 10 ** (5.84 * math.log10(days) - 17.01)
        ahr999 = round(
            (current_price / avg_200) * (current_price / fitting_price), 4
        )

        if ahr999 < 0.45:
            signal = "🟢 抄底"
        elif ahr999 < 1.2:
            signal = "🟡 定投"
        else:
            signal = "🔴 泡沫"

        return f"{int(current_price):,}", ahr999, signal

    except Exception as e:
        print(f"[BTC] 拉取失败: {e}")
        return "N/A", "N/A", ""


# ── 2. Stooq CSV 批量获取 ─────────────────────────────────────────────────────
def get_quotes_batch(stooq_symbols):
    """
    stooq_symbols: list of stooq symbols, e.g. ["aapl.us", "0700.hk"]
    Stooq /q/l 接口字段 f=sd2t2ohlcp:
      s   = symbol
      d2  = date
      t2  = time
      o   = open
      h   = high
      l   = low
      c   = close (当前价)
      p   = percent change (日涨跌幅,带百分号或纯数字)
    返回 dict: {symbol_lower: {"price": float, "pct": float}}
    """
    try:
        url = (
            "https://stooq.com/q/l/?"
            + urllib.parse.urlencode({
                "s": ",".join(stooq_symbols),
                "f": "sd2t2ohlcp",
                "h": "",
                "e": "csv",
            })
        )
        text = fetch_text(url)
        result = {}
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            sym = (row.get("Symbol") or "").lower()
            close_str = row.get("Close")
            pct_str = (row.get("Percent") or "").strip().rstrip("%")
            if not sym or not close_str or close_str == "N/D":
                continue
            try:
                price = float(close_str)
                pct = float(pct_str) if pct_str and pct_str != "N/D" else 0.0
                result[sym] = {"price": price, "pct": pct}
            except ValueError:
                continue
        return result
    except Exception as e:
        print(f"[batch quote] 拉取失败: {e}")
        return {}


# ── 3. 美股七巨头 ─────────────────────────────────────────────────────────────
# (display_symbol, stooq_symbol)
US_STOCKS = [
    ("AAPL",  "aapl.us"),
    ("MSFT",  "msft.us"),
    ("GOOGL", "googl.us"),
    ("AMZN",  "amzn.us"),
    ("META",  "meta.us"),
    ("NVDA",  "nvda.us"),
    ("TSLA",  "tsla.us"),
]

def build_us_lines(quotes):
    lines = []
    for display, stooq in US_STOCKS:
        q = quotes.get(stooq)
        if q is None:
            lines.append(f"{display:<6} N/A")
        else:
            sign = "+" if q["pct"] >= 0 else ""
            lines.append(f"{display:<6} ${q['price']:<10.2f} {sign}{q['pct']:.2f}%")
    return lines


# ── 4. 港股四只 ───────────────────────────────────────────────────────────────
# (name, code, stooq_symbol)
HK_STOCKS = [
    ("腾讯", "0700", "0700.hk"),
    ("阿里", "9988", "9988.hk"),
    ("美团", "3690", "3690.hk"),
    ("小米", "1810", "1810.hk"),
]

def build_hk_lines(quotes):
    lines = []
    for name, code, stooq in HK_STOCKS:
        q = quotes.get(stooq)
        if q is None:
            lines.append(f"{name}  {code}   N/A")
        else:
            sign = "+" if q["pct"] >= 0 else ""
            lines.append(f"{name}  {code}   ${q['price']:<10.2f} {sign}{q['pct']:.2f}%")
    return lines


# ── 5. 拼装消息 ───────────────────────────────────────────────────────────────
def build_message():
    btc_price, ahr999, signal = get_ahr999()

    all_stooq = [s for _, s in US_STOCKS] + [s for _, _, s in HK_STOCKS]
    quotes = get_quotes_batch(all_stooq)

    return "\n".join([
        "📊 多市场监控",
        f"<北京时间 {TIME_STR}>",
        "",
        "━━━ BTC ━━━",
        f"价格: ${btc_price}",
        f"AHR999: {ahr999} {signal}",
        "",
        "━━━ 美股七巨头 (USD) ━━━",
        *build_us_lines(quotes),
        "",
        "━━━ 港股 (HKD) ━━━",
        *build_hk_lines(quotes),
    ])


# ── 6. 飞书推送 ───────────────────────────────────────────────────────────────
def push_lark(text):
    webhook = os.environ.get("LARK_WEBHOOK_URL", "")
    if not webhook:
        print("⚠️  LARK_WEBHOOK_URL 未设置")
        print(text)
        return

    body = {"msg_type": "text", "content": {"text": text}}

    secret = os.environ.get("LARK_SIGN_SECRET", "")
    if secret:
        ts = str(int(time.time()))
        string_to_sign = f"{ts}\n{secret}"
        sig = base64.b64encode(
            hmac.new(
                string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256
            ).digest()
        ).decode()
        body["timestamp"] = ts
        body["sign"] = sig

    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        if result.get("code") == 0:
            print("已推送 ✅")
            print(text)
        else:
            print(f"推送失败 code={result.get('code')} msg={result.get('msg')}")
            print("payload:", json.dumps(body, ensure_ascii=False))
    except urllib.error.HTTPError as e:
        body_str = e.read().decode(errors="replace") if e.fp else ""
        print(f"推送失败 HTTP {e.code}: {body_str}")
        print("payload:", json.dumps(body, ensure_ascii=False))
    except Exception as e:
        print(f"推送异常: {e}")
        print("payload:", json.dumps(body, ensure_ascii=False))


if __name__ == "__main__":
    push_lark(build_message())
