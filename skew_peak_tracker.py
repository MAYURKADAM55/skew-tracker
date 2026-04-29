"""
SkewHunter Peak Tracker — Cloud Version (Railway.app)
------------------------------------------------------
Reads credentials from environment variables.
Runs 24/7 on Railway. Monitors 9:20 AM - 3:20 PM IST Mon-Fri.
Sends Telegram alerts only — NO auto-exit.

Token management:
  - Set ACCESS_TOKEN in Railway env vars.
  - Script sends a Telegram warning 2 hours before token expires.
  - Renew token daily on Dhan web -> update ACCESS_TOKEN in Railway vars.
"""

import os
import re
import time
import base64
import json
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests

# ── CONFIG FROM ENVIRONMENT VARIABLES ────────────────────────────────────────
CLIENT_ID    = os.environ["CLIENT_ID"]
TELEGRAM_BOT = os.environ["TELEGRAM_BOT"]
TELEGRAM_ID  = os.environ["TELEGRAM_ID"]

# Token is mutable — can be updated via Telegram /token command without redeploy
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
_last_update_id = 0   # Telegram message offset

TRAIL_PCT     = 30    # alert when P&L drops this % from peak
ATM_RANGE_PTS = 300   # strikes within +-300 pts of spot = SkewHunter
MIN_PEAK_INR  = 500   # ignore trail logic until peak crosses this
CHECK_EVERY   = 60    # seconds between polls
IST           = ZoneInfo("Asia/Kolkata")
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── TELEGRAM TOKEN UPDATE VIA /token COMMAND ─────────────────────────────────

def check_telegram_for_new_token() -> None:
    """Poll Telegram for /token <jwt> messages from the authorized user."""
    global ACCESS_TOKEN, _last_update_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT}/getUpdates",
            params={"offset": _last_update_id + 1, "timeout": 0},
            timeout=10,
        )
        updates = r.json().get("result", [])
        for upd in updates:
            _last_update_id = upd["update_id"]
            msg = upd.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").strip()
            # Only accept commands from the authorized chat
            if chat_id != str(TELEGRAM_ID):
                continue
            if text.lower().startswith("/token "):
                new_token = text[7:].strip()
                if len(new_token) > 20:
                    ACCESS_TOKEN = new_token
                    exp = get_token_expiry()
                    exp_str = (
                        datetime.fromtimestamp(exp, IST).strftime("%d %b %Y %I:%M %p IST")
                        if exp else "Unknown"
                    )
                    send_telegram(
                        "Token updated successfully!\n"
                        f"Expires : {exp_str}\n"
                        "Tracker is running with new token."
                    )
                    log.info("Token updated via Telegram. Expires: %s", exp_str)
                else:
                    send_telegram("Invalid token. Send: /token eyJ...")
    except Exception as e:
        log.error("check_telegram_for_new_token failed: %s", e)


# ── TOKEN EXPIRY CHECK ────────────────────────────────────────────────────────

def get_token_expiry() -> int:
    """Parse JWT and return the exp Unix timestamp (0 if unparseable)."""
    try:
        payload_b64 = ACCESS_TOKEN.split(".")[1]  # uses current global ACCESS_TOKEN
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(base64.b64decode(payload_b64))
        return int(payload.get("exp", 0))
    except Exception:
        return 0


def hours_until_expiry() -> float:
    exp = get_token_expiry()
    if exp == 0:
        return 999.0
    return (exp - time.time()) / 3600


# ── API HELPERS ───────────────────────────────────────────────────────────────

def get_headers() -> dict:
    return {
        "access-token": ACCESS_TOKEN,
        "client-id":    CLIENT_ID,
        "Content-Type": "application/json",
    }


def get_positions() -> list:
    try:
        r = requests.get(
            "https://api.dhan.co/v2/positions",
            headers=get_headers(),
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        log.error("get_positions failed: %s", e)
        return []


def get_nifty_spot() -> float:
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        r.raise_for_status()
        return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception as e:
        log.error("get_nifty_spot failed: %s", e)
        return 0.0


def send_telegram(msg: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage",
            data={"chat_id": TELEGRAM_ID, "text": msg},
            timeout=10,
        )
    except Exception as e:
        log.error("Telegram failed: %s", e)


# ── POSITION CLASSIFICATION ───────────────────────────────────────────────────

def extract_strike(symbol: str) -> int:
    m = re.search(r"-(\d{4,5})-(CE|PE)", symbol)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{5})(CE|PE)", symbol)
    return int(m.group(1)) if m else 0


def is_skew_position(pos: dict, spot: float) -> bool:
    qty    = pos.get("netQty", 0)
    symbol = pos.get("tradingSymbol", "")
    strike = extract_strike(symbol)
    return (
        qty > 0
        and symbol.startswith("NIFTY")
        and strike > 0
        and abs(strike - spot) <= ATM_RANGE_PTS
    )


def calc_skew_pnl(positions: list, spot: float) -> float:
    return sum(
        pos.get("unrealizedProfit", 0.0)
        for pos in positions
        if is_skew_position(pos, spot)
    )


# ── TIME HELPERS ──────────────────────────────────────────────────────────────

def now_ist():
    return datetime.now(IST)

