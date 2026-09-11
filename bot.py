importjson
importos
importsys
importtime
fromdatetimeimportdatetime,timezone

fromdotenvimportload_dotenv
frometh_accountimportAccount

fromhyperliquid.exchangeimportExchange
fromhyperliquid.infoimportInfo
fromhyperliquid.utilsimportconstants


#============================================================
#CONFIGURAZIONE
#============================================================

load_dotenv()

PRIVATE_KEY=os.getenv("HYPERLIQUID_PRIVATE_KEY")
ACCOUNT_ADDRESS=os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS")

#SPOTONLY
SPOT_COIN="BTC/USDC"

BUY_USD=float(os.getenv("BUY_USD","10"))
MAX_POSITION_USD=float(os.getenv("MAX_POSITION_USD","200"))
MAX_WEEKLY_BUYS=int(os.getenv("MAX_WEEKLY_BUYS","10"))

DIP_PERCENT=float(os.getenv("DIP_PERCENT","2"))
TAKE_PROFIT_PERCENT=float(
os.getenv("TAKE_PROFIT_PERCENT","4")
)

MAX_SLIPPAGE=float(
os.getenv("MAX_SLIPPAGE","0.01")
)

MAX_LOTS_TO_SELL_PER_RUN=int(
os.getenv("MAX_LOTS_TO_SELL_PER_RUN","1")
)

STATE_DIR=os.getenv("STATE_DIR","/data")
STATE_FILE=os.path.join(
STATE_DIR,
"state.json"
)

STARTUP_DELAY=int(
os.getenv("STARTUP_DELAY","5")
)

POST_ORDER_DELAY=int(
os.getenv("POST_ORDER_DELAY","3")
)

FILL_CHECK_ATTEMPTS=int(
os.getenv("FILL_CHECK_ATTEMPTS","5")
)

FILL_CHECK_DELAY=float(
os.getenv("FILL_CHECK_DELAY","1")
)

POSITION_TOLERANCE=float(
os.getenv("POSITION_TOLERANCE","0.00000001")
)


#============================================================
#VALIDAZIONE
#============================================================

ifnotPRIVATE_KEY:
raiseRuntimeError(
"HYPERLIQUID_PRIVATE_KEYmancante"
)

ifnotACCOUNT_ADDRESS:
raiseRuntimeError(
"HYPERLIQUID_ACCOUNT_ADDRESSmancante"
)


#============================================================
#CONNESSIONE
#============================================================

wallet=Account.from_key(
PRIVATE_KEY
)

info=Info(
constants.MAINNET_API_URL,
skip_ws=True
)

exchange=Exchange(
wallet,
constants.MAINNET_API_URL,
account_address=ACCOUNT_ADDRESS
)


#============================================================
#LOG
#============================================================

deflog(message):
now=datetime.now(
timezone.utc
).strftime(
"%Y-%m-%d%H:%M:%SUTC"
)

print(
f"[{now}]{message}",
flush=True
)


#============================================================
#STATE
#============================================================

defdefault_state():

return{
"version":5,

"performance_start_ms":int(
datetime.now(
timezone.utc
).timestamp()*1000
),

"last_processed_candle":None,

"week_id":None,
"weekly_buys":0,

"next_lot_id":1,

"open_lots":[],
"sell_trades":[],

"last_buy":None,
"last_sell":None
}


defload_state():

os.makedirs(
STATE_DIR,
exist_ok=True
)

ifnotos.path.exists(
STATE_FILE
):
returndefault_state()

withopen(
STATE_FILE,
"r"
)asf:

state=json.load(f)

base=default_state()
base.update(state)

returnbase


defsave_state(state):

os.makedirs(
STATE_DIR,
exist_ok=True
)

tmp_file=(
STATE_FILE+".tmp"
)

withopen(
tmp_file,
"w"
)asf:

json.dump(
state,
f,
indent=2
)

os.replace(
tmp_file,
STATE_FILE
)


#============================================================
#SETTIMANA
#============================================================

defcurrent_week_id():

now=datetime.now(
timezone.utc
)

iso=now.isocalendar()

return(
f"{iso.year}-W{iso.week:02d}"
)


