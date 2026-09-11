import json
import os
import sys
import time
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

COIN = "BTC"
INTERVAL = "4h"

PRIVATE_KEY = os.environ["HYPERLIQUID_PRIVATE_KEY"]
ACCOUNT_ADDRESS = os.environ["HYPERLIQUID_ACCOUNT_ADDRESS"]

# Questi valori sono modificabili direttamente da Railway.
BUY_USD = float(os.getenv("BUY_USD", "10"))
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "200"))
MAX_WEEKLY_BUYS = int(os.getenv("MAX_WEEKLY_BUYS", "10"))

DIP_PERCENT = float(os.getenv("DIP_PERCENT", "2"))
TAKE_PROFIT_PERCENT = float(
    os.getenv("TAKE_PROFIT_PERCENT", "1")
)

MAX_SLIPPAGE = float(
    os.getenv("MAX_SLIPPAGE", "0.01")
)

MAX_LOTS_TO_SELL_PER_RUN = int(
    os.getenv("MAX_LOTS_TO_SELL_PER_RUN", "1")
)

STATE_DIR = os.getenv("STATE_DIR", "/data")
STATE_FILE = os.path.join(
    STATE_DIR,
    "state.json"
)

STARTUP_DELAY = int(
    os.getenv("STARTUP_DELAY", "5")
)

POST_ORDER_DELAY = int(
    os.getenv("POST_ORDER_DELAY", "3")
)

FILL_CHECK_ATTEMPTS = int(
    os.getenv("FILL_CHECK_ATTEMPTS", "5")
)

FILL_CHECK_DELAY = float(
    os.getenv("FILL_CHECK_DELAY", "1")
)

POSITION_TOLERANCE = float(
    os.getenv("POSITION_TOLERANCE", "0.00000001")
)


# ============================================================
# CONNESSIONE HYPERLIQUID
# ============================================================

wallet = Account.from_key(PRIVATE_KEY)

info = Info(
    constants.MAINNET_API_URL,
    skip_ws=True,
)

exchange = Exchange(
    wallet,
    constants.MAINNET_API_URL,
    account_address=ACCOUNT_ADDRESS,
)


# ============================================================
# UTILITY
# ============================================================

def now_ms():
    return int(time.time() * 1000)


def utc_week_id():
    now = datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def log(message):
    timestamp = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    print(
        f"[{timestamp}] {message}",
        flush=True
    )


def ensure_state_dir():
    os.makedirs(
        STATE_DIR,
        exist_ok=True
    )


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "version": 3,

        "performance_start_ms": now_ms(),

        "last_processed_candle": None,

        "week_id": utc_week_id(),
        "weekly_buys": 0,

        "next_lot_id": 1,

        # Lotti ancora aperti
        "lots": [],

        # Storico completo delle vendite
        "sell_trades": [],

        "last_buy": None,
        "last_sell": None,
    }


def load_state():
    ensure_state_dir()

    if not os.path.exists(STATE_FILE):
        state = default_state()
        save_state(state)
        return state

    with open(
        STATE_FILE,
        "r",
        encoding="utf-8"
    ) as f:
        state = json.load(f)

    state.setdefault("version", 3)
    state.setdefault(
        "performance_start_ms",
        now_ms()
    )
    state.setdefault(
        "last_processed_candle",
        None
    )
    state.setdefault(
        "week_id",
        utc_week_id()
    )
    state.setdefault(
        "weekly_buys",
        0
    )
    state.setdefault(
        "next_lot_id",
        1
    )
    state.setdefault(
        "lots",
        []
    )
    state.setdefault(
        "sell_trades",
        []
    )
    state.setdefault(
        "last_buy",
        None
    )
    state.setdefault(
        "last_sell",
        None
    )

    return state


def save_state(state):
    ensure_state_dir()

    temp_file = STATE_FILE + ".tmp"

    with open(
        temp_file,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            state,
            f,
            indent=2,
            ensure_ascii=False
        )

    os.replace(
        temp_file,
        STATE_FILE
    )


