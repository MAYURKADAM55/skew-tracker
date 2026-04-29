"""
SkewHunter Peak Tracker - Cloud Version (Railway.app)
------------------------------------------------------
Reads credentials from environment variables.
Runs 24/7 on Railway. Monitors 9:20 AM - 3:20 PM IST Mon-Fri.
Refreshes Dhan token automatically every 23 hours.
Sends Telegram alerts only - NO auto-exit.
"""

import os
import re
import time
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests

# CONFIG FROM ENVIRONMENT VARIABLES
CLIENT_ID    = os.environ["CLIENT_ID"]
TELEGRAM_BOT = os.environ["TELEGRAM_BOT"]
TELEGRAM_ID  = os.environ["TELEGRAM_ID"]
DHAN_USER    = os.environ["DHAN_USER"]
DHAN_PASS    = os.environ["DHAN_PASS"]
DHAN_PIN     = os.environ["DHAN_PIN"]

TRAIL_PCT     = 30
ATM_RANGE_PTS = 300
MIN_PEAK_INR  = 500
CHECK_EVERY   = 60
IST           = ZoneInfo("Asia/Kolkata")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

_access_token     = os.environ.get("ACCESS_TOKEN", "")
_token_refresh_at = 0


def refresh_dhan_token() -> bool:
    global _access_token, _token_refresh_at
    log.info("Refreshing Dhan token via Playwright...")
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        import time as _time
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page    = browser.new_page()
            page.goto("https://web.dhan.co", timeout=30000)
            page.wait_for_load_state("networkidle")
            for sel in ['input[placeholder*="Mobile"]', 'input[type="tel"]', 'input[name="userId"]']:
                try:
                    page.fill(sel, DHAN_USER, timeout=3000)
                    break
                except Exception:
                    continue
            page.keyboard.press("Enter")
            _time.sleep(1)
            try:
                page.fill('input[type="password"]', DHAN_PASS, timeout=5000)
                page.keyboard.press("Enter")
                _time.sleep(2)
            except Exception:
                pass
            try:
                pin = page.wait_for_selector('input[maxlength="4"]', timeout=5000)
                if pin:
                    pin.fill(DHAN_PIN)
                    page.keyboard.press("Enter")
                    _time.sleep(2)
            except PWTimeout:
                pass
            page.goto("https://web.dhan.co/index/profile", timeout=30000)
            page.wait_for_load_state("networkidle")
            _time.sleep(2)
            try:
                page.click('text=DhanHQ Trading APIs', timeout=5000)
                _time.sleep(1)
            except Exception:
                pass
            try:
                page.fill('input[placeholder*="Name your Application"]', "SkewTracker")
                page.click('button:has-text("Generate API Key")')
                _time.sleep(2)
            except Exception:
                pass
            page.click('button:has-text("Generate Access Token")', timeout=10000)
            _time.sleep(2)
            try:
                pin = page.wait_for_selector('input[maxlength="4"]', timeout=5000)
                if pin:
                    pin.fill(DHAN_PIN)
                    page.keyboard.press("Enter")
                    _time.sleep(2)
            except PWTimeout:
                pass
            token = None
            for sel in ['input[readonly]', 'textarea[readonly]', 'code']:
                try:
                    el  = page.wait_for_selector(sel, timeout=3000)
                    tag = el.evaluate("e => e.tagName")
                    val = el.input_value() if tag in ["INPUT", "TEXTAREA"] else el.inner_text()
                    if len(val.strip()) > 20:
                        token = val.strip()
                        break
                except Exception:
                    continue
            browser.close()
            if token:
                _access_token     = token
                _token_refresh_at = _time.time() + 23 * 3600
                log.info("Token refreshed successfully.")
                send_telegram("Dhan token refreshed. Tracker active.")
                return True
            else:
                log.error("Could not extract token from page.")
                send_telegram("Dhan token refresh FAILED. Check credentials in Railway env vars.")
                return False
    except Exception as e:
        log.error("Token refresh error: %s", e)
        send_telegram(f"Token refresh error: {e}")
        return False


def get_headers() -> dict:
    return {"access-token": _access_token, "client-id": CLIENT_ID, "Content-Type": "application/json"}