defrefresh_week(state):

week_id=current_week_id()

ifstate.get("week_id")!=week_id:

state["week_id"]=week_id
state["weekly_buys"]=0

save_state(state)


#============================================================
#SPOTMETADATA
#============================================================

defget_spot_decimals():

meta=info.spot_meta()

fortokeninmeta["tokens"]:

iftoken["name"]=="BTC":

returnint(
token["szDecimals"]
)

raiseRuntimeError(
"BTCnontrovatoneimetadataSpot"
)


defround_btc(size):

decimals=get_spot_decimals()

returnround(
float(size),
decimals
)


#============================================================
#SALDISPOT
#============================================================

defget_spot_balances():

data=info.spot_user_state(
ACCOUNT_ADDRESS
)

usdc_total=0.0
usdc_hold=0.0

btc_total=0.0
btc_hold=0.0

forbalanceindata.get(
"balances",
[]
):

coin=balance.get(
"coin"
)

total=float(
balance.get(
"total",
0
)or0
)

hold=float(
balance.get(
"hold",
0
)or0
)

ifcoin=="USDC":

usdc_total=total
usdc_hold=hold

elifcoin=="BTC":

btc_total=total
btc_hold=hold

return{
"usdc_total":usdc_total,
"usdc_available":max(
0.0,
usdc_total-usdc_hold
),

"btc_total":btc_total,
"btc_available":max(
0.0,
btc_total-btc_hold
)
}


#============================================================
#PREZZOSPOT
#============================================================

defget_spot_price():

data=info.spot_meta_and_asset_ctxs()

meta=data[0]
contexts=data[1]

fori,marketinenumerate(
meta["universe"]
):

base_idx=market["tokens"][0]
quote_idx=market["tokens"][1]

base=meta["tokens"][base_idx]["name"]
quote=meta["tokens"][quote_idx]["name"]

if(
base=="BTC"
andquote=="USDC"
):

mid=contexts[i].get(
"midPx"
)

ifmidisNone:
raiseRuntimeError(
"PrezzoBTC/USDCnondisponibile"
)

returnfloat(mid)

raiseRuntimeError(
"MercatoBTC/USDCSpotnontrovato"
)


#============================================================
#ORDINESPOTAGGRESSIVOIOC
#============================================================

defget_spot_execution_price(
is_buy,
slippage
):

book=info.l2_snapshot(
SPOT_COIN
)

levels=book.get(
"levels",
[]
)

iflen(levels)<2:
raiseRuntimeError(
"OrderbookBTC/USDCnondisponibile"
)

bids=levels[0]
asks=levels[1]

ifis_buy:

ifnotasks:
raiseRuntimeError(
"AskBTC/USDCnondisponibile"
)

best_ask=float(
asks[0]["px"]
)

return(
best_ask
*(1+slippage)
)

else:

ifnotbids:
raiseRuntimeError(
"BidBTC/USDCnondisponibile"
)

best_bid=float(
bids[0]["px"]
)

return(
best_bid
*(1-slippage)
)


defspot_market_order(
is_buy,
size
):

price=get_spot_execution_price(
is_buy,
MAX_SLIPPAGE
)

#prezzoconprecisionesufficiente
price=float(
f"{price:.8f}"
)

log(
f"ORDINESPOT|"
f"{'BUY'ifis_buyelse'SELL'}|"
f"{size:.8f}BTC|"
f"limiteaggressivo${price:.2f}"
)

result=exchange.order(
SPOT_COIN,
is_buy,
size,
price,
{
"limit":{
"tif":"Ioc"
}
}
)

log(
f"ORDINESPOTRISPOSTA|{result}"
)

returnresult


#============================================================
#FILLSSPOT
#============================================================

defget_spot_fills_since(
start_ms
):

end_ms=int(
datetime.now(
timezone.utc
).timestamp()*1000
)

fills=info.user_fills_by_time(
ACCOUNT_ADDRESS,
start_ms,
end_ms
)

result=[]

forfillinfills:

coin=fill.get(
"coin"
)

ifcoinin(
SPOT_COIN,
"BTC/USDC"
):

