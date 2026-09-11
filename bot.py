import json
import os
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

TOP_N_COINS = int(os.getenv("TOP_N_COINS", "25"))

BUY_USD = float(os.getenv("BUY_USD", "10"))
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "200"))
MAX_WEEKLY_BUYS = int(os.getenv("MAX_WEEKLY_BUYS", "10"))

DIP_PERCENT = float(os.getenv("DIP_PERCENT", "2"))
TAKE_PROFIT_PERCENT = float(
    os.getenv("TAKE_PROFIT_PERCENT", "4")
)

MAX_SLIPPAGE = float(os.getenv("MAX_SLIPPAGE", "0.01"))

STATE_DIR = os.getenv("STATE_DIR", "/data")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

STARTUP_DELAY = int(os.getenv("STARTUP_DELAY", "5"))
POST_ORDER_DELAY = int(os.getenv("POST_ORDER_DELAY", "3"))

FILL_CHECK_ATTEMPTS = int(
    os.getenv("FILL_CHECK_ATTEMPTS", "5")
)

FILL_CHECK_DELAY = float(
    os.getenv("FILL_CHECK_DELAY", "1")
)

POSITION_TOLERANCE = float(
    os.getenv("POSITION_TOLERANCE", "0.00000001")
)

CANDLE_INTERVAL = os.getenv(
    "CANDLE_INTERVAL",
    "15m"
)

LOOP_INTERVAL_SECONDS = int(
    os.getenv("LOOP_INTERVAL_SECONDS", "60")
)


# ============================================================
# VALIDAZIONE
# ============================================================

if not PRIVATE_KEY:
    raise RuntimeError(
        "HYPERLIQUID_PRIVATE_KEY mancante"
    )

if not ACCOUNT_ADDRESS:
    raise RuntimeError(
        "HYPERLIQUID_ACCOUNT_ADDRESS mancante"
    )


# ============================================================
# CONNESSIONE
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
# LOG
# ============================================================

def log(message):
    now = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")

    print(
        f"[{now}] {message}",
        flush=True
    )


# ============================================================
# METADATA SPOT
# ============================================================

def get_spot_metadata():
    return info.spot_meta_and_asset_ctxs()


def resolve_active_coins():
    meta, contexts = get_spot_metadata()

    tokens = meta.get("tokens", [])
    universe = meta.get("universe", [])

    usdc_index = None

    for token in tokens:
        if token.get("name") == "USDC":
            usdc_index = token.get("index")
            break

    if usdc_index is None:
        raise RuntimeError(
            "Token USDC non trovato nei metadata Spot."
        )

    candidates = []

    for index, market in enumerate(universe):
        market_tokens = market.get(
            "tokens",
            []
        )

        if len(market_tokens) != 2:
            continue

        base_index = market_tokens[0]
        quote_index = market_tokens[1]

        if quote_index != usdc_index:
            continue

        if index >= len(contexts):
            continue

        if not market.get(
            "isCanonical",
            False
        ):
            continue

        base_token = None

        for token in tokens:
            if token.get("index") == base_index:
                base_token = token
                break

        if base_token is None:
            continue

        base_name = base_token.get("name")

        if not base_name or base_name == "USDC":
            continue

        volume = float(
            contexts[index].get(
                "dayNtlVlm",
                0
            ) or 0
        )

        mid_price = contexts[index].get(
            "midPx"
        )

        if mid_price is None:
            continue

        symbol = market.get("name")

        if not symbol:
            continue

        candidates.append(
            {
                "symbol": symbol,
                "base": base_name,
                "decimals": int(
                    base_token.get(
                        "szDecimals",
                        0
                    )
                ),
                "volume": volume,
            }
        )

    candidates.sort(
        key=lambda item: item["volume"],
        reverse=True
    )

    selected = candidates[:TOP_N_COINS]

    if not selected:
        raise RuntimeError(
            "Nessuna coppia Spot/USDC disponibile."
        )

    markets = {}

    for item in selected:
        markets[item["symbol"]] = {
            "base": item["base"],
            "decimals": item["decimals"],
            "volume": item["volume"],
        }

    log(
        f"COIN ATTIVE: {len(markets)} | "
        f"{[item['base'] for item in selected]}"
    )

    return markets


MARKETS = resolve_active_coins()

ACTIVE_SYMBOLS = list(
    MARKETS.keys()
)


# ============================================================
# STATE
# ============================================================