# ============================================================
# SETTIMANA
# ============================================================

def reset_week_if_needed(state):
    current_week = utc_week_id()

    if state["week_id"] != current_week:

        state["week_id"] = current_week
        state["weekly_buys"] = 0

        save_state(state)

        log(
            f"Nuova settimana: {current_week}"
        )


# ============================================================
# METADATA BTC
# ============================================================

def get_sz_decimals():
    meta = info.meta()

    for item in meta["universe"]:

        if item["name"] == COIN:
            return int(
                item["szDecimals"]
            )

    raise RuntimeError(
        f"{COIN} non trovato nei metadata Hyperliquid."
    )


SZ_DECIMALS = get_sz_decimals()


def round_size(size):
    return round(
        float(size),
        SZ_DECIMALS
    )


# ============================================================
# CAPITALE / ACCOUNT
# ============================================================

def get_account_state():
    """
    Legge il capitale del conto Hyperliquid.

    account_value  = valore totale del conto
    withdrawable   = USDC disponibile/non impegnato
    margin_used    = margine utilizzato
    """

    user_state = info.user_state(
        ACCOUNT_ADDRESS
    )
    
    log(f"ACCOUNT RAW | {user_state}")

    margin_summary = user_state.get(
        "marginSummary",
        {}
    )

    cross_margin_summary = user_state.get(
        "crossMarginSummary",
        {}
    )

    account_value = float(
        margin_summary.get(
            "accountValue",
            0
        ) or 0
    )

    margin_used = float(
        margin_summary.get(
            "totalMarginUsed",
            0
        ) or 0
    )

    # withdrawable è al livello principale
    # della risposta clearinghouseState.
    withdrawable_raw = user_state.get(
        "withdrawable"
    )

    if withdrawable_raw is None:
        withdrawable_raw = cross_margin_summary.get(
            "withdrawable"
        )

    available_usdc = float(
        withdrawable_raw or 0
    )

    log(
        f"ACCOUNT DEBUG | "
        f"accountValue={account_value} | "
        f"withdrawable={available_usdc} | "
        f"marginUsed={margin_used}"
    )

    return {
        "account_value": account_value,
        "available_usdc": available_usdc,
        "margin_used": margin_used,
    }

def log_capital():
    account = get_account_state()
    position = get_real_position()

    log(
        "CAPITALE | "
        f"disponibile ${account['available_usdc']:.2f} | "
        f"account ${account['account_value']:.2f} | "
        f"margine ${account['margin_used']:.2f} | "
        f"posizione BTC ${position['position_value']:.2f}"
    )

    return account


# ============================================================
# PREZZO
# ============================================================

def get_mid_price():
    mids = info.all_mids()

    if COIN not in mids:
        raise RuntimeError(
            f"Prezzo {COIN} non disponibile."
        )

    return float(
        mids[COIN]
    )


# ============================================================
# CANDELE 4H
# ============================================================

def get_last_two_closed_candles():

    current_ms = now_ms()

    start_ms = (
        current_ms
        - (4 * 60 * 60 * 1000 * 6)
    )

    candles = info.candles_snapshot(
        COIN,
        INTERVAL,
        start_ms,
        current_ms
    )

    closed = [
        candle
        for candle in candles
        if int(candle["T"]) <= current_ms
    ]

    if len(closed) < 2:
        raise RuntimeError(
            "Non ci sono almeno due candele 4H chiuse."
        )

    closed.sort(
        key=lambda x: int(x["T"])
    )

    return (
        closed[-2],
        closed[-1]
    )


# ============================================================
# POSIZIONE REALE
# ============================================================

def get_real_position():

    user_state = info.user_state(
        ACCOUNT_ADDRESS
    )

    for item in user_state.get(
        "assetPositions",
        []
    ):

        position = item["position"]

        if position["coin"] == COIN:

            size = float(
                position["szi"]
            )

            entry_px = position.get(
                "entryPx"
            )

            return {
                "size": size,

                "entry_price": (
                    float(entry_px)
                    if entry_px not in (
                        None,
                        "None"
                    )
                    else 0.0
                ),

                "position_value": float(
                    position["positionValue"]
                ),

                "unrealized_pnl": float(
                    position["unrealizedPnl"]
                ),
            }

    return {
        "size": 0.0,
        "entry_price": 0.0,
        "position_value": 0.0,
        "unrealized_pnl": 0.0,
    }