def get_positions() -> list:
    try:
        r = requests.get("https://api.dhan.co/v2/positions", headers=get_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        log.error("get_positions failed: %s", e)
        return []


def get_nifty_spot() -> float:
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception as e:
        log.error("get_nifty_spot failed: %s", e)
        return 0.0


def send_telegram(msg: str) -> None:
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage", data={"chat_id": TELEGRAM_ID, "text": msg}, timeout=10)
    except Exception as e:
        log.error("Telegram failed: %s", e)


def extract_strike(symbol: str) -> int:
    m = re.search(r"-(d{4,5})-(CE|PE)", symbol)
    if m: return int(m.group(1))
    m = re.search(r"(d{5})(CE|PE)", symbol)
    return int(m.group(1)) if m else 0


def is_skew_position(pos: dict, spot: float) -> bool:
    qty    = pos.get("netQty", 0)
    symbol = pos.get("tradingSymbol", "")
    strike = extract_strike(symbol)
    return qty > 0 and symbol.startswith("NIFTY") and strike > 0 and abs(strike - spot) <= ATM_RANGE_PTS


def calc_skew_pnl(positions: list, spot: float) -> float:
    return sum(pos.get("unrealizedProfit", 0.0) for pos in positions if is_skew_position(pos, spot))


def now_ist():
    return datetime.now(IST)

def in_market_hours() -> bool:
    t = now_ist()
    cur = t.hour * 60 + t.minute
    return t.weekday() < 5 and (9 * 60 + 20) <= cur <= (15 * 60 + 20)

def past_market_close() -> bool:
    t = now_ist()
    cur = t.hour * 60 + t.minute
    return t.weekday() < 5 and cur > (15 * 60 + 20)


def run():
    global _token_refresh_at
    log.info("SkewHunter Cloud Tracker starting...")
    send_telegram("SkewHunter Cloud Tracker ON\nTrail : 30% from peak\nMode  : Alert only (Railway cloud)")
    if not _access_token:
        refresh_dhan_token()
    peak_pnl = 0.0
    alert_sent = False
    eod_sent = False
    last_trade_day = None
    while True:
        if time.time() >= _token_refresh_at and _token_refresh_at > 0:
            refresh_dhan_token()
        today = now_ist().date()
        if today != last_trade_day and in_market_hours():
            peak_pnl = 0.0
            alert_sent = False
            eod_sent = False
            last_trade_day = today
            log.info("New trading day reset.")
        if not eod_sent and past_market_close() and last_trade_day == today:
            send_telegram(f"EOD Summary - {today.strftime('%d %b %Y')}\nPeak P&L : Rs {peak_pnl:,.0f}\nAlert    : {'Triggered' if alert_sent else 'Not triggered'}\nTracker  : Running on cloud")
            eod_sent = True
        if not in_market_hours():
            time.sleep(60)
            continue
        positions = get_positions()
        spot      = get_nifty_spot()
        if not positions or spot == 0:
            log.warning("No data - retrying in %ds", CHECK_EVERY)
            time.sleep(CHECK_EVERY)
            continue
        skew_pnl = calc_skew_pnl(positions, spot)
        if skew_pnl > peak_pnl:
            peak_pnl = skew_pnl
            alert_sent = False
            log.info("New Peak: Rs %.0f", peak_pnl)
        if peak_pnl >= MIN_PEAK_INR:
            decline_pct = (peak_pnl - skew_pnl) / peak_pnl * 100
            log.info("P&L Rs %.0f | Peak Rs %.0f | Decline %.1f%%", skew_pnl, peak_pnl, decline_pct)
            if decline_pct >= TRAIL_PCT and not alert_sent:
                send_telegram(f"SKEW TRAIL ALERT\nPeak   : Rs {peak_pnl:,.0f}\nNow    : Rs {skew_pnl:,.0f}\nDrop   : {decline_pct:.1f}%\nAction : Exit SkewHunter manually on Dhan.")
                alert_sent = True
                log.warning("Trail alert sent.")
        else:
            log.info("P&L Rs %.0f | Waiting for peak >= Rs %.0f", skew_pnl, MIN_PEAK_INR)
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    run()