result.append(fill)

returnresult


defcalculate_fill(
fills,
is_buy
):

selected=[]

forfillinfills:

side=fill.get(
"side"
)

ifis_buyandside=="B":
selected.append(fill)

elifnotis_buyandside=="A":
selected.append(fill)

ifnotselected:
returnNone

total_size=0.0
total_notional=0.0

forfillinselected:

size=abs(
float(
fill.get(
"sz",
0
)or0
)
)

price=float(
fill.get(
"px",
0
)or0
)

total_size+=size

total_notional+=(
size*price
)

iftotal_size<=0:
returnNone

return{
"size":total_size,

"price":(
total_notional
/total_size
),

"notional":total_notional
}


#============================================================
#FEESTIMATA
#============================================================

defestimate_fee(
notional
):

try:

fees=info.user_fees(
ACCOUNT_ADDRESS
)

#PerordineIOCaggressivo
rate=float(
fees.get(
"userCrossRate",
0
)or0
)

return(
notional*rate
)

exceptException:

return0.0


#============================================================
#CONTROLLOPOSIZIONESPOT
#============================================================

defverify_spot_position(
state
):

balances=get_spot_balances()

real_btc=balances[
"btc_total"
]

local_btc=sum(
float(
lot["remaining_size"]
)
forlotinstate[
"open_lots"
]
)

difference=abs(
real_btc-local_btc
)

log(
f"CONTROLLOSPOT|"
f"BTCreale{real_btc:.8f}|"
f"lotti{local_btc:.8f}|"
f"diff{difference:.8f}"
)

ifdifference>POSITION_TOLERANCE:

raiseRuntimeError(
"INCOERENZABTCSPOT:"
f"saldoreale={real_btc:.8f},"
f"lottilocali={local_btc:.8f}."
"BOTBLOCCATO."
)

returnbalances


#============================================================
#CANDELE4HSPOT
#============================================================

defget_closed_4h_candles():

now_ms=int(
datetime.now(
timezone.utc
).timestamp()*1000
)

start_ms=(
now_ms
-(
4
*60
*60
*1000
*10
)
)

candles=info.candles_snapshot(
SPOT_COIN,
"4h",
start_ms,
now_ms
)

closed=[]

forcandleincandles:

ifint(
candle["T"]
)<=now_ms:

closed.append(
candle
)

closed.sort(
key=lambdax:int(x["T"])
)

returnclosed


#============================================================
#BUYSPOT
#============================================================

defplace_buy(state):

balances=get_spot_balances()

current_btc=balances[
"btc_total"
]

current_price=get_spot_price()

current_position_value=(
current_btc
*current_price
)

if(
current_position_value
+BUY_USD
>MAX_POSITION_USD
):

log(
f"BUYBLOCCATO|"
f"BTCattuale${current_position_value:.2f}|"
f"BUY${BUY_USD:.2f}|"
f"MAX${MAX_POSITION_USD:.2f}"
)

returnFalse

if(
state["weekly_buys"]
>=MAX_WEEKLY_BUYS
):

log(
f"BUYBLOCCATO|"
f"limitesettimanale"
f"{MAX_WEEKLY_BUYS}"
)

returnFalse

if(
balances["usdc_available"]
<BUY_USD
):

log(
f"BUYBLOCCATO|"
f"USDCdisponibili"
f"${balances['usdc_available']:.4f}|"
f"necessari${BUY_USD:.2f}"
)

returnFalse

#sizeindicativa
buy_size=(
BUY_USD
/current_price
)

buy_size=round_btc(
buy_size
)

ifbuy_size<=0:

log(
"BUYBLOCCATO|sizeBTCnonvalida"
)

returnFalse

order_start_ms=int(
datetime.now(
timezone.utc
).timestamp()*1000
)

result=spot_market_order(
True,
buy_size
)

ifresult.get(
"status"
)!="ok":

raiseRuntimeError(
f"BUYSPOTrifiutato:{result}"
)

time.sleep(
POST_ORDER_DELAY
)

fill=None

for_inrange(
FILL_CHECK_ATTEMPTS
):

fills=get_spot_fills_since(
order_start_ms-2000
)