def local_lots_size(state):

    return sum(
        float(lot["remaining_size"])
        for lot in state["lots"]
    )


def verify_position_consistency(state):

    real = get_real_position()

    real_size = real["size"]
    local_size = local_lots_size(state)

    # Il bot deve gestire soltanto una posizione LONG.
    if real_size < -POSITION_TOLERANCE:

        raise RuntimeError(
            f"POSIZIONE SHORT RILEVATA: "
            f"{real_size} BTC. BOT BLOCCATO."
        )

    if (
        abs(
            real_size
            - local_size
        )
        > POSITION_TOLERANCE
    ):

        raise RuntimeError(
            "DISALLINEAMENTO POSIZIONE!\n"
            f"Hyperliquid: {real_size} BTC\n"
            f"Lotti locali: {local_size} BTC\n"
            "NESSUNA OPERAZIONE ESEGUITA."
        )

    return real


# ============================================================
# ORDINI / FILL
# ============================================================

def extract_filled_order(order_result):

    if not isinstance(
        order_result,
        dict
    ):
        raise RuntimeError(
            f"Risposta ordine non valida: "
            f"{order_result}"
        )

    if order_result.get(
        "status"
    ) != "ok":

        raise RuntimeError(
            f"Ordine rifiutato: "
            f"{order_result}"
        )

    response = order_result.get(
        "response",
        {}
    )

    data = response.get(
        "data",
        {}
    )

    statuses = data.get(
        "statuses",
        []
    )

    for status in statuses:

        if "filled" in status:

            filled = status["filled"]

            return {
                "oid": int(
                    filled["oid"]
                ),

                "size": float(
                    filled["totalSz"]
                ),

                "avg_price": float(
                    filled["avgPx"]
                ),
            }

        if "error" in status:

            raise RuntimeError(
                f"Ordine non eseguito: "
                f"{status['error']}"
            )

    raise RuntimeError(
        f"Ordine senza fill: "
        f"{order_result}"
    )


# ============================================================
# COMMISSIONI
# ============================================================

def estimate_fee(
    notional
):
    """
    L'API corrente dei fill non espone la fee
    direttamente nel record del fill.
    Usiamo quindi il userCrossRate come stima
    della fee taker.
    """

    try:

        fees = info.user_fees(
            ACCOUNT_ADDRESS
        )

        cross_rate = float(
            fees["userCrossRate"]
        )

        return (
            float(notional)
            * cross_rate
        )

    except Exception as exc:

        log(
            f"Avviso calcolo commissione: "
            f"{exc}"
        )

        return 0.0


# ============================================================
# WAIT POSIZIONE
# ============================================================

def wait_for_position_size(
    expected_size
):

    for _ in range(
        FILL_CHECK_ATTEMPTS
    ):

        position = get_real_position()

        if (
            abs(
                position["size"]
                - expected_size
            )
            <= POSITION_TOLERANCE
        ):

            return position

        time.sleep(
            FILL_CHECK_DELAY
        )

    raise RuntimeError(
        "La posizione reale non corrisponde "
        "a quella attesa dopo l'ordine."
    )


# ============================================================
# BUY
# ============================================================

