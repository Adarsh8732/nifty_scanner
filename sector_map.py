"""Static stock → NSE-sector-index mapping.

Why hardcoded instead of yfinance.info:
  Yahoo's /quoteSummary endpoint is aggressively rate-limited from cloud
  IPs — GitHub Actions runners get HTTP 401 "Invalid Crumb" on nearly
  every request. yfinance.info worked on the local dev box but was near-
  useless in production. A static dict covers the ~200 stocks we're most
  likely to alert on (Nifty 500 leaders across 11 sectors) with zero
  external calls and zero flakiness.

Coverage: any stock NOT in HARDCODED_MAP gets "OTHER" and skips the
sector block on its alerts. That's ~800 tail stocks — the ones you're
least likely to hold size in — and adding to the map is a one-line PR.

The values are NSE sector index Yahoo tickers, used by
scanner.build_sector_context to fetch each unique sector's OHLC once
per session and compute trend + demand/supply zones.
"""
from __future__ import annotations

# ─── SECTOR INDEX LABELS ────────────────────────────────────────────────
SECTOR_LABEL: dict[str, str] = {
    "^NSEBANK":    "NIFTY BANK",
    "^CNXFIN":     "NIFTY FIN",
    "^CNXIT":      "NIFTY IT",
    "^CNXAUTO":    "NIFTY AUTO",
    "^CNXPHARMA":  "NIFTY PHARMA",
    "^CNXFMCG":    "NIFTY FMCG",
    "^CNXMETAL":   "NIFTY METAL",
    "^CNXENERGY":  "NIFTY ENERGY",
    "^CNXREALTY":  "NIFTY REALTY",
    "^CNXMEDIA":   "NIFTY MEDIA",
    "^CNXINFRA":   "NIFTY INFRA",
    "OTHER":       "OTHER",
}


# ─── HARDCODED STOCK → SECTOR-INDEX MAP ─────────────────────────────────
# Curated from Nifty sector index constituents. When a stock qualifies
# for multiple sectors (e.g. HDFCBANK is in both Nifty Bank and Nifty
# Financial Services), we map it to the more specific one (BANK).
# Duplicates within this dict are a bug — key uniqueness enforced by dict.
_BANK = "^NSEBANK"
_FIN  = "^CNXFIN"
_IT   = "^CNXIT"
_AUTO = "^CNXAUTO"
_PHM  = "^CNXPHARMA"
_FMCG = "^CNXFMCG"
_MET  = "^CNXMETAL"
_ENR  = "^CNXENERGY"
_RLT  = "^CNXREALTY"
_MED  = "^CNXMEDIA"
_INF  = "^CNXINFRA"