fill=calculate_fill(
fills,
True
)

iffill:
break

time.sleep(
FILL_CHECK_DELAY
)

ifnotfill:

raiseRuntimeError(
"BUYinviatomafillSpotnonrilevato."
)

actual_size=fill[
"size"
]

actual_price=fill[
"price"
]

actual_notional=fill[
"notional"
]

fee=estimate_fee(
actual_notional
)

target_price=(
actual_price
*(
1
+TAKE_PROFIT_PERCENT/100
)
)

lot={

"id":state[
"next_lot_id"
],

"buy_time":int(
datetime.now(
timezone.utc
).timestamp()*1000
),

"buy_price":actual_price,

"buy_size":actual_size,

"remaining_size":actual_size,

"buy_notional":actual_notional,

"buy_fee":fee,

"target_price":target_price
}

state[
"next_lot_id"
]+=1

state[
"open_lots"
].append(
lot
)

state[
"weekly_buys"
]+=1

state[
"last_buy"
]=lot

save_state(
state
)

log(
f"BUYSPOTCONFERMATO|"
f"lotto#{lot['id']}|"
f"{actual_size:.8f}BTC|"
f"prezzo${actual_price:.2f}|"
f"investiti${actual_notional:.4f}|"
f"TP${target_price:.2f}"
)

returnTrue


#============================================================
#LOTTIVENDIBILI
#============================================================

defget_sellable_lots(
state
):

current_price=get_spot_price()

eligible=[]

forlotinsorted(
state["open_lots"],
key=lambdax:x["id"]
):

if(
lot["remaining_size"]
<=POSITION_TOLERANCE
):
continue

if(
current_price
>=lot["target_price"]
):

eligible.append(
lot
)

returneligible


#============================================================
#SELLSPOT
#============================================================

defsell_lot(
state,
lot
):

sell_size=round_btc(
lot["remaining_size"]
)

ifsell_size<=0:
returnFalse

current_price=get_spot_price()

if(
current_price
<lot["target_price"]
):

returnFalse

balances=get_spot_balances()

if(
balances["btc_available"]
+POSITION_TOLERANCE
<sell_size
):

raiseRuntimeError(
"BTCSpotdisponibile"
"inferioreallottodavendere."
)

log(
f"SELLSPOT|"
f"lotto#{lot['id']}|"
f"{sell_size:.8f}BTC|"
f"prezzo${current_price:.2f}|"
f"target${lot['target_price']:.2f}"
)

order_start_ms=int(
datetime.now(
timezone.utc
).timestamp()*1000
)

result=spot_market_order(
False,
sell_size
)

ifresult.get(
"status"
)!="ok":

raiseRuntimeError(
f"SELLSPOTrifiutato:{result}"
)

time.sleep(
POST_ORDER_DELAY
)

fill=None

for_inrange(
FILL_CHECK_ATTEMPTS
):

fills=get_spot_fills_since(
order_start_ms-2000
)

fill=calculate_fill(
fills,
False
)

iffill:
break

time.sleep(
FILL_CHECK_DELAY
)

ifnotfill:

raiseRuntimeError(
"SELLinviatomafillSpot"
"nonrilevato."
)

sold_size=min(
fill["size"],
lot["remaining_size"]
)

sell_price=fill[
"price"
]

sell_notional=fill[
"notional"
]

allocated_buy_cost=(
lot["buy_price"]
*sold_size
)

allocated_buy_fee=(
lot["buy_fee"]
*(
sold_size
/lot["buy_size"]
)
)

sell_fee=estimate_fee(
sell_notional
)

gross_pnl=(
sell_notional
-allocated_buy_cost
)

net_pnl=(
gross_pnl
-allocated_buy_fee
-sell_fee
)

lot[
"remaining_size"
]=max(
0.0,
lot["remaining_size"]
-sold_size
)

trade={

"lot_id":lot["id"],

"sell_time":int(
datetime.now(
timezone.utc
).timestamp()*1000
),

"sell_size":sold_size,

"sell_price":sell_price,

"sell_notional":sell_notional,

"buy_cost_allocated":
allocated_buy_cost,

"buy_fee_allocated":
allocated_buy_fee,

"sell_fee":sell_fee,

"gross_pnl":gross_pnl,

"net_pnl":net_pnl
}