def place_buy(state):

    # Posizione PRIMA dell'ordine.
    real_before = get_real_position()

    if real_before["size"] < -POSITION_TOLERANCE:

        raise RuntimeError(
            "Posizione SHORT: BUY bloccato."
        )

    current_price = get_mid_price()

    current_position_value = (
        abs(real_before["size"])
        * current_price
    )

    # --------------------------------------------------------
    # LIMITE POSIZIONE
    # --------------------------------------------------------

    if (
        current_position_value
        + BUY_USD
        > MAX_POSITION_USD
    ):

        log(
            "BUY BLOCCATO | "
            f"posizione ${current_position_value:.2f} + "
            f"BUY ${BUY_USD:.2f} > "
            f"limite ${MAX_POSITION_USD:.2f}"
        )

        return False

    # --------------------------------------------------------
    # LIMITE BUY SETTIMANALI
    # --------------------------------------------------------

    if (
        state["weekly_buys"]
        >= MAX_WEEKLY_BUYS
    ):

        log(
            "BUY BLOCCATO | "
            f"raggiunti {MAX_WEEKLY_BUYS} "
            "BUY settimanali"
        )

        return False

    # --------------------------------------------------------
    # CAPITALE DISPONIBILE
    # --------------------------------------------------------

    account = get_account_state()

    if (
        account["available_usdc"]
        < BUY_USD
    ):

        log(
            "BUY BLOCCATO | "
            f"capitale disponibile "
            f"${account['available_usdc']:.2f} "
            f"< BUY ${BUY_USD:.2f}"
        )

        return False

    # --------------------------------------------------------
    # QUANTITÀ
    # --------------------------------------------------------

    size = (
        BUY_USD
        / current_price
    )

    size = round_size(
        size
    )

    if size <= 0:

        raise RuntimeError(
            "Quantità BUY arrotondata a zero."
        )

    log(
        f"BUY | "
        f"{size:.8f} BTC | "
        f"valore circa ${BUY_USD:.2f} | "
        f"prezzo ${current_price:.2f}"
    )

    # --------------------------------------------------------
    # ORDINE
    # --------------------------------------------------------

    order_result = exchange.market_open(
        COIN,
        True,
        size,
        None,
        MAX_SLIPPAGE
    )

    fill = extract_filled_order(
        order_result
    )

    time.sleep(
        POST_ORDER_DELAY
    )

    # IMPORTANTE:
    # expected_size parte dalla posizione PRIMA del BUY.
    expected_size = (
        real_before["size"]
        + fill["size"]
    )

    wait_for_position_size(
        expected_size
    )

    # --------------------------------------------------------
    # LOTTO
    # --------------------------------------------------------

    buy_notional = (
        fill["size"]
        * fill["avg_price"]
    )

    buy_fee = estimate_fee(
        buy_notional
    )

    lot_id = state[
        "next_lot_id"
    ]

    state[
        "next_lot_id"
    ] += 1

    lot = {

        "lot_id": lot_id,

        "created_at_ms": now_ms(),

        "buy_oid": fill["oid"],

        "buy_size": fill["size"],

        "remaining_size": fill["size"],

        "buy_price": fill["avg_price"],

        "buy_notional": buy_notional,

        "buy_fee_est": buy_fee,

        "sold_size": 0.0,

        "sold_notional": 0.0,

        "realized_gross_pnl": 0.0,

        "realized_net_pnl": 0.0,
    }

    state["lots"].append(
        lot
    )

    state["weekly_buys"] += 1

    state["last_buy"] = {

        "time_ms": now_ms(),

        "oid": fill["oid"],

        "size": fill["size"],

        "price": fill["avg_price"],

        "notional": buy_notional,

        "fee_est": buy_fee,

        "lot_id": lot_id,
    }

    save_state(state)

    log(
        f"BUY ESEGUITO | "
        f"lotto #{lot_id} | "
        f"{fill['size']:.8f} BTC @ "
        f"${fill['avg_price']:.2f} | "
        f"fee stimata ${buy_fee:.6f}"
    )

    return True


# ============================================================
# LOTTI VENDIBILI
# ============================================================

def get_sellable_lots(
    state,
    current_price
):

    eligible = []

    target_multiplier = (
        1
        + TAKE_PROFIT_PERCENT / 100
    )

    for lot in state["lots"]:

        remaining = float(
            lot["remaining_size"]
        )

        buy_price = float(
            lot["buy_price"]
        )

        target_price = (
            buy_price
            * target_multiplier
        )

        if (
            remaining
            > POSITION_TOLERANCE
            and current_price
            >= target_price
        ):

            eligible.append(
                {
                    "lot": lot,
                    "target_price": target_price,
                }
            )

    # FIFO
    eligible.sort(
        key=lambda x:
        int(
            x["lot"]["lot_id"]
        )
    )

    return eligible


