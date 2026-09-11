import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv
from eth_account import Account

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants


# ============================================================
# CONFIGURAZIONE
# ============================================================

load_dotenv()

PRIVATE_KEY = os.getenv("HYPERLIQUID_PRIVATE_KEY")
ACCOUNT_ADDRESS = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS")

# SPOT ONLY
SPOT_COIN = "BTC/USDC"

BUY_USD = float(os.getenv("BUY_USD", "10"))
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "200"))
MAX_WEEKLY_BUYS = int(os.getenv("MAX_WEEKLY_BUYS", "10"))

DIP_PERCENT = float(os.getenv("DIP_PERCENT", "2"))
TAKE_PROFIT_PERCENT = float(os.getenv("TAKE_PROFIT_PERCENT", "4"))

MAX_SLIPPAGE = float(os.getenv("MAX_SLIPPAGE", "0.01"))

MAX_LOTS_TO_SELL_PER_RUN = int(os.getenv("MAX_LOTS_TO_SELL_PER_RUN", "1"))

STATE_DIR = os.getenv("STATE_DIR", "/data")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

STARTUP_DELAY = int(os.getenv("STARTUP_DELAY", "5"))

POST_ORDER_DELAY = int(os.getenv("POST_ORDER_DELAY", "3"))

FILL_CHECK_ATTEMPTS = int(os.getenv("FILL_CHECK_ATTEMPTS", "5"))

FILL_CHECK_DELAY = float(os.getenv("FILL_CHECK_DELAY", "1"))

POSITION_TOLERANCE = float(os.getenv("POSITION_TOLERANCE", "0.00000001"))


# ============================================================
# VALIDAZIONE
# ============================================================

if not PRIVATE_KEY:
    raise RuntimeError("HYPERLIQUID_PRIVATE_KEY mancante")

if not ACCOUNT_ADDRESS:
    raise RuntimeError("HYPERLIQUID_ACCOUNT_ADDRESS mancante")


# ============================================================
# CONNESSIONE
# ============================================================

wallet = Account.from_key(PRIVATE_KEY)

info = Info(constants.MAINNET_API_URL, skip_ws=True)

exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=ACCOUNT_ADDRESS)


# ============================================================
# LOG
# ============================================================

def log(message):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "version": 5,

        "performance_start_ms": int(datetime.now(timezone.utc).timestamp() * 1000),

        "last_processed_candle": None,

        "week_id": None,
        "weekly_buys": 0,

        "next_lot_id": 1,

        "open_lots": [],
        "sell_trades": [],

        "last_buy": None,
        "last_sell": None
    }


def load_state():
    os.makedirs(STATE_DIR, exist_ok=True)

    if not os.path.exists(STATE_FILE):
        return default_state()

    with open(STATE_FILE, "r") as f:
        state = json.load(f)

    base = default_state()
    base.update(state)

    return base


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)

    tmp_file = STATE_FILE + ".tmp"

    with open(tmp_file, "w") as f:
        json.dump(state, f, indent=2)

    os.replace(tmp_file, STATE_FILE)


# ============================================================
# SETTIMANA
# ============================================================

def current_week_id():
    now = datetime.now(timezone.utc)
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def refresh_week(state):
    week_id = current_week_id()

    if state.get("week_id") != week_id:
        state["week_id"] = week_id
        state["weekly_buys"] = 0
        save_state(state)


# ============================================================
# SPOT METADATA
# ============================================================

def get_spot_decimals():
    meta = info.spot_meta()

    for token in meta["tokens"]:
        if token["name"] == "BTC":
            return int(token["szDecimals"])

    raise RuntimeError("BTC non trovato nei metadata Spot")


def round_btc(size):
    decimals = get_spot_decimals()
    return round(float(size), decimals)


# ============================================================
# SALDI SPOT
# ============================================================

def get_spot_balances():
    data = info.spot_user_state(ACCOUNT_ADDRESS)

    usdc_total = 0.0
    usdc_hold = 0.0

    btc_total = 0.0
    btc_hold = 0.0

    for balance in data.get("balances", []):
        coin = balance.get("coin")

        total = float(balance.get("total", 0) or 0)
        hold = float(balance.get("hold", 0) or 0)

        if coin == "USDC":
            usdc_total = total
            usdc_hold = hold
        elif coin == "BTC":
            btc_total = total
            btc_hold = hold

    return {
        "usdc_total": usdc_total,
        "usdc_available": max(0.0, usdc_total - usdc_hold),

        "btc_total": btc_total,
        "btc_available": max(0.0, btc_total - btc_hold)
    }


