"""Static stock → NSE-sector-index mapping.

Why hardcoded instead of yfinance.info:
  Yahoo's /quoteSummary endpoint is aggressively rate-limited from cloud
  IPs — GitHub Actions runners get HTTP 401 "Invalid Crumb" on nearly
  every request. yfinance.info worked on the local dev box but was near-
  useless in production. A static dict maps stocks to their sector via
  the OFFICIAL NSE constituent lists (audited 2026-07-19) with zero
  external calls and zero flakiness.

Coverage: any stock NOT in HARDCODED_MAP gets "OTHER" and skips the
sector block on its alerts. Adding a stock is a one-line PR.

Overlap rule: when a stock appears in multiple NSE sector indices
(only Pharma ∩ Healthcare, 16 stocks), we map to the more specific
sector — pharma-only for the 16 dual-listed stocks; only pure hospital/
services names (Apollo, Max, Fortis, Syngene) go under Healthcare.

Sector-index tickers used (all verified to return ≥ 5y of daily data
on Yahoo):
  ^NSEBANK               Nifty Bank
  NIFTY_FIN_SERVICE.NS   Nifty Financial Services (^CNXFIN was broken)
  ^CNXIT                 Nifty IT
  ^CNXAUTO               Nifty Auto
  ^CNXPHARMA             Nifty Pharma
  HEALTHY.NS             ABSL Nifty Healthcare ETF (index itself broken)
  ^CNXFMCG               Nifty FMCG
  ^CNXCONSUM             Nifty Consumption (proxy for Consumer Durables —
                          NIFTY_CONSR_DURBL.NS returns 1 bar on Yahoo)
  ^CNXMETAL              Nifty Metal
  ^CNXENERGY             Nifty Energy (now correctly includes power CGs)
  ^CNXREALTY             Nifty Realty
  ^CNXMEDIA              Nifty Media
  ^CNXINFRA              Nifty Infrastructure
"""
from __future__ import annotations

# ─── SECTOR INDEX LABELS ────────────────────────────────────────────────
SECTOR_LABEL: dict[str, str] = {
    "^NSEBANK":              "NIFTY BANK",
    "NIFTY_FIN_SERVICE.NS":  "NIFTY FIN SERV",
    "^CNXIT":                "NIFTY IT",
    "^CNXAUTO":              "NIFTY AUTO",
    "^CNXPHARMA":            "NIFTY PHARMA",
    "HEALTHY.NS":            "NIFTY HEALTHCARE",
    "^CNXFMCG":              "NIFTY FMCG",
    "^CNXCONSUM":            "NIFTY CONSUMPTION",
    "^CNXMETAL":             "NIFTY METAL",
    "^CNXENERGY":            "NIFTY ENERGY",
    "^CNXREALTY":            "NIFTY REALTY",
    "^CNXMEDIA":             "NIFTY MEDIA",
    "^CNXINFRA":             "NIFTY INFRA",
    "OTHER":                 "OTHER",
}


# ─── HARDCODED STOCK → SECTOR-INDEX MAP ─────────────────────────────────
# Built from the official NSE sector constituent lists (screenshots
# 2026-07-19). Overlaps resolved as documented above.
_BANK  = "^NSEBANK"
_FIN   = "NIFTY_FIN_SERVICE.NS"
_IT    = "^CNXIT"
_AUTO  = "^CNXAUTO"
_PHM   = "^CNXPHARMA"
_HLTH  = "HEALTHY.NS"          # Healthcare (via ETF proxy)
_FMCG  = "^CNXFMCG"
_CDUR  = "^CNXCONSUM"          # Consumer Durables (via Consumption index)
_MET   = "^CNXMETAL"
_ENR   = "^CNXENERGY"
_RLT   = "^CNXREALTY"
_MED   = "^CNXMEDIA"
_INF   = "^CNXINFRA"