# ============================================================
# SELL SINGOLO LOTTO
# ============================================================

def sell_lot(
    state,
    lot,
    current_price
):

    lot_id = int(
        lot["lot_id"]
    )

    original_remaining = float(
        lot["remaining_size"]
    )

    if (
        original_remaining
        <= POSITION_TOLERANCE
    ):
        return False

    real_before = get_real_position()

    if (
        real_before["size"]
        <= POSITION_TOLERANCE
    ):

        raise RuntimeError(
            "Nessuna posizione reale disponibile "
            "per vendere il lotto."
        )

    # Mai vendere più della posizione reale.
    sell_size = min(
        original_remaining,
        real_before["size"]
    )

    sell_size = round_size(
        sell_size
    )

    if sell_size <= 0:

        raise RuntimeError(
            "SELL size arrotondata a zero."
        )

    buy_price = float(
        lot["buy_price"]
    )

    target_price = (
        buy_price
        * (
            1
            + TAKE_PROFIT_PERCENT / 100
        )
    )

    log(
        f"SELL | lotto #{lot_id} | "
        f"{sell_size:.8f} BTC | "
        f"prezzo ${current_price:.2f} | "
        f"target ${target_price:.2f}"
    )

    # --------------------------------------------------------
    # SELL REDUCE-ONLY
    # --------------------------------------------------------

    order_result = exchange.market_close(
        COIN,
        sell_size,
        None,
        MAX_SLIPPAGE
    )

    fill = extract_filled_order(
        order_result
    )

    time.sleep(
        POST_ORDER_DELAY
    )

    expected_size = (
        real_before["size"]
        - fill["size"]
    )

    wait_for_position_size(
        expected_size
    )

    # --------------------------------------------------------
    # RISULTATO
    # --------------------------------------------------------

    sold_size = fill[
        "size"
    ]

    sell_price = fill[
        "avg_price"
    ]

    sell_notional = (
        sold_size
        * sell_price
    )

    buy_cost_allocated = (
        sold_size
        * buy_price
    )

    original_buy_size = float(
        lot["buy_size"]
    )

    if original_buy_size > 0:

        buy_fee_allocated = (
            float(
                lot["buy_fee_est"]
            )
            * sold_size
            / original_buy_size
        )

    else:

        buy_fee_allocated = 0.0

    sell_fee = estimate_fee(
        sell_notional
    )

    gross_pnl = (
        sell_notional
        - buy_cost_allocated
    )

    net_pnl = (
        gross_pnl
        - buy_fee_allocated
        - sell_fee
    )

    # --------------------------------------------------------
    # AGGIORNA LOTTO
    # --------------------------------------------------------

    lot["remaining_size"] = max(
        0.0,
        original_remaining
        - sold_size
    )

    lot["sold_size"] = (
        float(
            lot["sold_size"]
        )
        + sold_size
    )

    lot["sold_notional"] = (
        float(
            lot["sold_notional"]
        )
        + sell_notional
    )

    lot["realized_gross_pnl"] = (
        float(
            lot["realized_gross_pnl"]
        )
        + gross_pnl
    )

    lot["realized_net_pnl"] = (
        float(
            lot["realized_net_pnl"]
        )
        + net_pnl
    )

    # --------------------------------------------------------
    # STORICO
    # --------------------------------------------------------

    sell_trade = {

        "time_ms": now_ms(),

        "lot_id": lot_id,

        "sell_oid": fill["oid"],

        "size": sold_size,

        "buy_price": buy_price,

        "sell_price": sell_price,

        "buy_cost_allocated":
            buy_cost_allocated,

        "sell_notional":
            sell_notional,

        "buy_fee_allocated_est":
            buy_fee_allocated,

        "sell_fee_est":
            sell_fee,

        "gross_pnl":
            gross_pnl,

        "net_pnl":
            net_pnl,

        "return_percent":
            (
                net_pnl
                / buy_cost_allocated
                * 100
                if buy_cost_allocated > 0
                else 0.0
            ),
    }

    state[
        "sell_trades"
    ].append(
        sell_trade
    )

    # Se completamente venduto,
    # il lotto esce dalla lista aperta.
    if (
        lot["remaining_size"]
        <= POSITION_TOLERANCE
    ):

        state["lots"] = [
            item
            for item in state["lots"]
            if int(
                item["lot_id"]
            ) != lot_id
        ]

    state["last_sell"] = (
        sell_trade
    )

    save_state(state)

    log(
        f"SELL ESEGUITO | "
        f"lotto #{lot_id} | "
        f"{sold_size:.8f} BTC @ "
        f"${sell_price:.2f} | "
        f"PnL lordo ${gross_pnl:.4f} | "
        f"PnL netto ${net_pnl:.4f}"
    )

    return True