def default_coin_state():
    return {
        "weekly_buys": 0,
        "next_lot_id": 1,
        "open_lots": [],
        "sell_trades": [],
        "last_processed_candle": None,
        "last_buy": None,
        "last_sell": None,
    }


def default_state():
    return {
        "version": 7,
        "week_id": None,
        "coins": {},
    }


def get_coin_state(
    state,
    symbol
):
    coins = state.setdefault(
        "coins",
        {}
    )

    if symbol not in coins:
        coins[symbol] = (
            default_coin_state()
        )

    return coins[symbol]


def load_state():
    os.makedirs(
        STATE_DIR,
        exist_ok=True
    )

    if not os.path.exists(
        STATE_FILE
    ):
        return default_state()

    with open(
        STATE_FILE,
        "r",
        encoding="utf-8"
    ) as file:
        state = json.load(file)

    version = int(
        state.get("version", 0)
    )

    if version < 7:
        raise RuntimeError(
            "STATE FILE OBSOLETO. "
            "Il file /data/state.json appartiene "
            "a una versione precedente del bot. "
            "Eliminalo solo se non esistono "
            "posizioni Spot acquistate dal bot."
        )

    state.setdefault(
        "coins",
        {}
    )

    state.setdefault(
        "week_id",
        None
    )

    return state