HARDCODED_MAP: dict[str, str] = {
    # ── NIFTY BANK (NSE official) ─────────────────────────────────────
    "HDFCBANK": _BANK, "ICICIBANK": _BANK, "SBIN": _BANK,
    "AXISBANK": _BANK, "KOTAKBANK": _BANK, "UNIONBANK": _BANK,
    "BANKBARODA": _BANK, "PNB": _BANK, "CANBK": _BANK,
    "FEDERALBNK": _BANK, "INDUSINDBK": _BANK, "AUBANK": _BANK,
    "YESBANK": _BANK, "IDFCFIRSTB": _BANK,
    # Additional NSE-listed banks not in Nifty Bank but still bank stocks
    "BANDHANBNK": _BANK, "IDBI": _BANK, "IOB": _BANK, "UCOBANK": _BANK,
    "CENTRALBK": _BANK, "MAHABANK": _BANK, "INDIANB": _BANK,
    "RBLBANK": _BANK, "SOUTHBANK": _BANK, "KARURVYSYA": _BANK,
    "CITYUNIONBK": _BANK, "DCBBANK": _BANK, "J&KBANK": _BANK,
    "CSBBANK": _BANK, "TMB": _BANK, "KTKBANK": _BANK, "JSFB": _BANK,
    "BANKINDIA": _BANK, "SBFC": _BANK, "EQUITASBNK": _BANK,
    "UJJIVANSFB": _BANK, "ESAFSFB": _BANK, "SURYODAY": _BANK,
    "UTKARSHBNK": _BANK,

    # ── NIFTY IT (NSE official) ───────────────────────────────────────
    "TCS": _IT, "INFY": _IT, "HCLTECH": _IT, "WIPRO": _IT,
    "TECHM": _IT, "LTIM": _IT, "OFSS": _IT, "PERSISTENT": _IT,
    "COFORGE": _IT, "MPHASIS": _IT,
    # Additional IT-services stocks
    "LTTS": _IT, "TATAELXSI": _IT, "KPITTECH": _IT, "HAPPSTMNDS": _IT,
    "INTELLECT": _IT, "ZENSARTECH": _IT, "BIRLASOFT": _IT, "CYIENT": _IT,
    "SONATSOFTW": _IT, "NEWGEN": _IT, "RATEGAIN": _IT, "TATATECH": _IT,
    "ROUTE": _IT, "MASTEK": _IT, "BSOFT": _IT, "NETWEB": _IT,
    "INOXINDIA": _IT, "TANLA": _IT, "IKS": _IT, "AFFLE": _IT,
    "STLTECH": _IT, "IZMO": _IT, "SASKEN": _IT, "DATAMATICS": _IT,
    "ECLERX": _IT, "RSYSTEMS": _IT, "DATAPATTNS": _IT, "FSL": _IT,
    "LATENTVIEW": _IT, "MAPMYINDIA": _IT, "MOBIKWIK": _IT,
    "HEXT": _IT, "MEDIASSIST": _IT, "ONESOURCE": _PHM,

    # ── NIFTY AUTO (NSE official + tail) ──────────────────────────────
    "MARUTI": _AUTO, "M&M": _AUTO, "BAJAJ-AUTO": _AUTO,
    "EICHERMOT": _AUTO, "TVSMOTOR": _AUTO, "MOTHERSON": _AUTO,
    "TMPV": _AUTO, "BOSCHLTD": _AUTO, "BHARATFORG": _AUTO,
    "HEROMOTOCO": _AUTO, "ASHOKLEY": _AUTO, "UNOMINDA": _AUTO,
    "TIINDIA": _AUTO, "SONACOMS": _AUTO, "EXIDEIND": _AUTO,
    # Additional auto-adjacent stocks
    "TATAMOTORS": _AUTO, "TMCV": _AUTO, "MRF": _AUTO,
    "BALKRISIND": _AUTO, "ENDURANCE": _AUTO, "APOLLOTYRE": _AUTO,
    "CEATLTD": _AUTO, "SUNDRMFAST": _AUTO, "ESCORTS": _AUTO,
    "SCHAEFFLER": _AUTO, "MINDACORP": _AUTO, "SANDHAR": _AUTO,
    "JBMA": _AUTO, "GABRIEL": _AUTO, "TALBROAUTO": _AUTO,
    "SHARDAMOTR": _AUTO, "RICOAUTO": _AUTO, "SUBROS": _AUTO,
    "JAMNAAUTO": _AUTO, "LUMAXTECH": _AUTO, "LUMAXIND": _AUTO,
    "SUPRAJIT": _AUTO, "PRICOLLTD": _AUTO, "MSUMI": _AUTO,
    "SANSERA": _AUTO, "SHRIRAMPPS": _AUTO, "VARROC": _AUTO,
    "JAINREC": _MET, "IGARASHI": _AUTO, "BAJEL": _AUTO,
    # AMBER moved to Consumer Durables (_CDUR) per NSE.       # was AUTO
                        # in Consumer Durables per NSE list — keep in AUTO
                        # since Amber is auto electronics
    # NOTE: Actually NSE lists AMBER in Consumer Durables — moved below.

    # ── NIFTY PHARMA (NSE official — 16 dual-listed + 3 pharma-only) ──
    # These 16 stocks are ALSO in Nifty Healthcare per NSE, but per the
    # user-signed rule we map them to the more specific Pharma benchmark.
    "SUNPHARMA": _PHM, "DIVISLAB": _PHM, "TORNTPHARM": _PHM,
    "ZYDUSLIFE": _PHM, "CIPLA": _PHM, "LUPIN": _PHM,
    "MANKIND": _PHM, "DRREDDY": _PHM, "AUROPHARMA": _PHM,
    "LAURUSLABS": _PHM, "BIOCON": _PHM, "ALKEM": _PHM,
    "GLENMARK": _PHM, "ABBOTINDIA": _PHM, "IPCALAB": _PHM,
    "PPLPHARMA": _PHM,
    # Pharma-only (not in Healthcare list)
    "AJANTPHARM": _PHM, "GLAND": _PHM, "WOCKPHARMA": _PHM,
    # Additional pharma-adjacent tail
    "SANOFI": _PHM, "PFIZER": _PHM, "NATCOPHARM": _PHM,
    "JBCHEPHARM": _PHM, "GRANULES": _PHM, "SAILIFE": _PHM,
    "CAPLIPOINT": _PHM, "ERIS": _PHM, "SEQUENT": _PHM,
    "STRIDES": _PHM, "SUPRIYA": _PHM, "SOLARA": _PHM,
    "INDOCO": _PHM, "SUVEN": _PHM, "SUVENPHAR": _PHM,
    "SHILPAMED": _PHM, "AARTIPHARM": _PHM, "AARTIDRUGS": _PHM,
    "PANACEABIO": _PHM, "CONCORDBIO": _PHM, "AKUMS": _PHM,
    "EMCURE": _PHM, "ANTHEM": _PHM, "AJMERA": _RLT,
    "JUBLPHARMA": _PHM, "COHANCE": _PHM, "NEULANDLAB": _PHM,
    "ANTELOPUS": _PHM, "MEDPLUS": _PHM, "ALIVUS": _PHM,
    "MOREPENLAB": _PHM, "ORCHPHARMA": _PHM, "SMSPHARMA": _PHM,
    "MARKSANS": _PHM, "GUFICBIO": _PHM, "BLISSGVS": _PHM,
    "VIYASH": _PHM, "SAGILITY": _IT, "IFBIND": _CDUR,
    "INDSWFTLAB": _PHM, "THEMISMED": _PHM, "VIMTALABS": _PHM,
    "ONESOURCE_PHARM": _PHM,

    # ── NIFTY HEALTHCARE (hospitals + services, per NSE) ──────────────
    "APOLLOHOSP": _HLTH, "MAXHEALTH": _HLTH, "FORTIS": _HLTH,
    "SYNGENE": _HLTH,
    # Additional hospital / diagnostics stocks (broader NSE health names)
    "MEDANTA": _HLTH, "NARAYANHRLR": _HLTH, "KIMS": _HLTH,
    "RAINBOW": _HLTH, "GLOBALHITECH": _HLTH, "ARTEMISMED": _HLTH,
    "ASTERDM": _HLTH, "HCG": _HLTH, "KRSNAA": _HLTH,
    "LALPATHLAB": _HLTH, "METROPOLIS": _HLTH, "THYROCARE": _HLTH,
    "VIJAYA": _HLTH, "NH": _HLTH, "YATHARTH": _HLTH,
    "INDRAMEDCO": _HLTH, "PGHL": _HLTH, "POLYMED": _HLTH,
    "GLAXO": _HLTH, "ASTRAZEN": _HLTH,
    "BAJAJHCARE": _HLTH,

    # ── NIFTY FMCG (NSE official — 15 stocks) ────────────────────────
    "HINDUNILVR": _FMCG, "ITC": _FMCG, "NESTLEIND": _FMCG,
    "VBL": _FMCG, "BRITANNIA": _FMCG, "GODREJCP": _FMCG,
    "MARICO": _FMCG, "TATACONSUM": _FMCG, "MCDOWELL-N": _FMCG,
    "DABUR": _FMCG, "COLPAL": _FMCG, "RADICO": _FMCG,
    "PATANJALI": _FMCG, "UBL": _FMCG, "EMAMILTD": _FMCG,
    # Additional FMCG-adjacent stocks
    "PGHH": _FMCG, "GILLETTE": _FMCG, "GODFRYPHLP": _FMCG,
    "AWL": _FMCG, "JUBLFOOD": _CDUR, "VARUN": _FMCG,
    "WESTLIFE": _FMCG, "HATSUN": _FMCG, "BAJAJCON": _FMCG,
    "JYOTHYLAB": _FMCG, "HERITGFOOD": _FMCG, "GOKULAGRO": _FMCG,
    "AVANTIFEED": _FMCG, "DODLA": _FMCG, "PARAGMILK": _FMCG,
    "VADILALIND": _FMCG, "BECTORFOOD": _FMCG, "SAPPHIRE": _CDUR,
    "DEVYANI": _CDUR, "BIKAJI": _FMCG, "LTFOODS": _FMCG,
    "KRBL": _FMCG, "GOKEX": "OTHER", "CCL": _FMCG, "TATVA": "OTHER",
    "TATACOMM": _INF, "DOMS": _FMCG, "GAEL": _FMCG,
    "SDBL": _FMCG, "GLOBUSSPR": _FMCG, "EPL": _FMCG,
    "HONASA": _FMCG, "NYKAA": _FMCG, "MANORAMA": _FMCG,

    # ── NIFTY CONSUMER DURABLES (NSE official, via Consumption proxy) ─
    "TITAN": _CDUR, "LGEINDIA": _CDUR, "DIXON": _CDUR,
    "HAVELLS": _CDUR, "KALYANKJIL": _CDUR, "VOLTAS": _CDUR,
    "BLUESTARCO": _CDUR, "AMBER": _CDUR, "KAJARIACER": _CDUR,
    "PGEL": _CDUR, "CROMPTON": _CDUR, "WHIRLPOOL": _CDUR,
    "BATAINDIA": _CDUR,
    # Additional consumer-durable tail (jewelry, appliances, furniture)
    "SENCO": _CDUR, "TBZ": _CDUR, "GOLDIAM": _CDUR,
    "CERA": _CDUR, "ASIANTILES": _CDUR, "GREENLAM": _CDUR,
    "GREENPLY": _CDUR, "CENTURYPLY": _CDUR, "STYLAMIND": _CDUR,
    "SYMPHONY": _CDUR, "TTKPRESTIGE": _CDUR, "BLUEDART": _INF,
    "VGUARD": _CDUR, "FINCABLES": _CDUR, "POLYCAB": _CDUR,
    "KEI": _CDUR, "APOLLOPIPE": _CDUR, "PRINCEPIPE": _CDUR,
    "FINPIPE": _CDUR, "ASTRAL": _CDUR, "SUPREMEIND": _CDUR,
    "TIPS": _CDUR, "RELAXO": _CDUR, "CAMPUS": _CDUR,
    "VIP": _CDUR, "SAFARI": _CDUR, "REDTAPE": _CDUR,
    "METROBRAND": _CDUR, "SIRCA": _CDUR,

    # ── NIFTY METAL (NSE official — 15 stocks) ────────────────────────
    "ADANIENT": _MET, "JSWSTEEL": _MET, "TATASTEEL": _MET,
    "HINDZINC": _MET, "HINDALCO": _MET, "LLOYDSME": _MET,
    "JINDALSTEL": _MET, "VEDL": _MET, "NMDC": _MET,
    "SAIL": _MET, "NATIONALUM": _MET, "JSL": _MET,
    "APLAPOLLO": _MET, "HINDCOPPER": _MET, "WELCORP": _MET,
    # Additional metal-adjacent stocks
    "RATNAMANI": _MET, "JINDALSAW": _MET, "MOIL": _MET,
    "GMDCLTD": _MET, "GRAVITA": _MET, "SANDUMA": _MET,
    "MAHSEAMLES": _MET, "SHYAMMETL": _MET, "SURYAROSNI": _MET,
    "RHIM": _MET, "ELECTCAST": _MET, "PENIND": _MET,
    "KIOCL": _MET, "MMTC": _MET, "GPIL": _MET,
    "GRAPHITE": _MET, "HEG": _MET, "JAIBALAJI": _MET,
    "GALLANTT": _MET, "GOODLUCK": _MET, "ELLEN": _MET,
    "TATAINVEST": _FIN,   # Tata Investment — capital allocated to metals holding
    "MSPL": _MET,

    # ── NIFTY ENERGY (NSE official — now includes power CGs correctly) ─
    "RELIANCE": _ENR, "ADANIPOWER": _ENR, "NTPC": _ENR,
    "ONGC": _ENR, "POWERGRID": _ENR, "COALINDIA": _ENR,
    "ADANIGREEN": _ENR, "ADANIENSOL": _ENR, "IOC": _ENR,
    "ABB": _ENR, "BHEL": _ENR, "HITACHIENERGY": _ENR,
    "CGPOWER": _ENR, "BPCL": _ENR, "SIEMENS": _ENR,
    "TATAPOWER": _ENR, "GAIL": _ENR, "GVT&D": _ENR,
    "JSWENERGY": _ENR, "HINDPETRO": _ENR, "NHPC": _ENR,
    "ATGL": _ENR, "NTPCGREEN": _ENR, "SUZLON": _ENR,
    # Additional energy-adjacent stocks
    "OIL": _ENR, "MGL": _ENR, "IGL": _ENR, "GUJENERGY": _ENR,  # was GUJGASLTD (NSE rename 2026-07-01)
    "AEGISLOG": _ENR, "PETRONET": _ENR, "GSPL": _ENR,
    "SJVN": _ENR, "INOXWIND": _ENR, "TORNTPOWER": _ENR,
    "CESC": _ENR, "MRPL": _ENR, "CHENNPETRO": _ENR,
    "PFC": _ENR, "RECLTD": _ENR, "IREDA": _FIN,
    "POWERINDIA": _ENR, "POWERMECH": _ENR, "SIEMENSENER": _ENR,
    "THERMAX": _ENR, "CUMMINSIND": _ENR, "TRITURBINE": _ENR,
    "TDPOWERSYS": _ENR, "KIRLOSBROS": _ENR, "KIRLOSENG": _ENR,
    "KIRLOSIND": _ENR, "ENGINERSIN": _ENR, "ENRIN": _ENR,
    "AEGISVOPAK": _ENR, "GANDHAR": _ENR, "PANAMAPET": _ENR,
    "CASTROLIND": _ENR, "IRMENERGY": _ENR, "PREMIERENE": _ENR,
    "SERVOTECH": _ENR, "SHAKTIPUMP": _ENR, "KPIGREEN": _ENR,
    "WAAREEENER": _ENR, "WAAREERTL": _ENR, "WEBELSOLAR": _ENR,
    "ACMESOLAR": _ENR, "OSWALPUMPS": _ENR, "GIPCL": _ENR,
    "JPPOWER": _ENR, "NLCINDIA": _ENR, "QPOWER": _ENR,
    "RPOWER": _ENR, "SPLPETRO": _ENR, "REFEX": _ENR,
    "EMMVEE": _ENR, "NAVA": _ENR, "GENUSPOWER": _ENR,
    "RIIL": _ENR, "SWANCORP": _ENR, "GNFC": _ENR,
    "PPL": _ENR, "PREMEXPLN": _ENR, "GVPIL": _ENR,
    "PARADEEP": _ENR, "AZAD": _ENR, "SOLARINDS": _ENR,

    # ── NIFTY REALTY (NSE official — 10 stocks) ──────────────────────
    "DLF": _RLT, "LODHA": _RLT, "PHOENIXLTD": _RLT,
    "PRESTIGE": _RLT, "OBEROIRLTY": _RLT, "GODREJPROP": _RLT,
    "ANANTRAJ": _RLT, "BRIGADE": _RLT, "ABREL": _RLT, "SOBHA": _RLT,
    # Additional realty tail
    "MAHLIFE": _RLT, "SUNTECK": _RLT, "IBREALEST": _RLT,
    "SIGNATURE": _RLT, "RAYMOND": _RLT, "RAYMONDLSL": _RLT,
    "KOLTEPATIL": _RLT, "ARVSMART": _RLT, "PURVA": _RLT,
    "DBREALTY": _RLT, "ATALREAL": _RLT, "SAMHI": _RLT,
    "HEMIPROP": _RLT, "LOTUSDEV": _RLT,
    "INDIASHLTR": _FIN,

    # ── NIFTY MEDIA (NSE official — 9 stocks + tail) ──────────────────
    "SUNTV": _MED, "NAZARA": _MED, "ZEEL": _MED,
    "PVRINOX": _MED, "SAREGAMA": _MED, "TIPSMUSIC": _MED,
    "NETWORK18": _MED, "DBCORP": _MED, "HATHWAY": _MED, "PFOCUS": _MED,
    # Additional media tail
    "SHEMAROO": _MED, "DEN": _MED, "TIPS": _MED,      # dupe key handled: last wins
    "IMAGICAA": _MED,
    "DELTACORP": _MED, "PPL_MEDIA": _MED, "BALAJITELE": _MED,

    # ── NIFTY INFRA (Nifty Infrastructure) ──────────────────────────
    # Infra-specific stocks that DIDN'T move to Energy
    "LT": _INF, "ADANIPORTS": _INF, "HAL": _INF, "BEL": _INF,
    "GRASIM": _INF, "ULTRACEMCO": _INF, "SHREECEM": _INF,
    "AMBUJACEM": _INF, "DALBHARAT": _INF, "GMRINFRA": _INF,
    "GMRAIRPORT": _INF, "IRB": _INF, "KEC": _INF, "KNRCON": _INF,
    "RVNL": _INF, "IRFC": _FIN, "RITES": _INF, "NBCC": _INF,
    "TITAGARH": _INF, "ACC": _INF, "JKCEMENT": _INF,
    "RAMCOCEM": _INF, "HEIDELBERG": _INF, "PRSMJOHNSN": _INF,
    "STARCEMENT": _INF, "IRCON": _INF, "GRINFRA": _INF,
    "PNCINFRA": _INF, "NCC": _INF, "HGINFRA": _INF, "JKIL": _INF,
    "TIMKEN": _INF, "SKFINDIA": _INF,
    "MAZDOCK": _INF, "COCHINSHIP": _INF, "BEML": _INF,
    "GESHIP": _INF, "SCI": _INF, "CONCOR": _INF, "GRSE": _INF,
    "BDL": _INF, "MIDHANI": _INF, "AFCONS": _INF,
    "TARIL": _INF, "BLUEDART_INF": _INF, "TCIEXP": _INF,
    "DELHIVERY": _INF, "MAHLOG": _INF, "TVSSCS": _INF,
    "INDIGO": _INF, "SPICEJET": _INF, "INDHOTEL": _INF,
    "EIHOTEL": _INF, "LEMONTREE": _INF, "CHALET": _INF,
    "ITCHOTELS": _INF, "THELEELA": _INF, "INDIAMART": _INF,
    "JUSTDIAL": _INF, "KFINTECH": _FIN, "CAMS": _INF,
    "NAUKRI": _INF, "SWIGGY": _INF, "MEESHO": _INF,
    "ETERNAL": _INF, "GROWW": _FIN, "PAYTM": _FIN,
    "POLICYBZR": _FIN, "NUVAMA": _FIN, "PINELABS": _FIN,
    "TRANSRAILL": _INF, "IRCTC": _INF, "RAILTEL": _INF,
    "PSPPROJECT": _INF, "ELECON": _INF, "ELGIEQUIP": _INF,
    "SCHNEIDER": _INF, "TEGA": _INF, "APARINDS": _INF,
    "HBLENGINE": _INF, "JWL": _INF, "TEXRAIL": _INF,
    "SHRIRAMFIN": _FIN,   # dupe fallback line, moves down to FIN section

    # ── NIFTY FIN SERVICES (non-bank finance) ─────────────────────────
    "BAJFINANCE": _FIN, "BAJAJFINSV": _FIN, "HDFCLIFE": _FIN,
    "SBILIFE": _FIN, "ICICIPRULI": _FIN, "ICICIGI": _FIN,
    "HDFCAMC": _FIN, "CHOLAFIN": _FIN, "SBICARD": _FIN,
    "MUTHOOTFIN": _FIN, "PFC": _FIN, "RECLTD": _FIN,   # dupes (energy) overridden here
    "LICHSGFIN": _FIN, "MFSL": _FIN, "MANAPPURAM": _FIN,
    "IIFL": _FIN, "POONAWALLA": _FIN, "PEL": _FIN,
    "ABCAPITAL": _FIN, "L&TFH": _FIN, "SHRIRAMFIN": _FIN,
    "EDELWEISS": _FIN, "CAMS": _FIN, "MOTILALOFS": _FIN,   # dupe (infra) overridden
    "ANGELONE": _FIN, "CDSL": _FIN, "BSE": _FIN, "MCX": _FIN,
    "NIACL": _FIN, "GICRE": _FIN, "LICI": _FIN,
    "STARHEALTH": _FIN, "UTIAMC": _FIN, "ICICIAMC": _FIN,
    "JMFINANCIL": _FIN, "MASFIN": _FIN, "CREDITACC": _FIN,
    "IEX": _FIN, "PAISALO": _FIN, "REPCOHOME": _FIN,
    "AAVAS": _FIN, "HOMEFIRST": _FIN, "APTUS": _FIN,
    "FIVESTAR": _FIN, "CANFINHOME": _FIN, "PNBHOUSING": _FIN,
    "SPANDANA": _FIN, "UJJIVAN": _FIN, "MAHINDFIN": _FIN,
    "SUNDARMFIN": _FIN, "CAPRIGLOBL": _FIN, "M&MFIN": _FIN,
    "SAMMAANCAP": _FIN, "PIRAMALFIN": _FIN, "CGCL": _FIN,
    "HDBFS": _FIN, "BAJAJHFL": _FIN, "AADHARHFC": _FIN,
    "FEDFINA": _FIN, "SBFC_FIN": _FIN, "ANANDRATHI": _FIN,
    "CHOICEIN": _FIN, "IIFLCAPS": _FIN, "SHAREINDIA": _FIN,
    "PRUDENT": _FIN, "SMCGLOBAL": _FIN, "NUVAMA_FIN": _FIN,
    "CHOLAHLDNG": _FIN, "BAJAJHLDNG": _FIN, "TATACAP": _FIN,
    "LTF": _FIN, "LTM_FIN": _FIN, "360ONE": _FIN,
    "ABSLAMC": _FIN, "NAM-INDIA": _FIN, "MUTHOOTMF": _FIN,
    "KOTAKMF": _FIN, "PTC": _FIN, "PTCIL": _INF,
    "IFCI": _FIN, "PSB": _FIN, "PNBGILTS": _FIN,
    "BLS": _INF, "MASTEK_FIN": _FIN, "MULTI_FIN": _FIN,
    "CARERATING": _FIN, "ICRA": _FIN, "CRISIL": _FIN,
    "MASTERTR": _FIN, "GODIGIT": _FIN, "SATIN": _FIN,
    "SBC": _FIN, "SBCL": _FIN, "CANHLIFE": _FIN,
    "MUFIN": _FIN, "MANCREDIT": _FIN, "MOTISONS": _FIN,
    "NIVABUPA": _FIN, "TSFINV": _FIN, "CIEINDIA": _AUTO,
    "REPCOHOME_FIN": _FIN, "RELIGARE": _FIN, "PROTEAN": _FIN,
    "IIFL_FIN": _FIN, "BSL": _FIN,

    # ── ADDITIONS (nifty500_by_sector research, 2026-08) ──────────────
    # Matched to the ticker already used for comparable stocks elsewhere
    # in this map. See sector_map_ADDITIONS_proposal.py for the reasoning
    # and the 65 stocks left unmapped (no clean-fit ticker exists).
    # Auto
    "ARE&M": _AUTO, "ASAHIINDIA": _AUTO, "ATHERENERG": _AUTO,
    "BANCOINDIA": _AUTO, "BELRISE": _AUTO, "CRAFTSMAN": _AUTO,
    "FIEMIND": _AUTO, "FORCEMOT": _AUTO, "GNA": _AUTO, "HYUNDAI": _AUTO,
    "JKTYRE": _AUTO, "MENONBE": _AUTO, "MUNJALAU": _AUTO,
    "OLAELEC": _AUTO, "OLECTRA": _AUTO, "RKFORGE": _AUTO, "RML": _AUTO,
    "SSWL": _AUTO, "TENNIND": _AUTO, "WHEELS": _AUTO, "ZFCVINDIA": _AUTO,
    # Bank
    "CUB": _BANK,
    # Consumer Durables / Consumption
    "ABFRL": _CDUR, "ABLBL": _CDUR, "ASIANPAINT": _CDUR,
    "BAJAJELEC": _CDUR, "BERGEPAINT": _CDUR, "CELLO": _CDUR,
    "DMART": _CDUR, "FIRSTCRY": _CDUR, "GOCOLORS": _CDUR,
    "JSWDULUX": _CDUR, "LENSKART": _CDUR, "PCJEWELLER": _CDUR,
    "RRKABEL": _CDUR, "SFL": _CDUR, "SOMANYCERA": _CDUR,
    "STOVEKRAFT": _CDUR, "THANGAMAYL": _CDUR, "TRENT": _CDUR,
    "VIPIND": _CDUR, "VMART": _CDUR, "VMM": _CDUR,
    # Energy
    "BBL": _ENR, "DEEPINDS": _ENR, "INOXGREEN": _ENR, "KIRLPNU": _ENR,
    "KSB": _ENR, "PRAJIND": _ENR, "SKIPPER": _ENR, "SWSOLAR": _ENR,
    "VOLTAMP": _ENR,
    # Fin Services
    "AIIL": _FIN, "ARMANFIN": _FIN, "EMKAY": _FIN, "FUSION": _FIN,
    "HUDCO": _FIN, "JIOFIN": _FIN, "MONARCH": _FIN,
    # FMCG
    "ADFFOODS": _FMCG, "BALRAMCHIN": _FMCG, "BBTC": _FMCG,
    "CLSEL": _FMCG, "DIAMONDYD": _FMCG, "EIDPARRY": _FMCG,
    "UNITDSPR": _FMCG, "VSTIND": _FMCG, "ZYDUSWELL": _FMCG, "ABDL": _FMCG,
    # Healthcare (services, not pharma-maker)
    "INDGN": _HLTH,
    # Infra (incl. telecom — no dedicated telecom ticker; Nifty Infra's
    # real NSE composition includes telecom)
    "ACE": _INF, "APOLLO": _INF, "ASHOKA": _INF, "BHARTIARTL": _INF,
    "BHARTIHEXA": _INF, "BIRLACORPN": _INF, "CAPACITE": _INF,
    "CARTRADE": _INF, "CEMPRO": _INF, "DBL": _INF, "DCXINDIA": _INF,
    "GATEWAY": _INF, "GPTINFRA": _INF, "GTLINFRA": _INF, "HCC": _INF,
    "HFCL": _INF, "HONAUT": _INF, "IDEA": _INF, "IGIL": _INF,
    "INDIACEM": _INF, "INDUSTOWER": _INF, "ISGEC": _INF, "ITI": _INF,
    "JKLAKSHMI": _INF, "JSWCEMENT": _INF, "JSWINFRA": _INF,
    "JYOTICNC": _INF, "KPIL": _INF, "MANINFRA": _INF,
    "MTARTECH": _INF, "MTNL": _INF, "NUVOCO": _INF, "ORIENTCEM": _INF,
    "PARAS": _INF, "PATELENG": _INF, "PWL": _INF, "QUESS": _INF,
    "REDINGTON": _INF, "SIMPLEXINF": _INF, "TBOTEK": _INF,
    "TECHNOE": _INF, "TEJASNET": _INF, "THOMASCOOK": _INF, "TTML": _INF,
    "UFBL": _INF, "URBANCO": _INF, "VINDHYATEL": _INF, "WABAG": _INF,
    "WELENT": _INF, "YATRA": _INF, "ZENTEC": _INF,
    "EASEMYTRIP": _INF, "TRAVELFOOD": _INF, "MHRIL": _INF,
    # IT
    "63MOONS": _IT, "CYIENTDLM": _IT, "KELLTONTEC": _IT, "LTM": _IT,
    "NIITLTD": _IT, "NIITMTS": _IT, "NUCLEUS": _IT, "RAMCOSYS": _IT,
    "ZAGGLE": _IT,
    # Media
    "JAGRAN": _MED,
    # Metal
    "MANINDS": _MET, "NSLNISP": _MET, "PRAKASH": _MET,
    "SARDAEN": _MET, "USHAMART": _MET,
    # Pharma
    "ACUTAAS": _PHM, "ADVENZYMES": _PHM, "APLLTD": _PHM,
    "BLUEJET": _PHM, "FDC": _PHM, "HIKAL": _PHM, "INNOVACAP": _PHM,
    "STAR": _PHM, "UNICHEMLAB": _PHM,
}


def build_sector_map(symbols: list[str]) -> dict[str, str]:
    """Return {symbol → sector_index_ticker | 'OTHER'} for every input.

    Instant — pure dict lookup, no network / IO. Symbols not in
    HARDCODED_MAP get 'OTHER' and their alerts skip the sector block.
    """
    unknown = 0
    mapping = {}
    for s in symbols:
        mapped = HARDCODED_MAP.get(s, "OTHER")
        mapping[s] = mapped
        if mapped == "OTHER":
            unknown += 1
    covered = len(symbols) - unknown
    print(f"  sector_map: {covered}/{len(symbols)} covered by hardcoded map "
          f"({unknown} → OTHER, skip sector block)")
    return mapping


def unique_sectors(mapping: dict[str, str]) -> list[str]:
    """Distinct sector-index tickers present in the mapping (excludes OTHER)."""
    return sorted({v for v in mapping.values() if v != "OTHER"})


def label(sector_ticker: str) -> str:
    return SECTOR_LABEL.get(sector_ticker, sector_ticker)