HARDCODED_MAP: dict[str, str] = {
    # ── NIFTY BANK ────────────────────────────────────────────────────
    "HDFCBANK": _BANK, "ICICIBANK": _BANK, "SBIN": _BANK,
    "KOTAKBANK": _BANK, "AXISBANK": _BANK, "INDUSINDBK": _BANK,
    "BANKBARODA": _BANK, "FEDERALBNK": _BANK, "PNB": _BANK,
    "IDFCFIRSTB": _BANK, "AUBANK": _BANK, "BANDHANBNK": _BANK,
    "CANBK": _BANK, "IDBI": _BANK, "IOB": _BANK, "UCOBANK": _BANK,
    "CENTRALBK": _BANK, "MAHABANK": _BANK, "INDIANB": _BANK,
    "RBLBANK": _BANK, "YESBANK": _BANK, "SOUTHBANK": _BANK,
    "KARURVYSYA": _BANK, "CITYUNIONBK": _BANK, "DCBBANK": _BANK,
    "J&KBANK": _BANK, "CSBBANK": _BANK, "TMB": _BANK,

    # ── NIFTY IT ──────────────────────────────────────────────────────
    "TCS": _IT, "INFY": _IT, "HCLTECH": _IT, "WIPRO": _IT,
    "LTIM": _IT, "TECHM": _IT, "PERSISTENT": _IT, "COFORGE": _IT,
    "MPHASIS": _IT, "LTTS": _IT, "OFSS": _IT, "TATAELXSI": _IT,
    "KPITTECH": _IT, "HAPPSTMNDS": _IT, "INTELLECT": _IT,
    "ZENSARTECH": _IT, "BIRLASOFT": _IT, "CYIENT": _IT,
    "SONATSOFTW": _IT, "NEWGEN": _IT, "RATEGAIN": _IT,
    "TATATECH": _IT, "ROUTE": _IT, "MASTEK": _IT,

    # ── NIFTY AUTO ────────────────────────────────────────────────────
    "MARUTI": _AUTO, "M&M": _AUTO, "TATAMOTORS": _AUTO,
    "BAJAJ-AUTO": _AUTO, "EICHERMOT": _AUTO, "HEROMOTOCO": _AUTO,
    "ASHOKLEY": _AUTO, "TVSMOTOR": _AUTO, "BHARATFORG": _AUTO,
    "MOTHERSON": _AUTO, "BOSCHLTD": _AUTO, "MRF": _AUTO,
    "BALKRISIND": _AUTO, "EXIDEIND": _AUTO, "TIINDIA": _AUTO,
    "SONACOMS": _AUTO, "ENDURANCE": _AUTO, "AMBER": _AUTO,
    "APOLLOTYRE": _AUTO, "CEATLTD": _AUTO, "SUNDRMFAST": _AUTO,
    "ESCORTS": _AUTO, "ASTRAZEN": _AUTO, "ZFCVINDIA": _AUTO,
    "SCHAEFFLER": _AUTO, "MINDA": _AUTO, "SANDHAR": _AUTO,
    "GOODYEAR": _AUTO, "JBMA": _AUTO, "GABRIEL": _AUTO,

    # ── NIFTY PHARMA ──────────────────────────────────────────────────
    "SUNPHARMA": _PHM, "DIVISLAB": _PHM, "CIPLA": _PHM,
    "DRREDDY": _PHM, "LUPIN": _PHM, "TORNTPHARM": _PHM,
    "BIOCON": _PHM, "AUROPHARMA": _PHM, "GLENMARK": _PHM,
    "LAURUSLABS": _PHM, "ZYDUSLIFE": _PHM, "ALKEM": _PHM,
    "MANKIND": _PHM, "ABBOTINDIA": _PHM, "SANOFI": _PHM,
    "IPCALAB": _PHM, "GRANULES": _PHM, "NATCOPHARM": _PHM,
    "JBCHEPHARM": _PHM, "AJANTPHARM": _PHM, "GLAND": _PHM,
    "PFIZER": _PHM, "PPLPHARMA": _PHM, "SUVENPHAR": _PHM,
    "CAPLIPOINT": _PHM, "ERIS": _PHM, "SEQUENT": _PHM,
    "STRIDES": _PHM, "SUPRIYA": _PHM, "SOLARA": _PHM,
    "INDOCO": _PHM, "MEDPLUS": _PHM, "FORTIS": _PHM,
    "APOLLOHOSP": _PHM, "MAXHEALTH": _PHM, "NARAYANHRLR": _PHM,
    "GLOBALHITECH": _PHM, "KIMS": _PHM, "SHILPAMED": _PHM,

    # ── NIFTY FMCG ────────────────────────────────────────────────────
    "HINDUNILVR": _FMCG, "ITC": _FMCG, "NESTLEIND": _FMCG,
    "BRITANNIA": _FMCG, "DABUR": _FMCG, "MARICO": _FMCG,
    "TATACONSUM": _FMCG, "GODREJCP": _FMCG, "COLPAL": _FMCG,
    "PGHH": _FMCG, "RADICO": _FMCG, "VBL": _FMCG,
    "EMAMILTD": _FMCG, "UBL": _FMCG, "MCDOWELL-N": _FMCG,
    "JUBLFOOD": _FMCG, "VARUN": _FMCG, "WESTLIFE": _FMCG,
    "HATSUN": _FMCG, "PATANJALI": _FMCG, "BAJAJCON": _FMCG,
    "GILLETTE": _FMCG, "GODFRYPHLP": _FMCG, "AWL": _FMCG,
    "KALYANKJIL": _FMCG, "TITAN": _FMCG, "TRENT": _FMCG,
    "PAGEIND": _FMCG, "VIP": _FMCG, "RELAXO": _FMCG,
    "BATAINDIA": _FMCG, "CENTURYPLY": _FMCG, "GREENPLY": _FMCG,
    "HAVELLS": _FMCG, "VOLTAS": _FMCG, "CROMPTON": _FMCG,
    "WHIRLPOOL": _FMCG, "BLUESTARCO": _FMCG, "SYMPHONY": _FMCG,

    # ── NIFTY METAL ───────────────────────────────────────────────────
    "TATASTEEL": _MET, "HINDALCO": _MET, "JSWSTEEL": _MET,
    "VEDL": _MET, "COALINDIA": _MET, "NMDC": _MET,
    "JINDALSTEL": _MET, "SAIL": _MET, "ADANIENT": _MET,
    "NATIONALUM": _MET, "HINDZINC": _MET, "HINDCOPPER": _MET,
    "RATNAMANI": _MET, "WELCORP": _MET, "APLAPOLLO": _MET,
    "JSL": _MET, "JINDALSAW": _MET, "MOIL": _MET,
    "GMDCLTD": _MET, "GRAVITA": _MET, "SANDUMA": _MET,
    "MAHSEAMLES": _MET, "SHYAMMETL": _MET,

    # ── NIFTY ENERGY ──────────────────────────────────────────────────
    "RELIANCE": _ENR, "ONGC": _ENR, "POWERGRID": _ENR,
    "NTPC": _ENR, "IOC": _ENR, "BPCL": _ENR, "GAIL": _ENR,
    "HINDPETRO": _ENR, "TATAPOWER": _ENR, "ADANIGREEN": _ENR,
    "ADANIPOWER": _ENR, "JSWENERGY": _ENR, "TORNTPOWER": _ENR,
    "NHPC": _ENR, "CESC": _ENR, "OIL": _ENR,
    "MGL": _ENR, "IGL": _ENR, "GUJGASLTD": _ENR,
    "AEGISLOG": _ENR, "PETRONET": _ENR, "GSPL": _ENR,
    "SJVN": _ENR, "SUZLON": _ENR, "INOXWIND": _ENR,

    # ── NIFTY REALTY ──────────────────────────────────────────────────
    "DLF": _RLT, "GODREJPROP": _RLT, "LODHA": _RLT,
    "OBEROIRLTY": _RLT, "PRESTIGE": _RLT, "PHOENIXLTD": _RLT,
    "BRIGADE": _RLT, "SOBHA": _RLT, "MAHLIFE": _RLT,
    "SUNTECK": _RLT, "IBREALEST": _RLT, "SIGNATURE": _RLT,
    "RAYMOND": _RLT, "ANANTRAJ": _RLT, "KOLTEPATIL": _RLT,
    "ARVSMART": _RLT, "PURVA": _RLT,

    # ── NIFTY MEDIA ───────────────────────────────────────────────────
    "ZEEL": _MED, "SUNTV": _MED, "NETWORK18": _MED,
    "DISHTV": _MED, "TV18BRDCST": _MED, "PVRINOX": _MED,
    "SAREGAMA": _MED, "HATHWAY": _MED, "DEN": _MED,
    "NAZARA": _MED, "TIPS": _MED, "SHEMAROO": _MED,

    # ── NIFTY INFRA ───────────────────────────────────────────────────
    "LT": _INF, "ADANIPORTS": _INF, "HAL": _INF, "BEL": _INF,
    "GRASIM": _INF, "ULTRACEMCO": _INF, "SHREECEM": _INF,
    "AMBUJACEM": _INF, "DALBHARAT": _INF, "GMRINFRA": _INF,
    "IRB": _INF, "KEC": _INF, "KNRCON": _INF, "RVNL": _INF,
    "IRFC": _INF, "RITES": _INF, "NBCC": _INF, "TITAGARH": _INF,
    "ACC": _INF, "JKCEMENT": _INF, "RAMCOCEM": _INF,
    "HEIDELBERG": _INF, "PRISMJOHNS": _INF, "STARCEMENT": _INF,
    "IRCON": _INF, "GRINFRA": _INF, "PNCINFRA": _INF,
    "NCC": _INF, "HGINFRA": _INF, "JKIL": _INF,
    "IGARASHI": _INF, "BHEL": _INF, "SIEMENS": _INF,
    "ABB": _INF, "CGPOWER": _INF, "THERMAX": _INF,
    "CUMMINSIND": _INF, "TIMKEN": _INF, "SKFINDIA": _INF,
    "MAZDOCK": _INF, "COCHINSHIP": _INF, "BEML": _INF,
    "GESHIP": _INF, "SCI": _INF, "CONCOR": _INF,
    "GRSE": _INF, "BDL": _INF, "SOLARINDS": _INF,
    "PARADEEP": _INF, "GNFC": _INF, "COROMANDEL": _INF,
    "DEEPAKNTR": _INF, "TATACHEM": _INF, "PIDILITIND": _INF,
    "SRF": _INF, "ATUL": _INF, "AARTIIND": _INF,

    # ── NIFTY FINANCIAL SERVICES (non-bank) ───────────────────────────
    "BAJFINANCE": _FIN, "BAJAJFINSV": _FIN, "HDFCLIFE": _FIN,
    "SBILIFE": _FIN, "ICICIPRULI": _FIN, "ICICIGI": _FIN,
    "HDFCAMC": _FIN, "CHOLAFIN": _FIN, "SBICARD": _FIN,
    "MUTHOOTFIN": _FIN, "PFC": _FIN, "RECLTD": _FIN,
    "LICHSGFIN": _FIN, "MFSL": _FIN, "MANAPPURAM": _FIN,
    "IIFL": _FIN, "POONAWALLA": _FIN, "PEL": _FIN,
    "ABCAPITAL": _FIN, "L&TFH": _FIN, "SHRIRAMFIN": _FIN,
    "EDELWEISS": _FIN, "PAYTM": _FIN, "POLICYBZR": _FIN,
    "CAMS": _FIN, "MOTILALOFS": _FIN, "ANGELONE": _FIN,
    "CDSL": _FIN, "BSE": _FIN, "MCX": _FIN,
    "NIACL": _FIN, "GICRE": _FIN, "LICI": _FIN,
    "STARHEALTH": _FIN, "KOTAKMF": _FIN, "UTIAMC": _FIN,
    "JMFINANCIL": _FIN, "MASFIN": _FIN, "CREDITACC": _FIN,
    "IEX": _FIN, "PAISALO": _FIN, "REPCOHOME": _FIN,
    "AAVAS": _FIN, "HOMEFIRST": _FIN, "APTUS": _FIN,
    "FIVESTAR": _FIN, "CANFINHOME": _FIN, "PNBHOUSING": _FIN,
    "SPANDANA": _FIN, "UJJIVAN": _FIN, "EQUITASBNK": _FIN,
    "UJJIVANSFB": _FIN, "ESAFSFB": _FIN, "SURYODAY": _FIN,
    "CAPRIGLOBL": _FIN, "MAHINDFIN": _FIN, "SUNDARMFIN": _FIN,
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