# ============================================================
# SELL CHECK
# ============================================================

def check_sell(
    state,
    current_price
):

    eligible = get_sellable_lots(
        state,
        current_price
    )

    if not eligible:
        return False

    sold_any = False

    for item in eligible[
        :MAX_LOTS_TO_SELL_PER_RUN
    ]:

        sell_lot(
            state,
            item["lot"],
            current_price
        )

        sold_any = True

    return sold_any


# ============================================================
# BUY CHECK
# ============================================================

def check_buy(
    state,
    previous_candle,
    latest_candle
):

    previous_close = float(
        previous_candle["c"]
    )

    latest_close = float(
        latest_candle["c"]
    )

    change_percent = (
        (
            latest_close
            - previous_close
        )
        / previous_close
        * 100
    )

    dip = -change_percent

    log(
        f"4H | "
        f"precedente ${previous_close:.2f} -> "
        f"ultima ${latest_close:.2f} | "
        f"variazione {change_percent:.2f}%"
    )

    if dip < DIP_PERCENT:

        log(
            f"Nessun BUY | "
            f"dip {dip:.2f}% < "
            f"{DIP_PERCENT:.2f}%"
        )

        return False

    log(
        f"CONDIZIONE BUY | "
        f"dip {dip:.2f}% >= "
        f"{DIP_PERCENT:.2f}%"
    )

    return place_buy(
        state
    )


# ============================================================
# FUNDING
# ============================================================

def get_funding_pnl(state):

    start_ms = int(
        state.get(
            "performance_start_ms",
            now_ms()
        )
    )

    try:

        funding = (
            info.user_funding_history(
                ACCOUNT_ADDRESS,
                start_ms,
                now_ms()
            )
        )

        total = 0.0

        for item in funding:

            delta = item.get(
                "delta",
                {}
            )

            # Supporta le risposte in cui
            # il valore USDC è nel delta.
            if (
                delta.get("coin")
                == "USDC"
            ):

                total += float(
                    delta.get(
                        "usdc",
                        0
                    )
                )

        return total

    except Exception as exc:

        log(
            f"Avviso lettura funding: "
            f"{exc}"
        )

        return 0.0


# ============================================================
# PERFORMANCE
# ============================================================

