#!/usr/bin/env python3
"""
市场播报 Agent (GitHub Actions 版) v4 - 新浪财经
数据源:
  - BTC AHR999: CryptoCompare
  - 美股:        新浪财经 hq.sinajs.cn (gb_ 前缀)
  - 港股:        新浪财经 hq.sinajs.cn (hk 前缀)
推送:  飞书自定义机器人 Webhook

为什么换新浪:
  - Yahoo v7: GitHub IP 全 401(crumb 鉴权)
  - Yahoo v8: chartPreviousClose 字段语义漂移导致涨跌幅错位
  - Stooq:    批量请求格式诡异 + 美港股数据缺失返回 N/D
  - 新浪:     国内服务,GitHub IP 可访问,字段直接给涨跌幅,关键是
              需要带 Referer 头,否则 403
"""
import os, time, math, hmac, hashlib, base64, json, re
from datetime import datetime, timezone, timedelta
import urllib.request, urllib.error, urllib.parse

VERSION = "v4-sina"

TZ_CST = timezone(timedelta(hours=8))
now_cst = datetime.now(TZ_CST)
TIME_STR = now_cst.strftime("%Y-%m-%d %H:%M")


def fetch_text(url, timeout=20, headers=None):
    base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }
    if headers:
        base_headers.update(headers)
    req = urllib.request.Request(url, headers=base_headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        # 新浪返回 GBK,需要单独处理
        raw = r.read()
        for enc in ("utf-8", "gbk", "gb18030"):
            try:
                return r.status, raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return r.status, raw.decode("utf-8", errors="replace")


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


# ── 2. 新浪财经批量获取 ────────────────────────────────────────────────────────
SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn/",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

def parse_sina_response(text):
    """
    新浪返回格式(每只股票一行):
      var hq_str_gb_aapl="苹果,210.50,1.23,1.50,...";
      var hq_str_hk00700="腾讯控股,TENCENT,420.0,425.0,418.0,...";
    返回 {sina_key: raw_fields_list}
    """
    result = {}
    for line in text.split("\n"):
        m = re.match(r'var hq_str_([^=]+)="([^"]*)"', line.strip())
        if not m:
            continue
        key = m.group(1)
        fields = m.group(2).split(",")
        result[key] = fields
    return result


def get_us_quotes(us_symbols):
    """
    美股:  hq_str_gb_<lowercase symbol>
    字段:  0=name, 1=price, 2=changePct(%), 3=changeAmount, 4=preClose, ...
    """
    print(f"\n========== US (Sina) ==========")
    sina_keys = [f"gb_{s.lower()}" for s in us_symbols]
    url = "https://hq.sinajs.cn/list=" + ",".join(sina_keys)
    print(f"URL: {url}")

    result = {}
    try:
        status, text = fetch_text(url, headers=SINA_HEADERS)
        print(f"HTTP {status}, length={len(text)}")
        print(f"first 300: {text[:300]}")

        parsed = parse_sina_response(text)
        for sym in us_symbols:
            key = f"gb_{sym.lower()}"
            fields = parsed.get(key)
            if not fields or len(fields) < 4 or not fields[1]:
                print(f"  ✗ {sym}: 数据缺失 {fields[:5] if fields else None}")
                continue
            try:
                price = float(fields[1])
                pct = float(fields[2])
                if price == 0:
                    print(f"  ✗ {sym}: price=0")
                    continue
                result[sym] = {"price": price, "pct": pct}
                print(f"  ✓ {sym}: ${price} {pct:+.2f}%")
            except (ValueError, IndexError) as e:
                print(f"  ✗ {sym}: 解析失败 {e}")
    except Exception as e:
        print(f"❌ {type(e).__name__}: {e}")
    print(f"========== END US ==========\n")
    return result


def get_hk_quotes(hk_codes):
    """
    港股:  hq_str_hk<5位代码>
    字段:  0=name_en, 1=name_cn, 2=open, 3=preClose, 4=price, ...
    涨跌幅需要自己算 (price - preClose) / preClose * 100
    """
    print(f"\n========== HK (Sina) ==========")
    sina_keys = [f"hk{code}" for code in hk_codes]
    url = "https://hq.sinajs.cn/list=" + ",".join(sina_keys)
    print(f"URL: {url}")

    result = {}
    try:
        status, text = fetch_text(url, headers=SINA_HEADERS)
        print(f"HTTP {status}, length={len(text)}")
        print(f"first 300: {text[:300]}")

        parsed = parse_sina_response(text)
        for code in hk_codes:
            key = f"hk{code}"
            fields = parsed.get(key)
            if not fields or len(fields) < 6:
                print(f"  ✗ {code}: 数据缺失 {fields[:5] if fields else None}")
                continue
            try:
                pre_close = float(fields[3])
                price = float(fields[6]) if len(fields) > 6 else float(fields[4])
                if price == 0 or pre_close == 0:
                    print(f"  ✗ {code}: price/preClose 为 0")
                    continue
                pct = (price - pre_close) / pre_close * 100
                result[code] = {"price": price, "pct": pct}
                print(f"  ✓ {code}: HK${price} {pct:+.2f}%")
            except (ValueError, IndexError) as e:
                print(f"  ✗ {code}: 解析失败 {e} fields={fields[:8]}")
    except Exception as e:
        print(f"❌ {type(e).__name__}: {e}")
    print(f"========== END HK ==========\n")
    return result


# ── 3. 美股七巨头 ─────────────────────────────────────────────────────────────
US_STOCKS = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"]

def build_us_lines(quotes):
    lines = []
    for sym in US_STOCKS:
        q = quotes.get(sym)
        if q is None:
            lines.append(f"{sym:<6} N/A")
        else:
            sign = "+" if q["pct"] >= 0 else ""
            lines.append(f"{sym:<6} ${q['price']:<10.2f} {sign}{q['pct']:.2f}%")
    return lines


# ── 4. 港股四只 ───────────────────────────────────────────────────────────────
# (name, code 5位)
HK_STOCKS = [
    ("腾讯", "00700"),
    ("阿里", "09988"),
    ("美团", "03690"),
    ("小米", "01810"),
]

def build_hk_lines(quotes):
    lines = []
    for name, code in HK_STOCKS:
        q = quotes.get(code)
        if q is None:
            lines.append(f"{name}  {code[1:]}   N/A")
        else:
            sign = "+" if q["pct"] >= 0 else ""
            lines.append(f"{name}  {code[1:]}   ${q['price']:<10.2f} {sign}{q['pct']:.2f}%")
    return lines


# ── 5. 拼装消息 ───────────────────────────────────────────────────────────────
def build_message():
    print(f"\n>>> 脚本版本: {VERSION}")
    print(f">>> 时间: {TIME_STR}\n")

    btc_price, ahr999, signal = get_ahr999()
    us_quotes = get_us_quotes(US_STOCKS)
    hk_quotes = get_hk_quotes([code for _, code in HK_STOCKS])

    return "\n".join([
        "📊 多市场监控",
        f"<北京时间 {TIME_STR}>",
        "",
        "━━━ BTC ━━━",
        f"价格: ${btc_price}",
        f"AHR999: {ahr999} {signal}",
        "",
        "━━━ 美股七巨头 (USD) ━━━",
        *build_us_lines(us_quotes),
        "",
        "━━━ 港股 (HKD) ━━━",
        *build_hk_lines(hk_quotes),
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
    except urllib.error.HTTPError as e:
        body_str = e.read().decode(errors="replace") if e.fp else ""
        print(f"推送失败 HTTP {e.code}: {body_str}")
    except Exception as e:
        print(f"推送异常: {e}")


if __name__ == "__main__":
    push_lark(build_message())