# ============================================================
# PREZZO SPOT
# ============================================================

def get_spot_price():
    data = info.spot_meta_and_asset_ctxs()

    meta = data[0]
    contexts = data[1]

    for i, market in enumerate(meta["universe"]):
        base_idx = market["tokens"][0]
        quote_idx = market["tokens"][1]

        base = meta["tokens"][base_idx]["name"]
        quote = meta["tokens"][quote_idx]["name"]

        if base == "BTC" and quote == "USDC":
            mid = contexts[i].get("midPx")

            if mid is None:
                raise RuntimeError("Prezzo BTC/USDC non disponibile")

            return float(mid)

    raise RuntimeError("Mercato BTC/USDC Spot non trovato")


# ============================================================
# ORDINE SPOT AGGRESSIVO IOC
# ============================================================

def get_spot_execution_price(is_buy, slippage):
    book = info.l2_snapshot(SPOT_COIN)

    levels = book.get("levels", [])

    if len(levels) < 2:
        raise RuntimeError("Orderbook BTC/USDC non disponibile")

    bids = levels[0]
    asks = levels[1]

    if is_buy:
        if not asks:
            raise RuntimeError("Ask BTC/USDC non disponibile")

        best_ask = float(asks[0]["px"])

        return best_ask * (1 + slippage)
    else:
        if not bids:
            raise RuntimeError("Bid BTC/USDC non disponibile")

        best_bid = float(bids[0]["px"])

        return best_bid * (1 - slippage)


def spot_market_order(is_buy, size):
    price = get_spot_execution_price(is_buy, MAX_SLIPPAGE)

    # prezzo con precisione sufficiente
    price = float(f"{price:.8f}")

    log(
        f"ORDINE SPOT | "
        f"{'BUY' if is_buy else 'SELL'} | "
        f"{size:.8f} BTC | "
        f"limite aggressivo ${price:.2f}"
    )

    result = exchange.order(
        SPOT_COIN,
        is_buy,
        size,
        price,
        {
            "limit": {
                "tif": "Ioc"
            }
        }
    )

    log(f"ORDINE SPOT RISPOSTA | {result}")

    return result


# ============================================================
# FILLS SPOT
# ============================================================

def get_spot_fills_since(start_ms):
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    fills = info.user_fills_by_time(ACCOUNT_ADDRESS, start_ms, end_ms)

    result = []

    for fill in fills:
        coin = fill.get("coin")

        if coin in (SPOT_COIN, "BTC/USDC"):
            result.append(fill)

    return result


def calculate_fill(fills, is_buy):
    selected = []

    for fill in fills:
        side = fill.get("side")

        if is_buy and side == "B":
            selected.append(fill)
        elif not is_buy and side == "A":
            selected.append(fill)

    if not selected:
        return None

    total_size = 0.0
    total_notional = 0.0

    for fill in selected:
        size = abs(float(fill.get("sz", 0) or 0))
        price = float(fill.get("px", 0) or 0)

        total_size += size
        total_notional += size * price

    if total_size <= 0:
        return None

    return {
        "size": total_size,
        "price": total_notional / total_size,
        "notional": total_notional
    }


# ============================================================
# FEE STIMATA
# ============================================================

def estimate_fee(notional):
    try:
        fees = info.user_fees(ACCOUNT_ADDRESS)

        # per ordine IOC aggressivo
        rate = float(fees.get("userCrossRate", 0) or 0)

        return notional * rate
    except Exception:
        return 0.0


# ============================================================
# CONTROLLO POSIZIONE SPOT
# ============================================================

def verify_spot_position(state):
    balances = get_spot_balances()

    real_btc = balances["btc_total"]

    local_btc = sum(float(lot["remaining_size"]) for lot in state["open_lots"])

    difference = abs(real_btc - local_btc)

    log(
        f"CONTROLLO SPOT | "
        f"BTC reale {real_btc:.8f} | "
        f"lotti {local_btc:.8f} | "
        f"diff {difference:.8f}"
    )

    if difference > POSITION_TOLERANCE:
        raise RuntimeError(
            "INCOERENZA BTC SPOT: "
            f"saldo reale={real_btc:.8f}, "
            f"lotti locali={local_btc:.8f}. "
            "BOT BLOCCATO."
        )

    return balances


# ============================================================
# CANDELE 4H SPOT
# ============================================================