def calculate_performance(
    state
):

    current_price = (
        get_mid_price()
    )

    # --------------------------------------------------------
    # STORICO COMPLETO
    # --------------------------------------------------------

    total_buy_notional = 0.0
    total_buy_fees = 0.0

    total_sell_notional = 0.0
    total_sell_fees = 0.0

    realized_gross = 0.0
    realized_net = 0.0

    # --------------------------------------------------------
    # LOTTI APERTI
    # --------------------------------------------------------

    open_size = 0.0
    open_cost = 0.0
    open_buy_fees = 0.0

    for lot in state["lots"]:

        buy_notional = float(
            lot["buy_notional"]
        )

        buy_fee = float(
            lot["buy_fee_est"]
        )

        total_buy_notional += (
            buy_notional
        )

        total_buy_fees += (
            buy_fee
        )

        remaining = float(
            lot["remaining_size"]
        )

        buy_price = float(
            lot["buy_price"]
        )

        open_size += remaining

        open_cost += (
            remaining
            * buy_price
        )

        if float(
            lot["buy_size"]
        ) > 0:

            open_buy_fees += (
                buy_fee
                * remaining
                / float(
                    lot["buy_size"]
                )
            )

    # --------------------------------------------------------
    # LOTTI CHIUSI / VENDITE
    # --------------------------------------------------------

    for trade in state[
        "sell_trades"
    ]:

        total_sell_notional += float(
            trade["sell_notional"]
        )

        total_sell_fees += float(
            trade["sell_fee_est"]
        )

        realized_gross += float(
            trade["gross_pnl"]
        )

        realized_net += float(
            trade["net_pnl"]
        )

        # Il costo BUY della parte venduta
        # era già contabilizzato nel trade.
        total_buy_notional += 0.0

    # --------------------------------------------------------
    # UNREALIZED
    # --------------------------------------------------------

    unrealized_gross = 0.0

    for lot in state["lots"]:

        remaining = float(
            lot["remaining_size"]
        )

        buy_price = float(
            lot["buy_price"]
        )

        unrealized_gross += (
            current_price
            - buy_price
        ) * remaining

    # --------------------------------------------------------
    # MEDIA PESATA POSIZIONE
    # --------------------------------------------------------

    weighted_avg_open_price = (
        open_cost / open_size
        if open_size > 0
        else 0.0
    )

    # --------------------------------------------------------
    # FEE USCITA STIMATA
    # --------------------------------------------------------

    estimated_exit_fee = 0.0

    if open_size > 0:

        estimated_exit_fee = (
            estimate_fee(
                open_size
                * current_price
            )
        )

    # --------------------------------------------------------
    # FUNDING
    # --------------------------------------------------------

    funding_pnl = (
        get_funding_pnl(
            state
        )
    )

    # --------------------------------------------------------
    # PNL TOTALE
    # --------------------------------------------------------

    total_net_pnl = (
        realized_net
        + unrealized_gross
        + funding_pnl
        - open_buy_fees
        - estimated_exit_fee
    )

    # Capitale effettivamente acquistato
    # dall'inizio della strategia:
    #
    # BUY dei lotti aperti
    # + costo BUY delle parti già vendute.
    #
    # Per le parti vendute ricaviamo il costo
    # direttamente dai sell_trade.
    closed_buy_cost = sum(
        float(
            trade["buy_cost_allocated"]
        )
        for trade in state[
            "sell_trades"
        ]
    )

    total_capital_deployed = (
        open_cost
        + closed_buy_cost
    )

    return_percent = (
        total_net_pnl
        / total_capital_deployed
        * 100
        if total_capital_deployed > 0
        else 0.0
    )

    return {
        "current_price":
            current_price,

        "total_buy_notional":
            total_buy_notional,

        "total_buy_fees":
            total_buy_fees,

        "total_sell_notional":
            total_sell_notional,

        "total_sell_fees":
            total_sell_fees,

        "open_size":
            open_size,

        "open_cost":
            open_cost,

        "weighted_avg_open_price":
            weighted_avg_open_price,

        "realized_gross_pnl":
            realized_gross,

        "realized_net_pnl":
            realized_net,

        "unrealized_gross_pnl":
            unrealized_gross,

        "funding_pnl":
            funding_pnl,

        "estimated_exit_fee":
            estimated_exit_fee,

        "total_capital_deployed":
            total_capital_deployed,

        "total_net_pnl":
            total_net_pnl,

        "return_percent":
            return_percent,
    }