state[
"sell_trades"
].append(
trade
)

if(
lot["remaining_size"]
<=POSITION_TOLERANCE
):

state[
"open_lots"
].remove(
lot
)

state[
"last_sell"
]=trade

save_state(
state
)

log(
f"SELLSPOTCONFERMATO|"
f"lotto#{lot['id']}|"
f"{sold_size:.8f}BTC|"
f"prezzo${sell_price:.2f}|"
f"PnLnetto${net_pnl:.4f}"
)

returnTrue


#============================================================
#SELLCHECK
#============================================================

defcheck_sell(
state
):

eligible=get_sellable_lots(
state
)

ifnoteligible:
returnFalse

#FIFO
lot=eligible[0]

returnsell_lot(
state,
lot
)


#============================================================
#BUYCHECK
#============================================================

defcheck_buy(
state,
latest_close,
previous_close
):

ifprevious_close<=0:
returnFalse

change_percent=(
(
latest_close
-previous_close
)
/previous_close
)*100

log(
f"VARIAZIONE4HSPOT|"
f"{change_percent:.4f}%"
)

if(
change_percent
<=-DIP_PERCENT
):

log(
f"DIPSPOTRILEVATO|"
f"{change_percent:.4f}%<="
f"-{DIP_PERCENT:.2f}%"
)

returnplace_buy(
state
)

returnFalse


#============================================================
#PERFORMANCE
#============================================================

defcalculate_performance(
state
):

balances=get_spot_balances()

current_price=get_spot_price()

btc_value=(
balances["btc_total"]
*current_price
)

open_cost=0.0
open_size=0.0
open_buy_fees=0.0

forlotinstate[
"open_lots"
]:

size=float(
lot["remaining_size"]
)

open_size+=size

open_cost+=(
size
*float(
lot["buy_price"]
)
)

iflot["buy_size"]>0:

open_buy_fees+=(
float(
lot["buy_fee"]
)
*(
size
/float(
lot["buy_size"]
)
)
)

closed_buy_cost=sum(
float(
trade[
"buy_cost_allocated"
]
)
fortradeinstate[
"sell_trades"
]
)

historical_buy_cost=(
open_cost
+closed_buy_cost
)

sold_notional=sum(
float(
trade[
"sell_notional"
]
)
fortradeinstate[
"sell_trades"
]
)

realized_net=sum(
float(
trade["net_pnl"]
)
fortradeinstate[
"sell_trades"
]
)

unrealized_gross=(
btc_value
-open_cost
)

estimated_exit_fee=estimate_fee(
btc_value
)

total_net_pnl=(
realized_net
+unrealized_gross
-open_buy_fees
-estimated_exit_fee
)

ifhistorical_buy_cost>0:

return_percent=(
total_net_pnl
/historical_buy_cost
)*100

else:

return_percent=0.0

return{

"usdc_available":
balances[
"usdc_available"
],

"btc_size":
balances[
"btc_total"
],

"btc_value":
btc_value,

"open_cost":
open_cost,

"weighted_avg_price":(
open_cost/open_size
ifopen_size>0
else0.0
),

"sold_notional":
sold_notional,

"realized_net":
realized_net,

"unrealized_gross":
unrealized_gross,

"total_net_pnl":
total_net_pnl,

"return_percent":
return_percent
}


#============================================================
#LOGCAPITALE
#============================================================

deflog_capital():

balances=get_spot_balances()

price=get_spot_price()

btc_value=(
balances["btc_total"]
*price
)

log(
f"CAPITALESPOT|"
f"USDCdisponibile"
f"${balances['usdc_available']:.4f}|"
f"BTC"
f"{balances['btc_total']:.8f}"
f"(~${btc_value:.4f})"
)


#============================================================
#MAIN
#============================================================

defrun():

globalstate

state=load_state()

refresh_week(
state
)

time.sleep(
STARTUP_DELAY
)

log("="*50)

log(
"AVVIOBOTHYPERLIQUIDBTCSPOT"
)