def get_closed_4h_candles():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    start_ms = now_ms - (4 * 60 * 60 * 1000 * 10)

    candles = info.candles_snapshot(SPOT_COIN, "4h", start_ms, now_ms)

    closed = []

    for candle in candles:
        if int(candle["T"]) <= now_ms:
            closed.append(candle)

    closed.sort(key=lambda x: int(x["T"]))

    return closed


# ============================================================
# BUY SPOT
# ============================================================

def place_buy(state):
    balances = get_spot_balances()

    current_btc = balances["btc_total"]

    current_price = get_spot_price()

    current_position_value = current_btc * current_price

    if current_position_value + BUY_USD > MAX_POSITION_USD:
        log(
            f"BUY BLOCCATO | "
            f"BTC attuale ${current_position_value:.2f} | "
            f"BUY ${BUY_USD:.2f} | "
            f"MAX ${MAX_POSITION_USD:.2f}"
        )
        return False

    if state["weekly_buys"] >= MAX_WEEKLY_BUYS:
        log(f"BUY BLOCCATO | limite settimanale {MAX_WEEKLY_BUYS}")
        return False

    if balances["usdc_available"] < BUY_USD:
        log(
            f"BUY BLOCCATO | "
            f"USDC disponibili ${balances['usdc_available']:.4f} | "
            f"necessari ${BUY_USD:.2f}"
        )
        return False

    # size indicativa
    buy_size = BUY_USD / current_price
    buy_size = round_btc(buy_size)

    if buy_size <= 0:
        log("BUY BLOCCATO | size BTC non valida")
        return False

    order_start_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    result = spot_market_order(True, buy_size)

    if result.get("status") != "ok":
        raise RuntimeError(f"BUY SPOT rifiutato: {result}")

    time.sleep(POST_ORDER_DELAY)

    fill = None

    for _ in range(FILL_CHECK_ATTEMPTS):
        fills = get_spot_fills_since(order_start_ms - 2000)
        fill = calculate_fill(fills, True)

        if fill:
            break

        time.sleep(FILL_CHECK_DELAY)

    if not fill:
        raise RuntimeError("BUY inviato ma fill Spot non rilevato.")

    actual_size = fill["size"]
    actual_price = fill["price"]
    actual_notional = fill["notional"]

    fee = estimate_fee(actual_notional)

    target_price = actual_price * (1 + TAKE_PROFIT_PERCENT / 100)

    lot = {
        "id": state["next_lot_id"],
        "buy_time": int(datetime.now(timezone.utc).timestamp() * 1000),
        "buy_price": actual_price,
        "buy_size": actual_size,
        "remaining_size": actual_size,
        "buy_notional": actual_notional,
        "buy_fee": fee,
        "target_price": target_price
    }

    state["next_lot_id"] += 1

    state["open_lots"].append(lot)

    state["weekly_buys"] += 1

    state["last_buy"] = lot

    save_state(state)

    log(
        f"BUY SPOT CONFERMATO | "
        f"lotto #{lot['id']} | "
        f"{actual_size:.8f} BTC | "
        f"prezzo ${actual_price:.2f} | "
        f"investiti ${actual_notional:.4f} | "
        f"TP ${target_price:.2f}"
    )

    return True


# ============================================================
# LOTTI VENDIBILI
# ============================================================

def get_sellable_lots(state):
    current_price = get_spot_price()

    eligible = []

    for lot in sorted(state["open_lots"], key=lambda x: x["id"]):
        if lot["remaining_size"] <= POSITION_TOLERANCE:
            continue

        if current_price >= lot["target_price"]:
            eligible.append(lot)

    return eligible


# ============================================================
# SELL SPOT
# ============================================================