def log_performance(state):

    performance = (
        calculate_performance(
            state
        )
    )

    account = (
        get_account_state()
    )

    log(
        "PERFORMANCE | "
        f"capitale disponibile "
        f"${account['available_usdc']:.2f} | "
        f"investito ${performance['total_capital_deployed']:.2f} | "
        f"venduto ${performance['total_sell_notional']:.2f} | "
        f"posizione {performance['open_size']:.8f} BTC | "
        f"media acquisto "
        f"${performance['weighted_avg_open_price']:.2f} | "
        f"realizzato "
        f"${performance['realized_net_pnl']:.4f} | "
        f"unrealizzato "
        f"${performance['unrealized_gross_pnl']:.4f} | "
        f"funding "
        f"${performance['funding_pnl']:.4f} | "
        f"PnL totale "
        f"${performance['total_net_pnl']:.4f} | "
        f"rendimento "
        f"{performance['return_percent']:.2f}%"
    )


# ============================================================
# CICLO PRINCIPALE
# ============================================================

def run():

    log(
        "=================================================="
    )

    log(
        "AVVIO BOT HYPERLIQUID BTC"
    )

    log(
        f"PARAMETRI | "
        f"BUY ${BUY_USD:.2f} | "
        f"DIP {DIP_PERCENT:.2f}% | "
        f"TP {TAKE_PROFIT_PERCENT:.2f}% | "
        f"MAX POS ${MAX_POSITION_USD:.2f} | "
        f"MAX BUY SETT {MAX_WEEKLY_BUYS}"
    )

    state = load_state()

    reset_week_if_needed(
        state
    )

    # --------------------------------------------------------
    # CAPITALE
    # --------------------------------------------------------

    log_capital()

    # --------------------------------------------------------
    # SICUREZZA POSIZIONE
    # --------------------------------------------------------

    verify_position_consistency(
        state
    )

    # --------------------------------------------------------
    # CANDELE
    # --------------------------------------------------------

    (
        previous_candle,
        latest_candle
    ) = get_last_two_closed_candles()

    candle_id = int(
        latest_candle["T"]
    )

    candle_datetime = (
        datetime.fromtimestamp(
            candle_id / 1000,
            tz=timezone.utc
        )
    )

    log(
        f"ULTIMA 4H CHIUSA | "
        f"{candle_datetime}"
    )

    # --------------------------------------------------------
    # GIÀ ELABORATA
    # --------------------------------------------------------

    if (
        state["last_processed_candle"]
        == candle_id
    ):

        log(
            "Candela già elaborata. "
            "Nessuna operazione."
        )

        log_performance(
            state
        )

        return

    current_price = (
        get_mid_price()
    )

    log(
        f"BTC MID ${current_price:.2f}"
    )

    # --------------------------------------------------------
    # 1. SELL PRIMA DEL BUY
    # --------------------------------------------------------

    sold = check_sell(
        state,
        current_price
    )

    if sold:

        verify_position_consistency(
            state
        )

        # Se ha venduto,
        # non compra sulla stessa candela.
        state[
            "last_processed_candle"
        ] = candle_id

        save_state(
            state
        )

        log_performance(
            state
        )

        log(
            "CICLO TERMINATO DOPO SELL."
        )

        return

    # --------------------------------------------------------
    # 2. BUY
    # --------------------------------------------------------

    check_buy(
        state,
        previous_candle,
        latest_candle
    )

    # --------------------------------------------------------
    # 3. SICUREZZA FINALE
    # --------------------------------------------------------

    verify_position_consistency(
        state
    )

    state[
        "last_processed_candle"
    ] = candle_id

    save_state(
        state
    )

    # --------------------------------------------------------
    # PERFORMANCE
    # --------------------------------------------------------

    log_performance(
        state
    )

    log(
        "CICLO TERMINATO."
    )

    log(
        "=================================================="
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    try:

        if STARTUP_DELAY > 0:
            time.sleep(
                STARTUP_DELAY
            )

        run()

    except Exception as exc:

        log(
            f"ERRORE FATALE: {exc}"
        )

        log(
            "BOT BLOCCATO: nessun altro ordine "
            "verrà eseguito in questo ciclo."
        )

        sys.exit(1)