def in_market_hours() -> bool:
    t   = now_ist()
    cur = t.hour * 60 + t.minute
    return t.weekday() < 5 and (9 * 60 + 20) <= cur <= (15 * 60 + 20)

def past_market_close() -> bool:
    t   = now_ist()
    cur = t.hour * 60 + t.minute
    return t.weekday() < 5 and cur > (15 * 60 + 20)


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run():
    exp = get_token_expiry()
    exp_ist = (
        datetime.fromtimestamp(exp, IST).strftime("%d %b %Y %I:%M %p IST")
        if exp else "Unknown"
    )

    log.info("SkewHunter Cloud Tracker starting...")
    send_telegram(
        "SkewHunter Cloud Tracker ON\n"
        f"Trail  : {TRAIL_PCT}% from peak\n"
        f"Mode   : Alert only (Railway cloud)\n"
        f"Token expires : {exp_ist}\n"
        "Renew token on Dhan before expiry!"
    )

    peak_pnl         = 0.0
    alert_sent       = False
    eod_sent         = False
    token_warn_sent  = False
    daily_reminder_sent = False
    last_trade_day   = None

    while True:
        now = now_ist()

        # ── Check Telegram for /token command (every loop) ───────────────────
        check_telegram_for_new_token()

        # ── Daily 5 PM token renewal reminder ───────────────────────────────
        if now.hour == 17 and now.minute == 0 and not daily_reminder_sent:
            send_telegram(
                "DAILY REMINDER - 5 PM\n"
                "Renew your Dhan token tonight on laptop!\n\n"
                "Steps (2 min):\n"
                "1. web.dhan.co -> Profile -> DhanHQ Trading APIs\n"
                "2. Generate Access Token -> enter PIN -> Copy\n"
                "3. Railway -> Variables -> update ACCESS_TOKEN -> Deploy\n\n"
                "Do it before sleeping so tracker works tomorrow 9:20 AM"
            )
            daily_reminder_sent = True
            log.info("Daily 5 PM token renewal reminder sent.")

        # Reset daily reminder flag after 5:01 PM
        if now.hour == 17 and now.minute >= 1:
            daily_reminder_sent = False

        # ── Token expiry warning (2 hrs before) ─────────────────────────────
        hrs_left = hours_until_expiry()
        if 0 < hrs_left < 2 and not token_warn_sent:
            send_telegram(
                "TOKEN EXPIRY WARNING\n"
                f"Dhan token expires in {hrs_left:.1f} hours!\n\n"
                "How to renew:\n"
                "1. Open web.dhan.co\n"
                "2. Profile -> DhanHQ Trading APIs\n"
                "3. Click 'Generate Access Token' -> enter PIN\n"
                "4. Copy the new token\n"
                "5. Railway -> service -> Variables -> update ACCESS_TOKEN\n"
                "6. Click Deploy to restart tracker\n\n"
                "Tracker stops working after token expires!"
            )
            token_warn_sent = True
            log.warning("Token expiry warning sent. %.1f hrs remaining.", hrs_left)

        today = now_ist().date()

        # ── Daily reset at market open ────────────────────────────────────────
        if today != last_trade_day and in_market_hours():
            peak_pnl        = 0.0
            alert_sent      = False
            eod_sent        = False
            token_warn_sent = False
            last_trade_day  = today
            log.info("New trading day reset.")

        # ── EOD summary ───────────────────────────────────────────────────────
        if not eod_sent and past_market_close() and last_trade_day == today:
            send_telegram(
                f"EOD Summary - {today.strftime('%d %b %Y')}\n"
                f"Peak P&L : Rs {peak_pnl:,.0f}\n"
                f"Alert    : {'Triggered' if alert_sent else 'Not triggered'}\n"
                f"Tracker  : Running on cloud"
            )
            eod_sent = True

        if not in_market_hours():
            time.sleep(60)
            continue

        # ── Live monitoring ───────────────────────────────────────────────────
        positions = get_positions()
        spot      = get_nifty_spot()

        if not positions or spot == 0:
            log.warning("No data - retrying in %ds", CHECK_EVERY)
            time.sleep(CHECK_EVERY)
            continue

        skew_pnl = calc_skew_pnl(positions, spot)

        if skew_pnl > peak_pnl:
            peak_pnl   = skew_pnl
            alert_sent = False
            log.info("New Peak: Rs %.0f", peak_pnl)

        if peak_pnl >= MIN_PEAK_INR:
            decline_pct = (peak_pnl - skew_pnl) / peak_pnl * 100
            log.info("P&L Rs %.0f | Peak Rs %.0f | Decline %.1f%%",
                     skew_pnl, peak_pnl, decline_pct)

            if decline_pct >= TRAIL_PCT and not alert_sent:
                send_telegram(
                    f"SKEW TRAIL ALERT\n"
                    f"Peak   : Rs {peak_pnl:,.0f}\n"
                    f"Now    : Rs {skew_pnl:,.0f}\n"
                    f"Drop   : {decline_pct:.1f}%\n"
                    f"Action : Exit SkewHunter manually on Dhan."
                )
                alert_sent = True
                log.warning("Trail alert sent.")
        else:
            log.info("P&L Rs %.0f | Waiting for peak >= Rs %.0f",
                     skew_pnl, MIN_PEAK_INR)

        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    run()