log(
f"PARAMETRI|"
f"BUY${BUY_USD:.2f}|"
f"DIP{DIP_PERCENT:.2f}%|"
f"TP{TAKE_PROFIT_PERCENT:.2f}%|"
f"MAXBTC${MAX_POSITION_USD:.2f}|"
f"MAXBUYSETT{MAX_WEEKLY_BUYS}"
)

log_capital()

#--------------------------------------------------------
#CONTROLLOSALDOBTC
#--------------------------------------------------------

verify_spot_position(
state
)

#--------------------------------------------------------
#CANDELE4HSPOT
#--------------------------------------------------------

candles=get_closed_4h_candles()

iflen(candles)<2:

log(
"Noncisonoabbastanza"
"candele4HSpot."
)

return

latest=candles[-1]
previous=candles[-2]

latest_candle_time=int(
latest["T"]
)

latest_close=float(
latest["c"]
)

previous_close=float(
previous["c"]
)

latest_datetime=(
datetime.fromtimestamp(
latest_candle_time/1000,
tz=timezone.utc
)
)

log(
f"ULTIMA4HSPOTCHIUSA|"
f"{latest_datetime}"
)

#--------------------------------------------------------
#EVITADOPPIAELABORAZIONE
#--------------------------------------------------------

if(
state.get(
"last_processed_candle"
)
==latest_candle_time
):

log(
"Candelagiàelaborata."
"Nessunaoperazione."
)

performance=(
calculate_performance(
state
)
)

log(
f"PERFORMANCESPOT|"
f"USDCdisponibile"
f"${performance['usdc_available']:.4f}|"
f"BTC"
f"{performance['btc_size']:.8f}|"
f"valoreBTC"
f"${performance['btc_value']:.4f}|"
f"investito"
f"${performance['open_cost']:.4f}|"
f"venduto"
f"${performance['sold_notional']:.4f}|"
f"mediaacquisto"
f"${performance['weighted_avg_price']:.2f}|"
f"realizzato"
f"${performance['realized_net']:.4f}|"
f"unrealizzato"
f"${performance['unrealized_gross']:.4f}|"
f"PnLtotale"
f"${performance['total_net_pnl']:.4f}|"
f"rendimento"
f"{performance['return_percent']:.2f}%"
)

return

#--------------------------------------------------------
#SELLPRIMADELBUY
#--------------------------------------------------------

sold=check_sell(
state
)

ifsold:

state[
"last_processed_candle"
]=latest_candle_time

save_state(
state
)

log(
"SELLSPOTeseguito:"
"nessunBUYsullastessacandela."
)

log_capital()

return

#--------------------------------------------------------
#BUY
#--------------------------------------------------------

bought=check_buy(
state,
latest_close,
previous_close
)

ifbought:

log(
"BUYSPOTeseguito."
)

else:

log(
"NessunBUYSPOT."
)

#--------------------------------------------------------
#CONTROLLOFINALE
#--------------------------------------------------------

verify_spot_position(
state
)

state[
"last_processed_candle"
]=latest_candle_time

save_state(
state
)

log_capital()

performance=(
calculate_performance(
state
)
)

log(
f"PERFORMANCESPOT|"
f"USDCdisponibile"
f"${performance['usdc_available']:.4f}|"
f"BTC"
f"{performance['btc_size']:.8f}|"
f"valoreBTC"
f"${performance['btc_value']:.4f}|"
f"investito"
f"${performance['open_cost']:.4f}|"
f"venduto"
f"${performance['sold_notional']:.4f}|"
f"mediaacquisto"
f"${performance['weighted_avg_price']:.2f}|"
f"realizzato"
f"${performance['realized_net']:.4f}|"
f"unrealizzato"
f"${performance['unrealized_gross']:.4f}|"
f"PnLtotale"
f"${performance['total_net_pnl']:.4f}|"
f"rendimento"
f"{performance['return_percent']:.2f}%"
)


#============================================================
#START
#============================================================

if__name__=="__main__":

try:

run()

exceptExceptionase:

log(
f"ERROREFATALE|{e}"
)

sys.exit(1)
