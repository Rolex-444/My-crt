import json, logging, os, threading, time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify
from PIL import Image, ImageDraw
from io import BytesIO

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("crt-bot")
app = Flask(__name__)

BASE = "https://api.india.delta.exchange"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSD,ETHUSD,XRPUSD").split(",") if s.strip()]
POLL = max(30, int(os.getenv("POLL_SECONDS", "60")))
STATE_PATH = Path(os.getenv("STATE_FILE", "/tmp/crt_state.json"))
LOCK = threading.Lock()
STATE = {"seen": {}, "setups": {}, "last_cycle": None, "errors": []}
SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": "crt-alert-bot/1.0"})


def load_state():
    global STATE
    try:
        if STATE_PATH.exists():
            STATE.update(json.loads(STATE_PATH.read_text()))
    except Exception as exc:
        log.warning("Could not load state: %s", exc)


def save_state():
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(STATE))
        tmp.replace(STATE_PATH)
    except Exception as exc:
        log.warning("Could not save state: %s", exc)


def telegram(message):
    if not TOKEN or not CHAT_ID:
        log.info("Telegram not configured; alert: %s", message.replace("\n", " | "))
        return False
    try:
        r = SESSION.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(str(data))
        return True
    except Exception as exc:
        log.error("Telegram send failed: %s", exc)
        return False