def sell_lot(state, lot):
    sell_size = round_btc(lot["remaining_size"])

    if sell_size <= 0:
        return False

    current_price = get_spot_price()

    if current_price < lot["target_price"]:
        return False

    balances = get_spot_balances()

    if balances["btc_available"] + POSITION_TOLERANCE < sell_size:
        raise RuntimeError("BTC Spot disponibile inferiore al lotto da vendere.")

    log(
        f"SELL SPOT | "
        f"lotto #{lot['id']} | "
        f"{sell_size:.8f} BTC | "
        f"prezzo ${current_price:.2f} | "
        f"target ${lot['target_price']:.2f}"
    )

    order_start_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    result = spot_market_order(False, sell_size)

    if result.get("status") != "ok":
        raise RuntimeError(f"SELL SPOT rifiutato: {result}")

    time.sleep(POST_ORDER_DELAY)

    fill = None

    for _ in range(FILL_CHECK_ATTEMPTS):
        fills = get_spot_fills_since(order_start_ms - 2000)
        fill = calculate_fill(fills, False)

        if fill:
            break

        time.sleep(FILL_CHECK_DELAY)

    if not fill:
        raise RuntimeError("SELL inviato ma fill Spot non rilevato.")

    sold_size = min(fill["size"], lot["remaining_size"])

    sell_price = fill["price"]
    sell_notional = fill["notional"]

    allocated_buy_cost = lot["buy_price"] * sold_size

    allocated_buy_fee = lot["buy_fee"] * (sold_size / lot["buy_size"])

    sell_fee = estimate_fee(sell_notional)

    gross_pnl = sell_notional - allocated_buy_cost

    net_pnl = gross_pnl - allocated_buy_fee - sell_fee

    lot["remaining_size"] = max(0.0, lot["remaining_size"] - sold_size)

    trade = {
        "lot_id": lot["id"],
        "sell_time": int(datetime.now(timezone.utc).timestamp() * 1000),
        "sell_size": sold_size,
        "sell_price": sell_price,
        "sell_notional": sell_notional,
        "buy_cost_allocated": allocated_buy_cost,
        "buy_fee_allocated": allocated_buy_fee,
        "sell_fee": sell_fee,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl
    }

    state["sell_trades"].append(trade)

    if lot["remaining_size"] <= POSITION_TOLERANCE:
        state["open_lots"].remove(lot)

    state["last_sell"] = trade

    save_state(state)

    log(
        f"SELL SPOT CONFERMATO | "
        f"lotto #{lot['id']} | "
        f"{sold_size:.8f} BTC | "
        f"prezzo ${sell_price:.2f} | "
        f"PnL netto ${net_pnl:.4f}"
    )

    return True


# ============================================================
# SELL CHECK
# ============================================================

def check_sell(state):
    eligible = get_sellable_lots(state)

    if not eligible:
        return False

    # FIFO
    lot = eligible[0]

    return sell_lot(state, lot)


# ============================================================
# BUY CHECK
# ============================================================

def check_buy(state, latest_close, previous_close):
    if previous_close <= 0:
        return False

    change_percent = ((latest_close - previous_close) / previous_close) * 100

    log(f"VARIAZIONE 4H SPOT | {change_percent:.4f}%")

    if change_percent <= -DIP_PERCENT:
        log(f"DIP SPOT RILEVATO | {change_percent:.4f}% <= -{DIP_PERCENT:.2f}%")
        return place_buy(state)

    return False


# ============================================================
# PERFORMANCE
# ============================================================

def calculate_performance(state):
    balances = get_spot_balances()

    current_price = get_spot_price()

    btc_value = balances["btc_total"] * current_price

    open_cost = 0.0
    open_size = 0.0
    open_buy_fees = 0.0

    for lot in state["open_lots"]:
        size = float(lot["remaining_size"])

        open_size += size

        open_cost += size * float(lot["buy_price"])

        if lot["buy_size"] > 0:
            open_buy_fees += float(lot["buy_fee"]) * (size / float(lot["buy_size"]))

    closed_buy_cost = sum(float(trade["buy_cost_allocated"]) for trade in state["sell_trades"])

    historical_buy_cost = open_cost + closed_buy_cost

    sold_notional = sum(float(trade["sell_notional"]) for trade in state["sell_trades"])

    realized_net = sum(float(trade["net_pnl"]) for trade in state["sell_trades"])

    unrealized_gross = btc_value - open_cost

    estimated_exit_fee = estimate_fee(btc_value)

    total_net_pnl = realized_net + unrealized_gross - open_buy_fees - estimated_exit_fee

    if historical_buy_cost > 0:
        return_percent = (total_net_pnl / historical_buy_cost) * 100
    else:
        return_percent = 0.0

    return {
        "usdc_available": balances["usdc_available"],
        "btc_size": balances["btc_total"],
        "btc_value": btc_value,
        "open_cost": open_cost,
        "weighted_avg_price": (open_cost / open_size if open_size > 0 else 0.0),
        "sold_notional": sold_notional,
        "realized_net": realized_net,
        "unrealized_gross": unrealized_gross,
        "total_net_pnl": total_net_pnl,
        "return_percent": return_percent
    }


# ============================================================
# LOG CAPITALE
# ============================================================