def save_state(state):
    os.makedirs(
        STATE_DIR,
        exist_ok=True
    )

    temp_file = (
        STATE_FILE + ".tmp"
    )

    with open(
        temp_file,
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            state,
            file,
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

def current_week_id():
    now = datetime.now(
        timezone.utc
    )

    iso = now.isocalendar()

    return (
        f"{iso.year}-W{iso.week:02d}"
    )


def refresh_week(state):
    week_id = current_week_id()

    if state.get(
        "week_id"
    ) != week_id:

        state["week_id"] = week_id

        for coin_state in state.get(
            "coins",
            {}
        ).values():

            coin_state[
                "weekly_buys"
            ] = 0

        save_state(state)


# ============================================================
# SALDI SPOT
# ============================================================

def get_balances_all():
    data = info.spot_user_state(
        ACCOUNT_ADDRESS
    )

    usdc_total = 0.0
    usdc_hold = 0.0

    bases = {}

    for balance in data.get(
        "balances",
        []
    ):
        coin = str(
            balance.get(
                "coin",
                ""
            )
        )

        total = float(
            balance.get(
                "total",
                0
            ) or 0
        )

        hold = float(
            balance.get(
                "hold",
                0
            ) or 0
        )

        if coin == "USDC":
            usdc_total = total
            usdc_hold = hold
        else:
            bases[coin] = {
                "total": total,
                "hold": hold,
            }

    return {
        "usdc_total": usdc_total,
        "usdc_available": max(
            0.0,
            usdc_total - usdc_hold
        ),
        "bases": bases,
    }


def get_coin_balance(
    balances,
    base_name
):
    balance = balances[
        "bases"
    ].get(
        base_name,
        {
            "total": 0.0,
            "hold": 0.0,
        }
    )

    return {
        "total": float(
            balance["total"]
        ),
        "available": max(
            0.0,
            float(
                balance["total"]
            )
            - float(
                balance["hold"]
            )
        ),
    }


# ============================================================
# PREZZI SPOT
# ============================================================

def get_prices_all():
    meta, contexts = (
        get_spot_metadata()
    )

    prices = {}

    for index, market in enumerate(
        meta.get(
            "universe",
            []
        )
    ):
        symbol = market.get(
            "name"
        )

        if (
            symbol not in MARKETS
            or index >= len(contexts)
        ):
            continue

        mid = contexts[index].get(
            "midPx"
        )

        if mid is not None:
            prices[symbol] = float(
                mid
            )

    return prices


# ============================================================
# PRECISIONE PREZZO SPOT
# ============================================================

def get_spot_order_price(
    symbol,
    is_buy,
    slippage
):
    coin = info.name_to_coin[
        symbol
    ]

    asset = info.coin_to_asset[
        coin
    ]

    decimals = info.asset_to_sz_decimals[
        asset
    ]

    book = info.l2_snapshot(
        symbol
    )

    levels = book.get(
        "levels",
        []
    )

    if len(levels) < 2:
        raise RuntimeError(
            f"Orderbook {symbol} non disponibile."
        )

    bids = levels[0]
    asks = levels[1]

    if is_buy:
        if not asks:
            raise RuntimeError(
                f"Ask {symbol} non disponibile."
            )

        reference_price = float(
            asks[0]["px"]
        )

        price = (
            reference_price
            * (1.0 + slippage)
        )

    else:
        if not bids:
            raise RuntimeError(
                f"Bid {symbol} non disponibile."
            )

        reference_price = float(
            bids[0]["px"]
        )

        price = (
            reference_price
            * (1.0 - slippage)
        )

    # Stessa logica di precisione usata
    # dall'SDK Hyperliquid per gli ordini Spot.
    precision = max(
        0,
        8 - decimals
    )

    return round(
        float(
            f"{price:.5g}"
        ),
        precision
    )


def round_size(
    symbol,
    size
):
    decimals = MARKETS[
        symbol
    ]["decimals"]

    return round(
        float(size),
        decimals
    )


# ============================================================
# ORDINE SPOT IOC
# ============================================================

def spot_order(
    symbol,
    is_buy,
    size
):
    size = round_size(
        symbol,
        size
    )

    if size <= 0:
        raise RuntimeError(
            f"{symbol}: size ordine non valida."
        )

    price = get_spot_order_price(
        symbol,
        is_buy,
        MAX_SLIPPAGE
    )

    log(
        f"ORDINE SPOT | "
        f"{symbol} | "
        f"{'BUY' if is_buy else 'SELL'} | "
        f"{size} | "
        f"limite IOC {price}"
    )

    result = exchange.order(
        symbol,
        is_buy,
        size,
        price,
        {
            "limit": {
                "tif": "Ioc"
            }
        }
    )

    if result.get(
        "status"
    ) != "ok":
        raise RuntimeError(
            f"Ordine {symbol} rifiutato: "
            f"{result}"
        )

    return result


# ============================================================
# LETTURA FILL DALLA RISPOSTA DELL'ORDINE
# ============================================================

def extract_order_fill(
    result
):
    try:
        statuses = (
            result[
                "response"
            ][
                "data"
            ][
                "statuses"
            ]
        )
    except (
        KeyError,
        TypeError
    ):
        return None

    for status in statuses:

        if "filled" in status:
            filled = status[
                "filled"
            ]

            size = float(
                filled.get(
                    "totalSz",
                    0
                )
            )

            price = float(
                filled.get(
                    "avgPx",
                    0
                )
            )

            if (
                size > 0
                and price > 0
            ):
                return {
                    "size": size,
                    "price": price,
                    "notional": (
                        size * price
                    ),
                    "oid": filled.get(
                        "oid"
                    ),
                }

        if "error" in status:
            raise RuntimeError(
                status["error"]
            )

    return None


# ============================================================
# FEE STIMATA
# ============================================================

def estimate_fee(
    notional
):
    try:
        fees = info.user_fees(
            ACCOUNT_ADDRESS
        )

        rate = float(
            fees.get(
                "userCrossRate",
                0
            ) or 0
        )

        return (
            float(notional)
            * rate
        )

    except Exception:
        return 0.0


# ============================================================
# CONTROLLO POSIZIONE
# ============================================================

def verify_position(
    state,
    symbol,
    balances
):
    coin_state = state[
        "coins"
    ][symbol]

    base = MARKETS[
        symbol
    ]["base"]

    balance = get_coin_balance(
        balances,
        base
    )

    real_size = balance[
        "total"
    ]

    local_size = sum(
        float(
            lot[
                "remaining_size"
            ]
        )
        for lot in coin_state[
            "open_lots"
        ]
    )

    difference = abs(
        real_size - local_size
    )

    if (
        difference
        > POSITION_TOLERANCE
    ):
        raise RuntimeError(
            f"HALT {symbol}: "
            f"saldo Spot reale="
            f"{real_size:.12f}, "
            f"lotti locali="
            f"{local_size:.12f}, "
            f"differenza="
            f"{difference:.12f}."
        )

    return balance


# ============================================================
# CANDELE
# ============================================================

def get_candle_interval_ms():
    mapping = {
        "1m": 60 * 1000,
        "3m": 3 * 60 * 1000,
        "5m": 5 * 60 * 1000,
        "15m": 15 * 60 * 1000,
        "30m": 30 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "2h": 2 * 60 * 60 * 1000,
        "4h": 4 * 60 * 60 * 1000,
        "8h": 8 * 60 * 60 * 1000,
        "12h": 12 * 60 * 60 * 1000,
        "1d": 24 * 60 * 60 * 1000,
    }

    if CANDLE_INTERVAL not in mapping:
        raise RuntimeError(
            f"Intervallo candela non supportato: "
            f"{CANDLE_INTERVAL}"
        )

    return mapping[
        CANDLE_INTERVAL
    ]


def get_closed_candles(
    symbol
):
    now_ms = int(
        time.time() * 1000
    )

    interval_ms = (
        get_candle_interval_ms()
    )

    start_ms = (
        now_ms
        - interval_ms * 10
    )

    candles = info.candles_snapshot(
        symbol,
        CANDLE_INTERVAL,
        start_ms,
        now_ms
    )

    closed = []

    for candle in candles:

        end_time = int(
            candle["T"]
        )

        if end_time <= now_ms:
            closed.append(
                candle
            )

    closed.sort(
        key=lambda item: int(
            item["T"]
        )
    )

    return closed


# ============================================================
# BUY
# ============================================================

def place_buy(
    state,
    symbol,
    current_price,
    balances
):
    coin_state = state[
        "coins"
    ][symbol]

    base = MARKETS[
        symbol
    ]["base"]

    balance = get_coin_balance(
        balances,
        base
    )

    current_position_value = (
        balance["total"]
        * current_price
    )

    if (
        current_position_value
        + BUY_USD
        > MAX_POSITION_USD
    ):
        log(
            f"BUY BLOCCATO {symbol} | "
            f"posizione ${current_position_value:.2f} | "
            f"BUY ${BUY_USD:.2f} | "
            f"MAX ${MAX_POSITION_USD:.2f}"
        )
        return False

    if (
        coin_state[
            "weekly_buys"
        ]
        >= MAX_WEEKLY_BUYS
    ):
        log(
            f"BUY BLOCCATO {symbol} | "
            f"limite settimanale "
            f"{MAX_WEEKLY_BUYS}"
        )
        return False

    if (
        balances[
            "usdc_available"
        ]
        < BUY_USD
    ):
        log(
            f"BUY BLOCCATO {symbol} | "
            f"USDC disponibili "
            f"${balances['usdc_available']:.4f}"
        )
        return False

    decimals = MARKETS[
        symbol
    ]["decimals"]

    buy_size = round(
        BUY_USD / current_price,
        decimals
    )

    if buy_size <= 0:
        return False

    result = spot_order(
        symbol,
        True,
        buy_size
    )

    if POST_ORDER_DELAY > 0:
        time.sleep(
            POST_ORDER_DELAY
        )

    fill = extract_order_fill(
        result
    )

    if fill is None:
        log(
            f"BUY {symbol}: "
            "ordine IOC senza fill."
        )
        return False

    actual_size = fill[
        "size"
    ]

    actual_price = fill[
        "price"
    ]

    actual_notional = fill[
        "notional"
    ]

    fee = estimate_fee(
        actual_notional
    )

    target_price = (
        actual_price
        * (
            1.0
            + TAKE_PROFIT_PERCENT
            / 100.0
        )
    )

    lot = {
        "id": coin_state[
            "next_lot_id"
        ],
        "buy_time": int(
            time.time() * 1000
        ),
        "buy_price": actual_price,
        "buy_size": actual_size,
        "remaining_size": actual_size,
        "buy_notional": actual_notional,
        "buy_fee": fee,
        "target_price": target_price,
    }

    coin_state[
        "next_lot_id"
    ] += 1

    coin_state[
        "open_lots"
    ].append(lot)

    coin_state[
        "weekly_buys"
    ] += 1

    coin_state[
        "last_buy"
    ] = lot

    save_state(state)

    log(
        f"BUY CONFERMATO | "
        f"{symbol} | "
        f"lotto #{lot['id']} | "
        f"{actual_size:.8f} | "
        f"${actual_price:.2f} | "
        f"investiti ${actual_notional:.4f} | "
        f"TP ${target_price:.2f}"
    )

    return True


# ============================================================
# LOTTI IN TP
# ============================================================

def get_sellable_lots(
    state,
    symbol,
    current_price
):
    coin_state = state[
        "coins"
    ][symbol]

    eligible = []

    for lot in sorted(
        coin_state[
            "open_lots"
        ],
        key=lambda item: item["id"]
    ):
        if (
            float(
                lot[
                    "remaining_size"
                ]
            )
            <= POSITION_TOLERANCE
        ):
            continue

        if (
            current_price
            >= float(
                lot[
                    "target_price"
                ]
            )
        ):
            eligible.append(
                lot
            )

    return eligible


# ============================================================
# SELL
# ============================================================

def sell_lot(
    state,
    symbol,
    lot,
    current_price,
    balances
):
    coin_state = state[
        "coins"
    ][symbol]

    base = MARKETS[
        symbol
    ]["base"]

    sell_size = round_size(
        symbol,
        lot[
            "remaining_size"
        ]
    )

    if sell_size <= 0:
        return False

    if (
        current_price
        < lot[
            "target_price"
        ]
    ):
        return False

    balance = get_coin_balance(
        balances,
        base
    )

    if (
        balance["available"]
        + POSITION_TOLERANCE
        < sell_size
    ):
        raise RuntimeError(
            f"{symbol}: saldo disponibile "
            "inferiore al lotto da vendere."
        )

    log(
        f"SELL SPOT | "
        f"{symbol} | "
        f"lotto #{lot['id']} | "
        f"{sell_size:.8f} | "
        f"target ${lot['target_price']:.2f}"
    )

    result = spot_order(
        symbol,
        False,
        sell_size
    )

    if POST_ORDER_DELAY > 0:
        time.sleep(
            POST_ORDER_DELAY
        )

    fill = extract_order_fill(
        result
    )

    if fill is None:
        log(
            f"SELL {symbol}: "
            "ordine IOC senza fill."
        )
        return False

    sold_size = min(
        fill["size"],
        lot[
            "remaining_size"
        ]
    )

    sell_price = fill[
        "price"
    ]

    sell_notional = (
        sold_size
        * sell_price
    )

    allocated_buy_cost = (
        lot["buy_price"]
        * sold_size
    )

    allocated_buy_fee = (
        lot["buy_fee"]
        * (
            sold_size
            / lot["buy_size"]
        )
    )

    sell_fee = estimate_fee(
        sell_notional
    )

    gross_pnl = (
        sell_notional
        - allocated_buy_cost
    )

    net_pnl = (
        gross_pnl
        - allocated_buy_fee
        - sell_fee
    )

    lot[
        "remaining_size"
    ] = max(
        0.0,
        lot[
            "remaining_size"
        ]
        - sold_size
    )

    trade = {
        "lot_id": lot["id"],
        "sell_time": int(
            time.time() * 1000
        ),
        "sell_size": sold_size,
        "sell_price": sell_price,
        "sell_notional": sell_notional,
        "buy_cost_allocated": (
            allocated_buy_cost
        ),
        "buy_fee_allocated": (
            allocated_buy_fee
        ),
        "sell_fee": sell_fee,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
    }

    coin_state[
        "sell_trades"
    ].append(trade)

    if (
        lot["remaining_size"]
        <= POSITION_TOLERANCE
    ):
        coin_state[
            "open_lots"
        ].remove(lot)

    coin_state[
        "last_sell"
    ] = trade

    save_state(state)

    log(
        f"SELL CONFERMATO | "
        f"{symbol} | "
        f"lotto #{lot['id']} | "
        f"{sold_size:.8f} | "
        f"${sell_price:.2f} | "
        f"PnL netto stimato "
        f"${net_pnl:.4f}"
    )

    return True


# ============================================================
# CONTROLLO SELL
# ============================================================

def check_sell(
    state,
    symbol,
    current_price,
    balances
):
    eligible = get_sellable_lots(
        state,
        symbol,
        current_price
    )

    if not eligible:
        return False

    # FIFO: un solo lotto per ciclo
    lot = eligible[0]

    return sell_lot(
        state,
        symbol,
        lot,
        current_price,
        balances
    )


# ============================================================
# CONTROLLO BUY
# ============================================================

def check_buy(
    state,
    symbol,
    latest_close,
    previous_close,
    current_price,
    balances
):
    if previous_close <= 0:
        return False

    change_percent = (
        (
            latest_close
            - previous_close
        )
        / previous_close
        * 100.0
    )

    if (
        change_percent
        <= -DIP_PERCENT
    ):
        log(
            f"DIP {symbol} | "
            f"{change_percent:.4f}% | "
            f"soglia "
            f"-{DIP_PERCENT:.2f}%"
        )

        return place_buy(
            state,
            symbol,
            current_price,
            balances
        )

    return False


# ============================================================
# PROCESSA UNA COIN
# ============================================================

def process_coin(
    state,
    symbol,
    prices,
    balances
):
    current_price = prices.get(
        symbol
    )

    if current_price is None:
        return

    coin_state = get_coin_state(
        state,
        symbol
    )

    # --------------------------------------------------------
    # SELL: controllato OGNI ciclo, indipendentemente
    # dal cambio della candela.
    # --------------------------------------------------------

    verify_position(
        state,
        symbol,
        balances
    )

    sold = check_sell(
        state,
        symbol,
        current_price,
        balances
    )

    if sold:
        # Dopo il SELL ricontrolliamo il saldo reale e aggiorniamo
        # il dizionario condiviso, cosi' le coin successive in questo
        # stesso ciclo vedono USDC disponibile aggiornato.
        fresh_balances = (
            get_balances_all()
        )

        balances.update(
            fresh_balances
        )

        verify_position(
            state,
            symbol,
            fresh_balances
        )

        save_state(state)

        # Un SELL chiude questo ciclo per la coin:
        # nessun BUY nello stesso ciclo.
        return

    # --------------------------------------------------------
    # BUY: solo una volta per candela chiusa.
    # --------------------------------------------------------

    candles = get_closed_candles(
        symbol
    )

    if len(candles) < 2:
        return

    previous = candles[-2]
    latest = candles[-1]

    previous_close = float(
        previous["c"]
    )

    latest_close = float(
        latest["c"]
    )

    candle_time = int(
        latest["T"]
    )

    if (
        coin_state[
            "last_processed_candle"
        ]
        == candle_time
    ):
        return

    bought = check_buy(
        state,
        symbol,
        latest_close,
        previous_close,
        current_price,
        balances
    )

    if bought:
        # Aggiorna il dizionario condiviso: le coin successive in
        # questo ciclo devono vedere l'USDC realmente rimasto.
        fresh_balances = (
            get_balances_all()
        )

        balances.update(
            fresh_balances
        )

        verify_position(
            state,
            symbol,
            fresh_balances
        )

    coin_state[
        "last_processed_candle"
    ] = candle_time

    save_state(state)


# ============================================================
# PORTAFOGLIO
# ============================================================

def log_portfolio(
    balances,
    prices
):
    total_value = (
        balances[
            "usdc_available"
        ]
    )

    positions = []

    for symbol in ACTIVE_SYMBOLS:

        base = MARKETS[
            symbol
        ]["base"]

        balance = get_coin_balance(
            balances,
            base
        )

        price = prices.get(
            symbol
        )

        if (
            price is None
            or balance["total"]
            <= POSITION_TOLERANCE
        ):
            continue

        value = (
            balance["total"]
            * price
        )

        total_value += value

        positions.append(
            f"{base} "
            f"{balance['total']:.8f} "
            f"(~${value:.2f})"
        )

    if positions:
        position_text = " | ".join(
            positions
        )
    else:
        position_text = (
            "nessuna posizione"
        )

    log(
        f"PORTAFOGLIO | "
        f"USDC ${balances['usdc_available']:.2f} | "
        f"{position_text} | "
        f"totale ~${total_value:.2f}"
    )


# ============================================================
# RUN
# ============================================================

def run():
    state = load_state()

    refresh_week(
        state
    )

    log("=" * 60)

    log(
        "HYPERLIQUID MULTI-COIN "
        "SPOT BOT"
    )

    log(
        f"COIN={len(ACTIVE_SYMBOLS)} | "
        f"BUY=${BUY_USD:.2f} | "
        f"DIP={DIP_PERCENT:.2f}% | "
        f"TP={TAKE_PROFIT_PERCENT:.2f}% | "
        f"MAX/COIN=${MAX_POSITION_USD:.2f} | "
        f"MAX BUY/SETT={MAX_WEEKLY_BUYS} | "
        f"CANDLE={CANDLE_INTERVAL}"
    )

    balances = get_balances_all()

    prices = get_prices_all()

    log_portfolio(
        balances,
        prices
    )

    for symbol in ACTIVE_SYMBOLS:

        try:
            process_coin(
                state,
                symbol,
                prices,
                balances
            )

        except Exception as error:
            log(
                f"ERRORE {symbol} | "
                f"{error}"
            )

            log(
                traceback.format_exc()
            )

    save_state(
        state
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    if STARTUP_DELAY > 0:
        time.sleep(
            STARTUP_DELAY
        )

    while True:

        try:
            run()

        except Exception as error:

            log(
                f"ERRORE FATALE | "
                f"{error}"
            )

            log(
                traceback.format_exc()
            )

        time.sleep(
            LOOP_INTERVAL_SECONDS
        )
