#!/usr/bin/env python3
"""
市场播报 Agent
数据源: CryptoCompare (BTC) + Yahoo Finance (美股/港股)
推送:  飞书自定义机器人 Webhook
"""
import os, time, math, hmac, hashlib, base64, json
from datetime import datetime, timezone, timedelta
import urllib.request, urllib.error

TZ_CST = timezone(timedelta(hours=8))
now_cst = datetime.now(TZ_CST)
TIME_STR = now_cst.strftime("%Y-%m-%d %H:%M")


def fetch_json(url, timeout=20):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


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


# ── 2. Yahoo Finance 通用 ──────────────────────────────────────────────────────
def get_yahoo(symbol):
    try:
        data = fetch_json(
            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{symbol}?interval=1d&range=2d"
        )
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev  = meta.get("chartPreviousClose")
        if price is None or prev is None or prev == 0:
            raise ValueError("price/prev 为空")
        return price, (price - prev) / prev * 100
    except Exception as e:
        print(f"[{symbol}] 拉取失败: {e}")
        return None, None


# ── 3. 美股七巨头 ─────────────────────────────────────────────────────────────
US_STOCKS = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"]

def build_us_lines():
    lines = []
    for sym in US_STOCKS:
        price, pct = get_yahoo(sym)
        if price is None:
            lines.append(f"{sym:<6} N/A")
        else:
            sign = "+" if pct >= 0 else ""
            lines.append(f"{sym:<6} ${price:<10.2f} {sign}{pct:.2f}%")
    return lines


# ── 4. 港股五只 ───────────────────────────────────────────────────────────────
HK_STOCKS = [
    ("0700.HK", "腾讯",   "0700"),
    ("9988.HK", "阿里",   "9988"),
    ("3690.HK", "美团",   "3690"),
    ("1810.HK", "小米",   "1810"),
    ("1211.HK", "比亚迪", "1211"),
]

def build_hk_lines():
    lines = []
    for sym_full, name, code in HK_STOCKS:
        price, pct = get_yahoo(sym_full)
        if price is None:
            lines.append(f"{name}  {code}   N/A")
        else:
            sign = "+" if pct >= 0 else ""
            lines.append(f"{name}  {code}   ${price:<10.2f} {sign}{pct:.2f}%")
    return lines


# ── 5. 拼装消息 ───────────────────────────────────────────────────────────────
def build_message():
    btc_price, ahr999, signal = get_ahr999()
    return "\n".join([
        "📊 多市场监控",
        f"<北京时间 {TIME_STR}>",
        "",
        "━━━ BTC ━━━",
        f"价格: ${btc_price}",
        f"AHR999: {ahr999} {signal}",
        "",
        "━━━ 美股七巨头 (USD) ━━━",
        *build_us_lines(),
        "",
        "━━━ 港股 (HKD) ━━━",
        *build_hk_lines(),
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