def log_capital():
    balances = get_spot_balances()

    price = get_spot_price()

    btc_value = balances["btc_total"] * price

    log(
        f"CAPITALE SPOT | "
        f"USDC disponibile ${balances['usdc_available']:.4f} | "
        f"BTC {balances['btc_total']:.8f} "
        f"(~${btc_value:.4f})"
    )


# ============================================================
# MAIN
# ============================================================

def run():
    global state

    state = load_state()

    refresh_week(state)

    time.sleep(STARTUP_DELAY)

    log("=" * 50)

    log("AVVIO BOT HYPERLIQUID BTC SPOT")

    log(
        f"PARAMETRI | "
        f"BUY ${BUY_USD:.2f} | "
        f"DIP {DIP_PERCENT:.2f}% | "
        f"TP {TAKE_PROFIT_PERCENT:.2f}% | "
        f"MAX BTC ${MAX_POSITION_USD:.2f} | "
        f"MAX BUY SETT {MAX_WEEKLY_BUYS}"
    )

    log_capital()

    # --------------------------------------------------------
    # CONTROLLO SALDO BTC
    # --------------------------------------------------------

    verify_spot_position(state)

    # --------------------------------------------------------
    # CANDELE 4H SPOT
    # --------------------------------------------------------

    candles = get_closed_4h_candles()

    if len(candles) < 2:
        log("Non ci sono abbastanza candele 4H Spot.")
        return

    latest = candles[-1]
    previous = candles[-2]

    latest_candle_time = int(latest["T"])

    latest_close = float(latest["c"])

    previous_close = float(previous["c"])

    latest_datetime = datetime.fromtimestamp(latest_candle_time / 1000, tz=timezone.utc)

    log(f"ULTIMA 4H SPOT CHIUSA | {latest_datetime}")

    # --------------------------------------------------------
    # EVITA DOPPIA ELABORAZIONE
    # --------------------------------------------------------

    if state.get("last_processed_candle") == latest_candle_time:
        log("Candela già elaborata. Nessuna operazione.")

        performance = calculate_performance(state)

        log(
            f"PERFORMANCE SPOT | "
            f"USDC disponibile ${performance['usdc_available']:.4f} | "
            f"BTC {performance['btc_size']:.8f} | "
            f"valore BTC ${performance['btc_value']:.4f} | "
            f"investito ${performance['open_cost']:.4f} | "
            f"venduto ${performance['sold_notional']:.4f} | "
            f"media acquisto ${performance['weighted_avg_price']:.2f} | "
            f"realizzato ${performance['realized_net']:.4f} | "
            f"unrealizzato ${performance['unrealized_gross']:.4f} | "
            f"PnL totale ${performance['total_net_pnl']:.4f} | "
            f"rendimento {performance['return_percent']:.2f}%"
        )

        return

    # --------------------------------------------------------
    # SELL PRIMA DEL BUY
    # --------------------------------------------------------

    sold = check_sell(state)

    if sold:
        state["last_processed_candle"] = latest_candle_time

        save_state(state)

        log("SELL SPOT eseguito: nessun BUY sulla stessa candela.")

        log_capital()

        return

    # --------------------------------------------------------
    # BUY
    # --------------------------------------------------------

    bought = check_buy(state, latest_close, previous_close)

    if bought:
        log("BUY SPOT eseguito.")
    else:
        log("Nessun BUY SPOT.")

    # --------------------------------------------------------
    # CONTROLLO FINALE
    # --------------------------------------------------------

    verify_spot_position(state)

    state["last_processed_candle"] = latest_candle_time

    save_state(state)

    log_capital()

    performance = calculate_performance(state)

    log(
        f"PERFORMANCE SPOT | "
        f"USDC disponibile ${performance['usdc_available']:.4f} | "
        f"BTC {performance['btc_size']:.8f} | "
        f"valore BTC ${performance['btc_value']:.4f} | "
        f"investito ${performance['open_cost']:.4f} | "
        f"venduto ${performance['sold_notional']:.4f} | "
        f"media acquisto ${performance['weighted_avg_price']:.2f} | "
        f"realizzato ${performance['realized_net']:.4f} | "
        f"unrealizzato ${performance['unrealized_gross']:.4f} | "
        f"PnL totale ${performance['total_net_pnl']:.4f} | "
        f"rendimento {performance['return_percent']:.2f}%"
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log(f"ERRORE FATALE | {e}\n{traceback.format_exc()}")
        sys.exit(1)