def chart_image(data, title, candle_marks=None, levels=None, zones=None):
    """Render the latest 30 completed candles as an annotated PNG."""
    data = data[-30:]
    if not data:
        return None
    width, height = 1100, 650
    left, right, top, bottom = 90, 1030, 90, 555
    image = Image.new("RGB", (width, height), "#101820")
    draw = ImageDraw.Draw(image)
    draw.text((left, 25), title, fill="#FFFFFF")
    draw.text((left, 55), "Delta Exchange | Latest 30 completed candles", fill="#AAB7C4")

    lo = min(c["low"] for c in data)
    hi = max(c["high"] for c in data)
    pad = max((hi - lo) * 0.12, abs(hi) * 0.0005, 0.000001)
    lo, hi = lo - pad, hi + pad

    def y(price):
        return int(bottom - (price - lo) / (hi - lo) * (bottom - top))

    step = (right - left) / len(data)
    body_w = max(5, int(step * 0.55))

    for zone in zones or []:
        zlo, zhi = sorted((float(zone["low"]), float(zone["high"])))
        if zhi < lo or zlo > hi:
            continue
        y1, y2 = y(min(zhi, hi)), y(max(zlo, lo))
        draw.rectangle((left, y1, right, y2), fill="#263746", outline="#D6A84F")
        draw.text((right - 190, max(top, y1 + 2)), zone["label"], fill="#F4C76B")

    marks = {int(m["time"]): m["label"] for m in (candle_marks or [])}
    for i, candle in enumerate(data):
        x = int(left + (i + 0.5) * step)
        color = "#20C997" if candle["close"] >= candle["open"] else "#FF647C"
        draw.line((x, y(candle["high"]), x, y(candle["low"])), fill=color, width=2)
        y1, y2 = sorted((y(candle["open"]), y(candle["close"])))
        if y2 - y1 < 3:
            y2 = y1 + 3
        draw.rectangle((x - body_w // 2, y1, x + body_w // 2, y2), fill=color)
        if candle["time"] in marks:
            draw.text((max(left, x - 35), max(top, y(candle["high"]) - 25)),
                      marks[candle["time"]], fill="#FFE082")

    for level in levels or []:
        price = float(level["price"])
        if lo <= price <= hi:
            yy = y(price)
            color = level.get("color", "#65B8FF")
            draw.line((left, yy, right, yy), fill=color, width=2)
            draw.text((right - 190, max(top, yy - 18)),
                      f'{level["label"]}: {price:g}', fill=color)

    draw.rectangle((left, top, right, bottom), outline="#52616B", width=2)
    draw.text((left, 585), "Older candles  <--- time --->  Newer candles", fill="#AAB7C4")
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output


def telegram_chart(data, title, candle_marks=None, levels=None, zones=None, caption=None):
    if not TOKEN or not CHAT_ID:
        return False
    try:
        photo = chart_image(data, title, candle_marks, levels, zones)
        if photo is None:
            return False
        response = SESSION.post(
            f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
            data={"chat_id": CHAT_ID, "caption": (caption or title)[:900]},
            files={"photo": ("crt_signal.png", photo, "image/png")},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload))
        return True
    except Exception as exc:
        log.error("Telegram chart send failed: %s", exc)
        return False


def telegram_command_reply(chat_id, message):
    if not TOKEN:
        return
    try:
        r = SESSION.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as exc:
        log.error("Telegram command reply failed: %s", exc)


def handle_command(message):
    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    # Only accept commands from the configured destination chat.
    if not chat_id or chat_id != CHAT_ID:
        return
    text_value = (message.get("text") or "").strip()
    if not text_value.startswith("/"):
        return
    command = text_value.split()[0].split("@")[0].lower()

    if command == "/start":
        reply = ("Welcome to your CRT alert bot!\n\n"
                 "I monitor Delta Exchange futures and send CRT / Model #1 alerts.\n"
                 "Alert-only mode: no orders are placed.\n\n"
                 "Use /status, /signals, or /help.")
    elif command == "/help":
        reply = ("/start — Welcome message\n"
                 "/status — Bot health and monitored symbols\n"
                 "/signals — Current setup stages\n"
                 "/help — Show this help")
    elif command == "/status":
        last_cycle = STATE.get("last_cycle") or "Not completed yet"
        errors = STATE.get("errors") or []
        reply = ("CRT bot status: RUNNING\n"
                 f"Symbols: {', '.join(SYMBOLS)}\n"
                 f"Scan interval: {POLL} seconds\n"
                 f"Last scan (UTC): {last_cycle}\n"
                 f"Last scan errors: {'; '.join(errors) if errors else 'None'}\n"
                 "Mode: alerts only; no automatic trading.")
    elif command == "/signals":
        setups = STATE.get("setups", {})
        active = []
        for symbol, item in setups.items():
            stage = item.get("stage", "unknown")
            if stage != "completed":
                active.append(f"{symbol}: {item.get('side', '?')} — {stage}")
        reply = "Current CRT setups:\n" + ("\n".join(active) if active else "No active setups currently.")
    else:
        return
    telegram_command_reply(chat_id, reply)


def command_listener():
    if not TOKEN or not CHAT_ID:
        log.warning("Telegram command listener disabled: token or chat ID missing")
        return
    offset = None
    while True:
        try:
            params = {"timeout": 10, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            r = SESSION.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", params=params, timeout=15)
            r.raise_for_status()
            payload = r.json()
            if not payload.get("ok"):
                raise RuntimeError(str(payload))
            for update in payload.get("result", []):
                offset = update["update_id"] + 1
                if "message" in update:
                    handle_command(update["message"])
        except Exception as exc:
            log.error("Telegram command polling failed: %s", exc)
            time.sleep(5)


def candles(symbol, resolution, count=240):
    seconds = 4 * 3600 if resolution == "4h" else 15 * 60
    end = int(time.time())
    start = end - seconds * count
    r = SESSION.get(
        f"{BASE}/v2/history/candles",
        params={"resolution": resolution, "symbol": symbol, "start": start, "end": end},
        timeout=20,
    )
    r.raise_for_status()
    payload = r.json()
    if not payload.get("success"):
        raise RuntimeError(f"Delta candle API error for {symbol} {resolution}: {payload}")
    rows = payload.get("result", [])
    parsed = []
    for x in rows:
        try:
            parsed.append({
                "time": int(x["time"]), "open": float(x["open"]),
                "high": float(x["high"]), "low": float(x["low"]),
                "close": float(x["close"]), "volume": float(x.get("volume", 0) or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    parsed.sort(key=lambda c: c["time"])
    # Never use the currently forming candle.
    return [c for c in parsed if c["time"] + seconds <= end]


def body(c):
    return abs(c["close"] - c["open"])


def upper_wick(c):
    return c["high"] - max(c["open"], c["close"])


def lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]


def overlap(a_low, a_high, b_low, b_high):
    return max(a_low, b_low) <= min(a_high, b_high)


def pivots(data, left=2, right=2):
    out = []
    for i in range(left, len(data) - right):
        window = data[i-left:i+right+1]
        c = data[i]
        if c["high"] > max(x["high"] for j, x in enumerate(window) if j != left):
            out.append(("high", i, c["high"]))
        if c["low"] < min(x["low"] for j, x in enumerate(window) if j != left):
            out.append(("low", i, c["low"]))
    return out


def key_zones(data, old_candle=None):
    zones = []
    # Old High/Low are taken from the 4H candle immediately before the CRT candle.
    if old_candle is not None:
        zones += [("Old High", old_candle["high"], old_candle["high"]), ("Old Low", old_candle["low"], old_candle["low"])]

    # Standard three-candle FVGs and their inverse after a later close through the zone.
    fvg_list = []
    for i in range(2, len(data)):
        a, b, c = data[i-2], data[i-1], data[i]
        if c["low"] > a["high"]:
            fvg_list.append(("Bullish FVG", a["high"], c["low"], i))
        if c["high"] < a["low"]:
            fvg_list.append(("Bearish FVG", c["high"], a["low"], i))
    for name, lo, hi, made_at in fvg_list:
        zones.append((name, lo, hi))
        for later in data[made_at+1:]:
            if name == "Bullish FVG" and later["close"] < lo:
                zones.append(("Bearish IFVG", lo, hi))
                break
            if name == "Bearish FVG" and later["close"] > hi:
                zones.append(("Bullish IFVG", lo, hi))
                break

    # Deterministic OB approximation: last opposite candle before displacement >=1.5x
    # median body of the previous 20 candles.
    for i in range(20, len(data)):
        med = sorted(body(x) for x in data[i-20:i])[10]
        c = data[i]
        if med <= 0 or body(c) < 1.5 * med:
            continue
        prior = data[i-1]
        if c["close"] > c["open"] and prior["close"] < prior["open"]:
            zones.append(("Bullish OB", prior["low"], prior["high"]))
        elif c["close"] < c["open"] and prior["close"] > prior["open"]:
            zones.append(("Bearish OB", prior["low"], prior["high"]))

    # Rejection block approximation: pivot swept, then candle closes back through pivot.
    for kind, idx, level in pivots(data):
        for c in data[idx+1:]:
            if kind == "high" and c["high"] > level and c["close"] < level:
                zones.append(("Bearish Rejection Block", level, c["high"]))
                break
            if kind == "low" and c["low"] < level and c["close"] > level:
                zones.append(("Bullish Rejection Block", c["low"], level))
                break
    return zones


def key_level_match(crt, second, zones):
    hits = []
    for name, lo, hi in zones:
        if overlap(crt["low"], crt["high"], lo, hi) or overlap(second["low"], second["high"], lo, hi):
            hits.append(name)
    return sorted(set(hits))


def find_latest_crt_setup(h4):
    # Candidate CRT and its immediately following completed candle.
    for i in range(len(h4)-2, max(0, len(h4)-35), -1):
        crt, second = h4[i], h4[i+1]
        if body(crt) <= upper_wick(crt) + lower_wick(crt):
            continue
        sell_sweep = second["high"] > crt["high"] and second["close"] < crt["high"] and second["close"] >= crt["low"]
        buy_sweep = second["low"] < crt["low"] and second["close"] > crt["low"] and second["close"] <= crt["high"]
        if not (sell_sweep or buy_sweep):
            continue
        zones = key_zones(h4[:i+2], h4[i-1] if i > 0 else None)
        hits = key_level_match(crt, second, zones)
        if not hits:
            continue
        matched_zones = [{"label": name, "low": lo, "high": hi} for name, lo, hi in zones
                         if name in hits and (overlap(crt["low"], crt["high"], lo, hi)
                                              or overlap(second["low"], second["high"], lo, hi))]
        return {
            "side": "SELL" if sell_sweep else "BUY", "crt": crt, "second": second,
            "mid": (crt["high"] + crt["low"]) / 2,
            "entry_target": crt["low"] if sell_sweep else crt["high"],
            "key_hits": hits, "matched_zones": matched_zones, "id": f"{crt['time']}:{'S' if sell_sweep else 'B'}",
        }
    return None


def tbs_events(data):
    confirmed = pivots(data)
    events = []
    # For each candle, only pivots confirmed before that candle are eligible.
    for i, c in enumerate(data):
        prior = [(k, idx, lvl) for k, idx, lvl in confirmed if idx + 2 < i]
        highs = [x for x in prior if x[0] == "high"]
        lows = [x for x in prior if x[0] == "low"]
        if highs:
            level = highs[-1][2]
            # Body must trade above the swing high, then candle closes back below it.
            if max(c["open"], c["close"]) > level and min(c["open"], c["close"]) <= level and c["close"] < level:
                events.append((i, "SELL", level, c))
        if lows:
            level = lows[-1][2]
            if min(c["open"], c["close"]) < level and max(c["open"], c["close"]) >= level and c["close"] > level:
                events.append((i, "BUY", level, c))
    return events


def smt_status(symbol, side, h4_map):
    other = {"BTCUSD": "ETHUSD", "ETHUSD": "BTCUSD", "XRPUSD": "BTCUSD"}.get(symbol)
    if not other or symbol not in h4_map or other not in h4_map:
        return "SMT unavailable — check manually"
    a, b = h4_map[symbol], h4_map[other]
    if len(a) < 3 or len(b) < 3:
        return "SMT unavailable — check manually"
    # Informational divergence proxy on latest two completed 4H candles.
    if side == "SELL":
        a_sweep = a[-1]["high"] > a[-2]["high"]
        b_sweep = b[-1]["high"] <= b[-2]["high"]
    else:
        a_sweep = a[-1]["low"] < a[-2]["low"]
        b_sweep = b[-1]["low"] >= b[-2]["low"]
    return "SMT divergence present (proxy)" if a_sweep and b_sweep else "SMT not confirmed — check manually"


def price_crossed(old_price, new_price, target, side):
    if side == "SELL":
        return old_price > target >= new_price
    return old_price < target <= new_price


def largest_body_crt_candidate(h4):
    """
    Select only the newest completed 4H candle when its body is the largest
    among the latest 30 completed candles and satisfies the CRT body rule.
    This prevents repeatedly discovering an old historical candle.
    """
    if len(h4) < 30:
        return None
    recent = h4[-30:]
    candidate = recent[-1]
    largest_body = max(body(c) for c in recent)
    if body(candidate) < largest_body:
        return None
    if body(candidate) <= upper_wick(candidate) + lower_wick(candidate):
        return None
    return candidate


def scan_symbol(symbol, h4, m15, h4_map):
    state = STATE["setups"].get(symbol)

    # Stage 1: identify a new, exceptionally large CRT candle.
    candidate = largest_body_crt_candidate(h4)
    can_start_new = (not state) or state.get("stage") in ("invalidated", "completed")
    if candidate and can_start_new:
        candidate_id = f"{candidate['time']}:PENDING"
        if not state or state.get("crt_time") != candidate["time"]:
            old_candle = h4[-2] if len(h4) >= 2 else None
            zones = key_zones(h4[:-1], old_candle)
            crt_hits = sorted(set(
                name for name, lo, hi in zones
                if overlap(candidate["low"], candidate["high"], lo, hi)
            ))
            crt_zones = [{"label": name, "low": lo, "high": hi}
                         for name, lo, hi in zones
                         if name in crt_hits and overlap(candidate["low"], candidate["high"], lo, hi)]
            state = {
                "id": candidate_id, "stage": "waiting_second",
                "crt_time": candidate["time"], "crt_high": candidate["high"],
                "crt_low": candidate["low"], "crt_open": candidate["open"],
                "crt_close": candidate["close"], "crt_body": body(candidate),
                "mid": (candidate["high"] + candidate["low"]) / 2,
                "key_hits": crt_hits, "key_zones": crt_zones,
                "crt_key_alerted": bool(crt_hits), "last_price": None
            }
            STATE["setups"][symbol] = state
            telegram(
                f"🟡 BIG CRT CANDLE FOUND — {symbol}\n"
                f"Time (UTC): {datetime.fromtimestamp(candidate['time'], timezone.utc).strftime('%Y-%m-%d %H:%M')}\n"
                f"Body: {body(candidate):g} (largest among latest 30 completed 4H candles)\n"
                f"CRT High: {candidate['high']:g}\nCRT Low: {candidate['low']:g}\n"
                f"CRT 50%: {state['mid']:g}\n"
                f"Key level touched by CRT: {', '.join(crt_hits) if crt_hits else 'Not yet'}\n"
                "Waiting for the next completed 4H candle to sweep and close back inside."
            )
            telegram_chart(
                h4, f"{symbol} | BIG 4H CRT CANDLE FOUND",
                candle_marks=[{"time": candidate["time"], "label": "BIG CRT"}],
                levels=[{"label": "CRT High", "price": candidate["high"]},
                        {"label": "CRT 50%", "price": state["mid"]},
                        {"label": "CRT Low", "price": candidate["low"]}],
                zones=crt_zones,
                caption=f"{symbol}: largest-body CRT candidate in latest 30 completed 4H candles"
            )
            if crt_hits:
                telegram(
                    f"🟠 KEY LEVEL TOUCHED BY CRT CANDLE — {symbol}\n"
                    f"Levels: {', '.join(crt_hits)}\n"
                    "The CRT candle itself overlapped the marked key-level zone."
                )
                telegram_chart(
                    h4, f"{symbol} | CRT CANDLE KEY-LEVEL TOUCH",
                    candle_marks=[{"time": candidate["time"], "label": "CRT + KEY TOUCH"}],
                    levels=[{"label": "CRT 50%", "price": state["mid"]}],
                    zones=crt_zones,
                    caption=f"{symbol}: key level touched by the CRT candle"
                )

    state = STATE["setups"].get(symbol)
    if not state:
        return

    # Stage 2: evaluate only the immediate next completed 4H candle.
    if state.get("stage") == "waiting_second":
        crt_index = next((i for i, c in enumerate(h4) if c["time"] == state["crt_time"]), None)
        if crt_index is not None and len(h4) > crt_index + 1:
            crt = h4[crt_index]
            second = h4[crt_index + 1]
            sell_sweep = (second["high"] > crt["high"] and
                          second["close"] < crt["high"] and
                          second["close"] >= crt["low"])
            buy_sweep = (second["low"] < crt["low"] and
                         second["close"] > crt["low"] and
                         second["close"] <= crt["high"])
            if not (sell_sweep or buy_sweep):
                state["stage"] = "invalidated"
                telegram(
                    f"⚪ CRT CANDIDATE INVALIDATED — {symbol}\n"
                    "The immediate next 4H candle did not sweep the CRT boundary and close back inside."
                )
                return

            side = "SELL" if sell_sweep else "BUY"
            state["side"] = side
            state["second_time"] = second["time"]
            state["target"] = crt["low"] if sell_sweep else crt["high"]
            state["entry_target"] = state["target"]
            zones = key_zones(h4[:crt_index + 2], h4[crt_index - 1] if crt_index > 0 else None)
            second_hits = sorted(set(
                name for name, lo, hi in zones
                if overlap(second["low"], second["high"], lo, hi)
            ))
            second_zones = [{"label": name, "low": lo, "high": hi}
                            for name, lo, hi in zones
                            if name in second_hits and overlap(second["low"], second["high"], lo, hi)]
            state["key_hits"] = sorted(set(state.get("key_hits", []) + second_hits))
            state["key_zones"] = state.get("key_zones", []) + [
                z for z in second_zones
                if not any(old["label"] == z["label"] and old["low"] == z["low"] and old["high"] == z["high"]
                           for old in state.get("key_zones", []))
            ]
            state["stage"] = "waiting_tbs" if state["key_hits"] else "invalidated"
            telegram(
                f"🔵 SECOND CANDLE SWEEP CONFIRMED — {symbol} {side}\n"
                f"CRT High: {crt['high']:g} | CRT Low: {crt['low']:g}\n"
                f"Second candle High: {second['high']:g} | Low: {second['low']:g}\n"
                f"Second candle Close: {second['close']:g} (inside CRT range)\n"
                f"Key level touched by second candle: {', '.join(second_hits) if second_hits else 'No'}\n"
                f"Key level touched by CRT candle: {', '.join(state['key_hits']) if state['key_hits'] else 'No'}"
            )
            telegram_chart(
                h4, f"{symbol} | 4H SECOND-CANDLE SWEEP CONFIRMED",
                candle_marks=[{"time": crt["time"], "label": "BIG CRT"},
                              {"time": second["time"], "label": "SWEEP + CLOSE IN"}],
                levels=[{"label": "CRT High", "price": crt["high"]},
                        {"label": "CRT 50%", "price": state["mid"]},
                        {"label": "CRT Low", "price": crt["low"]}],
                zones=state.get("key_zones", []),
                caption=f"{symbol} {side}: second 4H candle swept CRT and closed inside"
            )
            if second_hits:
                telegram(
                    f"🟠 KEY LEVEL TOUCHED BY SECOND CANDLE — {symbol} {side}\n"
                    f"Levels: {', '.join(second_hits)}\n"
                    "Second candle overlapped the marked key-level zone."
                )
                telegram_chart(
                    h4, f"{symbol} | SECOND CANDLE KEY-LEVEL TOUCH",
                    candle_marks=[{"time": crt["time"], "label": "BIG CRT"},
                                  {"time": second["time"], "label": "SWEEP + KEY TOUCH"}],
                    levels=[{"label": "CRT 50%", "price": state["mid"]}],
                    zones=state.get("key_zones", []),
                    caption=f"{symbol}: second candle touched key level(s): {', '.join(second_hits)}"
                )

    # Continue to 15M Model #1 only when sweep and key-level conditions are met.
    if state.get("stage") in ("invalidated", "completed"):
        return

    if state.get("stage") == "waiting_tbs":
        for idx, side, level, candle in tbs_events(m15):
            if side == state["side"] and candle["time"] > state["second_time"]:
                state.update({"stage": "waiting_model1_break", "model1_high": candle["high"],
                              "model1_low": candle["low"], "tbs_time": candle["time"]})
                telegram(f"🔵 15M TURTLE BODY SOUP — {symbol} {side}\nSwing level: {level}\nModel #1 High: {candle['high']}\nModel #1 Low: {candle['low']}\nWaiting for next 15M candle CLOSE beyond Model #1.")
                telegram_chart(m15, f"{symbol} | 15M TURTLE BODY SOUP",
                               candle_marks=[{"time": candle["time"], "label": "TBS / MODEL #1"}],
                               levels=[{"label": "Swing level", "price": level},
                                       {"label": "Model #1 High", "price": candle["high"]},
                                       {"label": "Model #1 Low", "price": candle["low"]}],
                               caption=f"{symbol} 15M TBS: Model #1 candle and swing level")
                break
    if state["stage"] == "waiting_model1_break" and m15:
        last = m15[-1]
        if last["time"] > state["tbs_time"]:
            confirmed = last["close"] < state["model1_low"] if state["side"] == "SELL" else last["close"] > state["model1_high"]
            if confirmed:
                state["stage"] = "entry_alerted"
                smt = smt_status(symbol, state["side"], h4_map)
                telegram(f"🚨 MODEL #1 ENTRY CONFIRMED — {symbol} {state['side']}\n15M close: {last['close']}\nModel #1 range: {state['model1_low']} – {state['model1_high']}\nSMT: {smt}\nAlert only: place/manage trades manually.")
                telegram_chart(m15, f"{symbol} | 15M MODEL #1 ENTRY",
                               candle_marks=[{"time": state["tbs_time"], "label": "MODEL #1"},
                                             {"time": last["time"], "label": "ENTRY CLOSE"}],
                               levels=[{"label": "Model #1 High", "price": state["model1_high"]},
                                       {"label": "Model #1 Low", "price": state["model1_low"]},
                                       {"label": "CRT 50%", "price": state["mid"]},
                                       {"label": "CRT target", "price": state["target"]}],
                               caption=f"{symbol} {state['side']} 15M entry confirmation")
    if m15 and state.get("stage") in ("entry_alerted", "waiting_model1_break"):
        price = m15[-1]["close"]
        old = state.get("last_price")
        if old is not None and state.get("stage") == "entry_alerted":
            mid = state["mid"]
            if not state.get("partial_alerted") and price_crossed(old, price, mid, state["side"]):
                state["partial_alerted"] = True
                telegram(f"🟣 CRT 50% LEVEL — {symbol} {state['side']}\nPrice reached CRT midpoint {mid}. Reminder: consider partials and move SL to breakeven manually.")
            target = state["target"]
            if not state.get("exit_alerted") and price_crossed(old, price, target, state["side"]):
                state["exit_alerted"] = True
                state["stage"] = "completed"
                telegram(f"🏁 CRT FINAL TARGET — {symbol} {state['side']}\nPrice reached opposite CRT boundary {target}. Reminder: manage/close manually.")
        state["last_price"] = price


def cycle():
    h4_map = {}
    m15_map = {}
    for symbol in SYMBOLS:
        h4_map[symbol] = candles(symbol, "4h", 240)
        m15_map[symbol] = candles(symbol, "15m", 240)
    for symbol in SYMBOLS:
        scan_symbol(symbol, h4_map[symbol], m15_map[symbol], h4_map)
    STATE["last_cycle"] = datetime.now(timezone.utc).isoformat()
    STATE["errors"] = []
    save_state()


def worker():
    load_state()
    if TOKEN and CHAT_ID:
        telegram("🤖 CRT alert bot started. Alert-only mode; no orders will be placed.")
    while True:
        try:
            with LOCK:
                cycle()
        except Exception as exc:
            log.exception("Scan cycle failed")
            STATE["errors"] = [str(exc)[:300]]
            save_state()
        time.sleep(POLL)


@app.get("/")
def home():
    return jsonify({"service": "CRT Telegram Alert Bot", "mode": "alert-only", "symbols": SYMBOLS,
                    "health": "/health"})


@app.get("/health")
def health():
    return jsonify({"ok": True, "telegram_configured": bool(TOKEN and CHAT_ID),
                    "last_cycle": STATE.get("last_cycle"), "symbols": SYMBOLS,
                    "last_errors": STATE.get("errors", [])})


def start_worker():
    if os.getenv("DISABLE_WORKER", "0") != "1":
        t = threading.Thread(target=worker, daemon=True, name="market-scanner")
        t.start()
        commands = threading.Thread(target=command_listener, daemon=True, name="telegram-commands")
        commands.start()


start_worker()
