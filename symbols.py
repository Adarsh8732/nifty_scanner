"""NSE Nifty 500 universe + momentum additions, categorized by backtest results.

Source for Nifty 500: https://archives.nseindia.com/content/indices/ind_nifty500list.csv
Tickers are NSE symbols WITHOUT the ".NS" suffix.

──── STRATEGY CATEGORIZATION ────
Based on bar-by-bar walk-forward backtest:
  - Full 528-stock universe, 5 years of daily + 5y weekly history
  - Data source: yfinance(auto_adjust=False) + auto_adjust_missed_corp_actions
    (validated 0% diff vs Dhan on 9 stocks including VEDL demerger)
  - 7171 production-equivalent alerts (touch trigger on proximal, score ≥ 7,
    strict filter: HTF trend + HTF zone confluence)
  - Trade levels: 0.3% entry/SL buffers, R:R 2.6
  - Universe-wide WR: 22.0% (E[R] = -0.21R/trade) — net NEGATIVE on average

Binary categorization (every stock is in WORKS or DOES_NOT_WORK):
  STRATEGY_WORKS:         alerted WR >= 33% (above 2.6R breakeven 27.78%)
  STRATEGY_DOES_NOT_WORK: alerted WR <  33%  OR  no resolved alerts at all
                          (no proven edge — conservative default)

Note: small-sample stocks (n=1-4) land in whichever bucket their tiny
sample puts them. Treat with appropriate caution.

ALL_SYMBOLS = STRATEGY_WORKS + STRATEGY_DOES_NOT_WORK.
Every stock still alerts. The categorization tag (✅/⚠️) in alert messages
helps the trader weight each alert. The strategy as a WHOLE is breakeven-
to-negative at the universe level — only the WORKS subset is profitable.
"""

# ───────────── 1. STRATEGY VALIDATED — WORKS ─────────────
# 75 stocks. Alerted WR >= 33%.
STRATEGY_WORKS = [
    "ADANIPORTS",     # 4W/8L (33.3%, n=12)
    "AIAENG",         # 10W/9L (52.6%, n=19)
    "ALKEM",          # 9W/13L (40.9%, n=22)
    "ANANDRATHI",     # 5W/7L (41.7%, n=12)
    "APOLLOHOSP",     # 7W/13L (35.0%, n=20)
    "ARTEMISMED",     # 7W/9L (43.8%, n=16)
    "ASHOKLEY",       # 2W/0L (100.0%, n=2)
    "ASTERDM",        # 9W/11L (45.0%, n=20)
    "BAJAJHLDNG",     # 5W/10L (33.3%, n=15)
    "BDL",            # 12W/22L (35.3%, n=34)
    "BIOCON",         # 6W/8L (42.9%, n=14)
    "BLUEJET",        # 2W/4L (33.3%, n=6)
    "CANFINHOME",     # 4W/7L (36.4%, n=11)
    "CAPLIPOINT",     # 8W/15L (34.8%, n=23)
    "CHOICEIN",       # 3W/5L (37.5%, n=8)
    "COHANCE",        # 5W/7L (41.7%, n=12)
    "CYIENT",         # 4W/2L (66.7%, n=6)
    "DCMSHRIRAM",     # 6W/7L (46.2%, n=13)
    "DLF",            # 9W/9L (50.0%, n=18)
    "EICHERMOT",      # 10W/20L (33.3%, n=30)
    "ETERNAL",        # 8W/9L (47.1%, n=17)
    "FEDERALBNK",     # 10W/20L (33.3%, n=30)
    "FINEORG",        # 4W/8L (33.3%, n=12)
    "FORTIS",         # 11W/16L (40.7%, n=27)
    "FSL",            # 7W/14L (33.3%, n=21)
    "GESHIP",         # 4W/7L (36.4%, n=11)
    "GLAND",          # 6W/6L (50.0%, n=12)
    "HAVELLS",        # 7W/14L (33.3%, n=21)
    "HCLTECH",        # 8W/15L (34.8%, n=23)
    "HSCL",           # 5W/10L (33.3%, n=15)
    "INDIACEM",       # 5W/7L (41.7%, n=12)
    "IOB",            # 3W/4L (42.9%, n=7)
    "JBMA",           # 6W/12L (33.3%, n=18)
    "JSWDULUX",       # 6W/9L (40.0%, n=15)
    "JSWSTEEL",       # 8W/15L (34.8%, n=23)
    "JUBLPHARMA",     # 4W/8L (33.3%, n=12)
    "JYOTICNC",       # 1W/1L (50.0%, n=2)
    "KAJARIACER",     # 4W/6L (40.0%, n=10)
    "KAYNES",         # 4W/5L (44.4%, n=9)
    "LALPATHLAB",     # 5W/10L (33.3%, n=15)
    "LLOYDSME",       # 2W/1L (66.7%, n=3)
    "LUPIN",          # 7W/11L (38.9%, n=18)
    "MGL",            # 7W/10L (41.2%, n=17)
    "MOTILALOFS",     # 9W/17L (34.6%, n=26)
    "NAM-INDIA",      # 9W/15L (37.5%, n=24)
    "NEULANDLAB",     # 1W/2L (33.3%, n=3)
    "NTPC",           # 9W/11L (45.0%, n=20)
    "NUVAMA",         # 1W/2L (33.3%, n=3)
    "NYKAA",          # 5W/7L (41.7%, n=12)
    "PGEL",           # 9W/11L (45.0%, n=20)
    "POLICYBZR",      # 7W/12L (36.8%, n=19)
    "PPLPHARMA",      # 2W/2L (50.0%, n=4)
    "RAILTEL",        # 8W/12L (40.0%, n=20)
    "SBICARD",        # 7W/14L (33.3%, n=21)
    "SHRIRAMFIN",     # 9W/15L (37.5%, n=24)
    "SJVN",           # 8W/11L (42.1%, n=19)
    "SONATSOFTW",     # 4W/7L (36.4%, n=11)
    "TATACHEM",       # 2W/3L (40.0%, n=5)
    "TATAINVEST",     # 7W/7L (50.0%, n=14)
    "TCS",            # 6W/11L (35.3%, n=17)
    "TDPOWERSYS",     # 2W/2L (50.0%, n=4)
    "TECHNOE",        # 7W/8L (46.7%, n=15)
    "TEJASNET",       # 6W/12L (33.3%, n=18)
    "THERMAX",        # 7W/14L (33.3%, n=21)
    "TITAGARH",       # 10W/8L (55.6%, n=18)
    "TORNTPHARM",     # 6W/7L (46.2%, n=13)
    "TRIDENT",        # 4W/5L (44.4%, n=9)
    "TTML",           # 3W/6L (33.3%, n=9)
    "TVSMOTOR",       # 11W/21L (34.4%, n=32)
    "USHAMART",       # 3W/5L (37.5%, n=8)
    "UTIAMC",         # 4W/6L (40.0%, n=10)
    "VTL",            # 7W/6L (53.8%, n=13)
    "YESBANK",        # 6W/8L (42.9%, n=14)
    "ZEEL",           # 2W/4L (33.3%, n=6)
    "ZENSARTECH",     # 2W/4L (33.3%, n=6)
]

# ───────────── 2. STRATEGY DOES NOT WORK ─────────────
# 453 stocks. Alerted WR < 33% OR zero resolved alerts.
STRATEGY_DOES_NOT_WORK = [
    "360ONE",         # 1W/15L (6.2%, n=16)
    "3MINDIA",        # 3W/13L (18.8%, n=16)
    "AADHARHFC",      # 0 alerts in window
    "AARTIIND",       # 0W/18L (0.0%, n=18)
    "AARTIPHARM",     # 2W/7L (22.2%, n=9)
    "AAVAS",          # 1W/12L (7.7%, n=13)
    "ABB",            # 8W/26L (23.5%, n=34)
    "ABBOTINDIA",     # 6W/14L (30.0%, n=20)
    "ABCAPITAL",      # 5W/21L (19.2%, n=26)
    "ABDL",           # 0 alerts in window
    "ABFRL",          # 3W/12L (20.0%, n=15)
    "ABLBL",          # 0 alerts in window
    "ABREL",          # 3W/19L (13.6%, n=22)
    "ABSLAMC",        # 4W/13L (23.5%, n=17)
    "ACC",            # 3W/7L (30.0%, n=10)
    "ACE",            # 1W/15L (6.2%, n=16)
    "ACMESOLAR",      # 0 alerts in window
    "ACUTAAS",        # 3W/12L (20.0%, n=15)
    "ADANIENSOL",     # 3W/20L (13.0%, n=23)
    "ADANIENT",       # 2W/5L (28.6%, n=7)
    "ADANIGREEN",     # 3W/17L (15.0%, n=20)
    "ADANIPOWER",     # 2W/17L (10.5%, n=19)
    "AEGISLOG",       # 4W/16L (20.0%, n=20)
    "AEGISVOPAK",     # 0 alerts in window
    "AFCONS",         # 0 alerts in window
    "AFFLE",          # 2W/14L (12.5%, n=16)
    "AIIL",           # 0 alerts in window
    "AJANTPHARM",     # 5W/15L (25.0%, n=20)
    "AMBER",          # 4W/16L (20.0%, n=20)
    "AMBUJACEM",      # 6W/13L (31.6%, n=19)
    "ANANTRAJ",       # 1W/11L (8.3%, n=12)
    "ANGELONE",       # 1W/13L (7.1%, n=14)
    "ANTHEM",         # 0 alerts in window
    "ANURAS",         # 0W/1L (0.0%, n=1)
    "APARINDS",       # 1W/14L (6.7%, n=15)
    "APLAPOLLO",      # 2W/14L (12.5%, n=16)
    "APOLLOTYRE",     # 2W/20L (9.1%, n=22)
    "APTUS",          # 1W/7L (12.5%, n=8)
    "ARE&M",          # 4W/15L (21.1%, n=19)
    "ASAHIINDIA",     # 6W/20L (23.1%, n=26)
    "ASIANPAINT",     # 2W/17L (10.5%, n=19)
    "ASTRAL",         # 3W/19L (13.6%, n=22)
    "ASTRAMICRO",     # 0 alerts in window
    "ATGL",           # 1W/8L (11.1%, n=9)
    "ATHERENERG",     # 0 alerts in window
    "ATUL",           # 2W/15L (11.8%, n=17)
    "AUBANK",         # 0W/13L (0.0%, n=13)
    "AUROPHARMA",     # 5W/18L (21.7%, n=23)
    "AVANTIFEED",     # 2W/12L (14.3%, n=14)
    "AWL",            # 3W/15L (16.7%, n=18)
    "AXISBANK",       # 3W/14L (17.6%, n=17)
    "BAJAJ-AUTO",     # 4W/22L (15.4%, n=26)
    "BAJAJFINSV",     # 4W/11L (26.7%, n=15)
    "BAJAJHFL",       # 0 alerts in window
    "BAJFINANCE",     # 2W/24L (7.7%, n=26)
    "BALKRISIND",     # 2W/21L (8.7%, n=23)
    "BALRAMCHIN",     # 1W/10L (9.1%, n=11)
    "BANDHANBNK",     # 3W/15L (16.7%, n=18)
    "BANKBARODA",     # 3W/24L (11.1%, n=27)
    "BANKINDIA",      # 1W/20L (4.8%, n=21)
    "BATAINDIA",      # 4W/13L (23.5%, n=17)
    "BAYERCROP",      # 2W/11L (15.4%, n=13)
    "BBTC",           # 3W/14L (17.6%, n=17)
    "BEL",            # 5W/28L (15.2%, n=33)
    "BELRISE",        # 0 alerts in window
    "BEML",           # 3W/21L (12.5%, n=24)
    "BERGEPAINT",     # 3W/10L (23.1%, n=13)
    "BHARATFORG",     # 1W/16L (5.9%, n=17)
    "BHARTIARTL",     # 5W/17L (22.7%, n=22)
    "BHARTIHEXA",     # 0 alerts in window
    "BHEL",           # 2W/17L (10.5%, n=19)
    "BIKAJI",         # 1W/3L (25.0%, n=4)
    "BLS",            # 1W/12L (7.7%, n=13)
    "BLUEDART",       # 3W/12L (20.0%, n=15)
    "BLUESTARCO",     # 5W/13L (27.8%, n=18)
    "BOSCHLTD",       # 3W/16L (15.8%, n=19)
    "BPCL",           # 5W/28L (15.2%, n=33)
    "BRIGADE",        # 4W/9L (30.8%, n=13)
    "BRITANNIA",      # 2W/16L (11.1%, n=18)
    "BSE",            # 4W/21L (16.0%, n=25)
    "BSL",            # 2W/7L (22.2%, n=9)
    "BSOFT",          # 4W/12L (25.0%, n=16)
    "CAMS",           # 5W/15L (25.0%, n=20)
    "CANBK",          # 6W/15L (28.6%, n=21)
    "CANHLIFE",       # 0 alerts in window
    "CARBORUNIV",     # 3W/8L (27.3%, n=11)
    "CARTRADE",       # 4W/16L (20.0%, n=20)
    "CASTROLIND",     # 4W/15L (21.1%, n=19)
    "CCL",            # 5W/15L (25.0%, n=20)
    "CDSL",           # 5W/14L (26.3%, n=19)
    "CEATLTD",        # 2W/11L (15.4%, n=13)
    "CEMPRO",         # 6W/16L (27.3%, n=22)
    "CENTRALBK",      # 3W/14L (17.6%, n=17)
    "CESC",           # 4W/12L (25.0%, n=16)
    "CGCL",           # 2W/6L (25.0%, n=8)
    "CGPOWER",        # 4W/18L (18.2%, n=22)
    "CHALET",         # 4W/12L (25.0%, n=16)
    "CHAMBLFERT",     # 2W/17L (10.5%, n=19)
    "CHENNPETRO",     # 1W/15L (6.2%, n=16)
    "CHOLAFIN",       # 8W/27L (22.9%, n=35)
    "CHOLAHLDNG",     # 4W/10L (28.6%, n=14)
    "CIEINDIA",       # 3W/7L (30.0%, n=10)
    "CIPLA",          # 5W/12L (29.4%, n=17)
    "CLEAN",          # 2W/10L (16.7%, n=12)
    "COALINDIA",      # 5W/21L (19.2%, n=26)
    "COCHINSHIP",     # 3W/17L (15.0%, n=20)
    "COFORGE",        # 2W/18L (10.0%, n=20)
    "COLPAL",         # 6W/21L (22.2%, n=27)
    "CONCOR",         # 4W/16L (20.0%, n=20)
    "CONCORDBIO",     # 0W/3L (0.0%, n=3)
    "COROMANDEL",     # 5W/14L (26.3%, n=19)
    "CPPLUS",         # 0 alerts in window
    "CRAFTSMAN",      # 4W/10L (28.6%, n=14)
    "CREDITACC",      # 1W/12L (7.7%, n=13)
    "CRISIL",         # 7W/16L (30.4%, n=23)
    "CROMPTON",       # 2W/9L (18.2%, n=11)
    "CUB",            # 4W/18L (18.2%, n=22)
    "CUMMINSIND",     # 3W/19L (13.6%, n=22)
    "DABUR",          # 1W/16L (5.9%, n=17)
    "DALBHARAT",      # 2W/10L (16.7%, n=12)
    "DATAPATTNS",     # 3W/12L (20.0%, n=15)
    "DCBBANK",        # 2W/11L (15.4%, n=13)
    "DEEPAKFERT",     # 2W/9L (18.2%, n=11)
    "DEEPAKNTR",      # 1W/11L (8.3%, n=12)
    "DELHIVERY",      # 1W/11L (8.3%, n=12)
    "DEVYANI",        # 2W/8L (20.0%, n=10)
    "DIVISLAB",       # 4W/19L (17.4%, n=23)
    "DIXON",          # 2W/10L (16.7%, n=12)
    "DMART",          # 0W/8L (0.0%, n=8)
    "DODLA",          # 1W/11L (8.3%, n=12)
    "DOMS",           # 0 alerts in window
    "DRREDDY",        # 1W/16L (5.9%, n=17)
    "ECLERX",         # 4W/13L (23.5%, n=17)
    "EIDPARRY",       # 3W/11L (21.4%, n=14)
    "EIHOTEL",        # 1W/19L (5.0%, n=20)
    "ELECON",         # 0W/1L (0.0%, n=1)
    "ELGIEQUIP",      # 2W/15L (11.8%, n=17)
    "EMAMILTD",       # 4W/15L (21.1%, n=19)
    "EMCURE",         # 0 alerts in window
    "EMMVEE",         # 0 alerts in window
    "ENDURANCE",      # 1W/11L (8.3%, n=12)
    "ENGINERSIN",     # 7W/19L (26.9%, n=26)
    "ENRIN",          # 0 alerts in window
    "ERIS",           # 2W/9L (18.2%, n=11)
    "ESCORTS",        # 8W/24L (25.0%, n=32)
    "EXIDEIND",       # 1W/21L (4.5%, n=22)
    "FACT",           # 0W/3L (0.0%, n=3)
    "FINCABLES",      # 2W/11L (15.4%, n=13)
    "FIRSTCRY",       # 0 alerts in window
    "FIVESTAR",       # 0 alerts in window
    "FLUOROCHEM",     # 6W/14L (30.0%, n=20)
    "FORCEMOT",       # 4W/10L (28.6%, n=14)
    "GABRIEL",        # 4W/14L (22.2%, n=18)
    "GAIL",           # 6W/18L (25.0%, n=24)
    "GALLANTT",       # 1W/9L (10.0%, n=10)
    "GICRE",          # 4W/12L (25.0%, n=16)
    "GILLETTE",       # 5W/18L (21.7%, n=23)
    "GLAXO",          # 4W/14L (22.2%, n=18)
    "GLENMARK",       # 4W/18L (18.2%, n=22)
    "GMDCLTD",        # 2W/13L (13.3%, n=15)
    "GMRAIRPORT",     # 2W/16L (11.1%, n=18)
    "GODFRYPHLP",     # 0 alerts in window
    "GODIGIT",        # 0 alerts in window
    "GODREJCP",       # 2W/15L (11.8%, n=17)
    "GODREJIND",      # 2W/9L (18.2%, n=11)
    "GODREJPROP",     # 4W/17L (19.0%, n=21)
    "GPIL",           # 3W/18L (14.3%, n=21)
    "GRANULES",       # 0W/11L (0.0%, n=11)
    "GRAPHITE",       # 5W/14L (26.3%, n=19)
    "GRASIM",         # 4W/22L (15.4%, n=26)
    "GRAVITA",        # 3W/7L (30.0%, n=10)
    "GROWW",          # 0 alerts in window
    "GRSE",           # 8W/22L (26.7%, n=30)
    "GVT&D",          # 3W/8L (27.3%, n=11)
    "HAL",            # 6W/17L (26.1%, n=23)
    "HAPPSTMNDS",     # 3W/10L (23.1%, n=13)
    "HBLENGINE",      # 0 alerts in window
    "HCG",            # 3W/7L (30.0%, n=10)
    "HDBFS",          # 0 alerts in window
    "HDFCAMC",        # 4W/15L (21.1%, n=19)
    "HDFCBANK",       # 3W/15L (16.7%, n=18)
    "HDFCLIFE",       # 1W/12L (7.7%, n=13)
    "HEG",            # 3W/20L (13.0%, n=23)
    "HEROMOTOCO",     # 0W/19L (0.0%, n=19)
    "HEXT",           # 0 alerts in window
    "HFCL",           # 1W/5L (16.7%, n=6)
    "HINDALCO",       # 5W/17L (22.7%, n=22)
    "HINDCOPPER",     # 2W/16L (11.1%, n=18)
    "HINDPETRO",      # 4W/19L (17.4%, n=23)
    "HINDUNILVR",     # 5W/11L (31.2%, n=16)
    "HINDZINC",       # 2W/8L (20.0%, n=10)
    "HOMEFIRST",      # 5W/11L (31.2%, n=16)
    "HONASA",         # 0 alerts in window
    "HONAUT",         # 0W/11L (0.0%, n=11)
    "HUDCO",          # 3W/15L (16.7%, n=18)
    "HYUNDAI",        # 0 alerts in window
    "ICICIAMC",       # 0 alerts in window
    "ICICIBANK",      # 4W/13L (23.5%, n=17)
    "ICICIGI",        # 3W/15L (16.7%, n=18)
    "ICICIPRULI",     # 6W/14L (30.0%, n=20)
    "IDBI",           # 7W/20L (25.9%, n=27)
    "IDEA",           # 3W/9L (25.0%, n=12)
    "IDFCFIRSTB",     # 4W/13L (23.5%, n=17)
    "IEX",            # 2W/14L (12.5%, n=16)
    "IFCI",           # 3W/16L (15.8%, n=19)
    "IGIL",           # 0 alerts in window
    "IGL",            # 4W/14L (22.2%, n=18)
    "IIFL",           # 2W/12L (14.3%, n=14)
    "IKS",            # 0 alerts in window
    "INDGN",          # 0 alerts in window
    "INDHOTEL",       # 4W/20L (16.7%, n=24)
    "INDIAMART",      # 2W/11L (15.4%, n=13)
    "INDIANB",        # 9W/21L (30.0%, n=30)
    "INDIGO",         # 6W/18L (25.0%, n=24)
    "INDUSINDBK",     # 1W/7L (12.5%, n=8)
    "INDUSTOWER",     # 1W/9L (10.0%, n=10)
    "INFY",           # 3W/20L (13.0%, n=23)
    "INOXINDIA",      # 0 alerts in window
    "INOXWIND",       # 5W/13L (27.8%, n=18)
    "INTELLECT",      # 0W/8L (0.0%, n=8)
    "IOC",            # 3W/20L (13.0%, n=23)
    "IPCALAB",        # 2W/12L (14.3%, n=14)
    "IRB",            # 3W/12L (20.0%, n=15)
    "IRCON",          # 7W/16L (30.4%, n=23)
    "IRCTC",          # 3W/12L (20.0%, n=15)
    "IREDA",          # 0W/1L (0.0%, n=1)
    "IRFC",           # 3W/14L (17.6%, n=17)
    "ITC",            # 4W/13L (23.5%, n=17)
    "ITCHOTELS",      # 0 alerts in window
    "ITI",            # 1W/14L (6.7%, n=15)
    "J&KBANK",        # 4W/9L (30.8%, n=13)
    "JAINREC",        # 0 alerts in window
    "JBCHEPHARM",     # 3W/14L (17.6%, n=17)
    "JINDALSAW",      # 5W/19L (20.8%, n=24)
    "JINDALSTEL",     # 2W/16L (11.1%, n=18)
    "JIOFIN",         # 1W/3L (25.0%, n=4)
    "JKCEMENT",       # 6W/21L (22.2%, n=27)
    "JKTYRE",         # 3W/21L (12.5%, n=24)
    "JMFINANCIL",     # 2W/19L (9.5%, n=21)
    "JPPOWER",        # 0 alerts in window
    "JSL",            # 2W/17L (10.5%, n=19)
    "JSWCEMENT",      # 0 alerts in window
    "JSWENERGY",      # 3W/16L (15.8%, n=19)
    "JSWINFRA",       # 0 alerts in window
    "JUBLFOOD",       # 1W/5L (16.7%, n=6)
    "JUBLINGREA",     # 3W/12L (20.0%, n=15)
    "JWL",            # 0 alerts in window
    "KALYANKJIL",     # 2W/14L (12.5%, n=16)
    "KARURVYSYA",     # 5W/17L (22.7%, n=22)
    "KEC",            # 1W/13L (7.1%, n=14)
    "KEI",            # 5W/16L (23.8%, n=21)
    "KFINTECH",       # 4W/9L (30.8%, n=13)
    "KIMS",           # 2W/7L (22.2%, n=9)
    "KIRLOSBROS",     # 0W/1L (0.0%, n=1)
    "KIRLOSENG",      # 4W/12L (25.0%, n=16)
    "KOTAKBANK",      # 4W/12L (25.0%, n=16)
    "KPIL",           # 0W/6L (0.0%, n=6)
    "KPITTECH",       # 2W/8L (20.0%, n=10)
    "KPRMILL",        # 3W/11L (21.4%, n=14)
    "KRSNAA",         # 2W/6L (25.0%, n=8)
    "LATENTVIEW",     # 5W/14L (26.3%, n=19)
    "LAURUSLABS",     # 5W/11L (31.2%, n=16)
    "LEMONTREE",      # 5W/18L (21.7%, n=23)
    "LENSKART",       # 0 alerts in window
    "LGEINDIA",       # 0 alerts in window
    "LICHSGFIN",      # 2W/13L (13.3%, n=15)
    "LICI",           # 3W/12L (20.0%, n=15)
    "LINDEINDIA",     # 4W/17L (19.0%, n=21)
    "LODHA",          # 2W/17L (10.5%, n=19)
    "LT",             # 9W/25L (26.5%, n=34)
    "LTF",            # 4W/18L (18.2%, n=22)
    "LTFOODS",        # 1W/18L (5.3%, n=19)
    "LTM",            # 2W/8L (20.0%, n=10)
    "LTTS",           # 2W/8L (20.0%, n=10)
    "LXCHEM",         # 1W/8L (11.1%, n=9)
    "M&M",            # 4W/30L (11.8%, n=34)
    "M&MFIN",         # 5W/13L (27.8%, n=18)
    "MAHABANK",       # 7W/16L (30.4%, n=23)
    "MANAPPURAM",     # 9W/19L (32.1%, n=28)
    "MANKIND",        # 0W/2L (0.0%, n=2)
    "MAPMYINDIA",     # 2W/9L (18.2%, n=11)
    "MARICO",         # 2W/12L (14.3%, n=14)
    "MARUTI",         # 2W/16L (11.1%, n=18)
    "MAXHEALTH",      # 6W/24L (20.0%, n=30)
    "MAZDOCK",        # 7W/20L (25.9%, n=27)
    "MCX",            # 2W/18L (10.0%, n=20)
    "MEDANTA",        # 0W/8L (0.0%, n=8)
    "MEDIASSIST",     # 0 alerts in window
    "MEESHO",         # 0 alerts in window
    "MFSL",           # 2W/12L (14.3%, n=14)
    "MIDHANI",        # 3W/13L (18.8%, n=16)
    "MINDACORP",      # 6W/20L (23.1%, n=26)
    "MMTC",           # 2W/9L (18.2%, n=11)
    "MOTHERSON",      # 4W/14L (22.2%, n=18)
    "MPHASIS",        # 2W/12L (14.3%, n=14)
    "MRF",            # 5W/19L (20.8%, n=24)
    "MRPL",           # 2W/8L (20.0%, n=10)
    "MSUMI",          # 1W/5L (16.7%, n=6)
    "MUTHOOTFIN",     # 7W/19L (26.9%, n=26)
    "NATCOPHARM",     # 4W/10L (28.6%, n=14)
    "NATIONALUM",     # 1W/17L (5.6%, n=18)
    "NAUKRI",         # 6W/19L (24.0%, n=25)
    "NAVA",           # 4W/12L (25.0%, n=16)
    "NAVINFLUOR",     # 4W/9L (30.8%, n=13)
    "NBCC",           # 6W/13L (31.6%, n=19)
    "NCC",            # 4W/13L (23.5%, n=17)
    "NESTLEIND",      # 5W/13L (27.8%, n=18)
    "NETWEB",         # 0 alerts in window
    "NEWGEN",         # 6W/20L (23.1%, n=26)
    "NH",             # 6W/13L (31.6%, n=19)
    "NHPC",           # 1W/11L (8.3%, n=12)
    "NIACL",          # 4W/15L (21.1%, n=19)
    "NIVABUPA",       # 0 alerts in window
    "NLCINDIA",       # 5W/13L (27.8%, n=18)
    "NMDC",           # 4W/19L (17.4%, n=23)
    "NSLNISP",        # 2W/5L (28.6%, n=7)
    "NTPCGREEN",      # 0 alerts in window
    "NUVOCO",         # 1W/6L (14.3%, n=7)
    "OBEROIRLTY",     # 8W/18L (30.8%, n=26)
    "OFSS",           # 4W/12L (25.0%, n=16)
    "OIL",            # 6W/17L (26.1%, n=23)
    "OLAELEC",        # 0 alerts in window
    "OLECTRA",        # 1W/7L (12.5%, n=8)
    "ONESOURCE",      # 0 alerts in window
    "ONGC",           # 4W/15L (21.1%, n=19)
    "PAGEIND",        # 5W/15L (25.0%, n=20)
    "PARADEEP",       # 6W/13L (31.6%, n=19)
    "PATANJALI",      # 2W/11L (15.4%, n=13)
    "PAYTM",          # 0W/12L (0.0%, n=12)
    "PCBL",           # 5W/12L (29.4%, n=17)
    "PERSISTENT",     # 2W/17L (10.5%, n=19)
    "PETRONET",       # 3W/10L (23.1%, n=13)
    "PFC",            # 7W/24L (22.6%, n=31)
    "PFIZER",         # 0W/7L (0.0%, n=7)
    "PHOENIXLTD",     # 8W/24L (25.0%, n=32)
    "PIDILITIND",     # 0W/17L (0.0%, n=17)
    "PIIND",          # 1W/9L (10.0%, n=10)
    "PINELABS",       # 0 alerts in window
    "PIRAMALFIN",     # 0 alerts in window
    "PNB",            # 2W/16L (11.1%, n=18)
    "PNBHOUSING",     # 2W/12L (14.3%, n=14)
    "POLYCAB",        # 9W/24L (27.3%, n=33)
    "POLYMED",        # 0W/1L (0.0%, n=1)
    "POLYPLEX",       # 2W/12L (14.3%, n=14)
    "POONAWALLA",     # 2W/18L (10.0%, n=20)
    "POWERGRID",      # 9W/25L (26.5%, n=34)
    "POWERINDIA",     # 6W/18L (25.0%, n=24)
    "POWERMECH",      # 6W/16L (27.3%, n=22)
    "PREMIERENE",     # 0 alerts in window
    "PRESTIGE",       # 5W/13L (27.8%, n=18)
    "PTCIL",          # 0W/1L (0.0%, n=1)
    "PVRINOX",        # 6W/18L (25.0%, n=24)
    "PWL",            # 0 alerts in window
    "RADICO",         # 5W/12L (29.4%, n=17)
    "RAINBOW",        # 2W/8L (20.0%, n=10)
    "RAMCOCEM",       # 0W/5L (0.0%, n=5)
    "RATNAMANI",      # 3W/8L (27.3%, n=11)
    "RAYMOND",        # 4W/11L (26.7%, n=15)
    "RAYMONDLSL",     # 0 alerts in window
    "RBA",            # 2W/12L (14.3%, n=14)
    "RBLBANK",        # 5W/12L (29.4%, n=17)
    "RECLTD",         # 4W/12L (25.0%, n=16)
    "REDINGTON",      # 1W/18L (5.3%, n=19)
    "RELIANCE",       # 5W/21L (19.2%, n=26)
    "RHIM",           # 3W/11L (21.4%, n=14)
    "RITES",          # 6W/20L (23.1%, n=26)
    "RKFORGE",        # 1W/14L (6.7%, n=15)
    "ROUTE",          # 1W/5L (16.7%, n=6)
    "RPOWER",         # 0 alerts in window
    "RRKABEL",        # 0W/3L (0.0%, n=3)
    "RVNL",           # 3W/15L (16.7%, n=18)
    "SAGILITY",       # 0 alerts in window
    "SAIL",           # 2W/22L (8.3%, n=24)
    "SAILIFE",        # 0 alerts in window
    "SAMMAANCAP",     # 3W/12L (20.0%, n=15)
    "SANSERA",        # 1W/8L (11.1%, n=9)
    "SAPPHIRE",       # 0 alerts in window
    "SARDAEN",        # 5W/14L (26.3%, n=19)
    "SAREGAMA",       # 2W/9L (18.2%, n=11)
    "SBFC",           # 0W/2L (0.0%, n=2)
    "SBILIFE",        # 2W/24L (7.7%, n=26)
    "SBIN",           # 1W/6L (14.3%, n=7)
    "SCHAEFFLER",     # 3W/12L (20.0%, n=15)
    "SCHNEIDER",      # 3W/19L (13.6%, n=22)
    "SCI",            # 3W/10L (23.1%, n=13)
    "SHREECEM",       # 2W/13L (13.3%, n=15)
    "SHYAMMETL",      # 2W/8L (20.0%, n=10)
    "SIEMENS",        # 6W/26L (18.8%, n=32)
    "SIGNATURE",      # 0W/5L (0.0%, n=5)
    "SOBHA",          # 4W/11L (26.7%, n=15)
    "SOLARINDS",      # 3W/11L (21.4%, n=14)
    "SONACOMS",       # 1W/10L (9.1%, n=11)
    "SPLPETRO",       # 0W/5L (0.0%, n=5)
    "SRF",            # 0W/19L (0.0%, n=19)
    "STARHEALTH",     # 0W/3L (0.0%, n=3)
    "STLTECH",        # 0W/5L (0.0%, n=5)
    "SUMICHEM",       # 3W/13L (18.8%, n=16)
    "SUNDARMFIN",     # 2W/11L (15.4%, n=13)
    "SUNPHARMA",      # 2W/10L (16.7%, n=12)
    "SUNTV",          # 2W/12L (14.3%, n=14)
    "SUPREMEIND",     # 2W/16L (11.1%, n=18)
    "SUZLON",         # 0W/6L (0.0%, n=6)
    "SWANCORP",       # 0W/2L (0.0%, n=2)
    "SWIGGY",         # 0 alerts in window
    "SYNGENE",        # 2W/7L (22.2%, n=9)
    "SYRMA",          # 2W/8L (20.0%, n=10)
    "TANLA",          # 0W/4L (0.0%, n=4)
    "TARIL",          # 0 alerts in window
    "TATACAP",        # 0 alerts in window
    "TATACOMM",       # 2W/19L (9.5%, n=21)
    "TATACONSUM",     # 3W/13L (18.8%, n=16)
    "TATAELXSI",      # 4W/10L (28.6%, n=14)
    "TATAPOWER",      # 4W/12L (25.0%, n=16)
    "TATASTEEL",      # 1W/14L (6.7%, n=15)
    "TATATECH",       # 0W/2L (0.0%, n=2)
    "TBOTEK",         # 0 alerts in window
    "TECHM",          # 3W/21L (12.5%, n=24)
    "TEGA",           # 2W/9L (18.2%, n=11)
    "TENNIND",        # 0 alerts in window
    "THELEELA",       # 0 alerts in window
    "TIINDIA",        # 6W/24L (20.0%, n=30)
    "TIMKEN",         # 2W/14L (12.5%, n=16)
    "TITAN",          # 5W/19L (20.8%, n=24)
    "TMCV",           # 0 alerts in window
    "TMPV",           # 3W/15L (16.7%, n=18)
    "TORNTPOWER",     # 4W/11L (26.7%, n=15)
    "TRAVELFOOD",     # 0 alerts in window
    "TRENT",          # 5W/18L (21.7%, n=23)
    "TRITURBINE",     # 4W/16L (20.0%, n=20)
    "UBL",            # 1W/8L (11.1%, n=9)
    "UCOBANK",        # 3W/12L (20.0%, n=15)
    "ULTRACEMCO",     # 3W/17L (15.0%, n=20)
    "UNIONBANK",      # 2W/18L (10.0%, n=20)
    "UNITDSPR",       # 8W/17L (32.0%, n=25)
    "UNOMINDA",       # 4W/16L (20.0%, n=20)
    "UPL",            # 3W/12L (20.0%, n=15)
    "URBANCO",        # 0 alerts in window
    "VBL",            # 4W/11L (26.7%, n=15)
    "VEDL",           # 1W/6L (14.3%, n=7)
    "VIJAYA",         # 2W/10L (16.7%, n=12)
    "VMM",            # 0 alerts in window
    "VOLTAS",         # 4W/16L (20.0%, n=20)
    "WAAREEENER",     # 0 alerts in window
    "WELCORP",        # 4W/21L (16.0%, n=25)
    "WELSPUNLIV",     # 4W/9L (30.8%, n=13)
    "WHIRLPOOL",      # 3W/8L (27.3%, n=11)
    "WIPRO",          # 2W/10L (16.7%, n=12)
    "WOCKPHARMA",     # 2W/20L (9.1%, n=22)
    "ZAGGLE",         # 0 alerts in window
    "ZENTEC",         # 5W/14L (26.3%, n=19)
    "ZFCVINDIA",      # 4W/9L (30.8%, n=13)
    "ZYDUSLIFE",      # 6W/14L (30.0%, n=20)
    "ZYDUSWELL",      # 2W/11L (15.4%, n=13)
]

# ───────────── 3. UNTESTED — broader-universe additions (no backtest data) ─────────────
# 183 stocks from NSE Total Market 750 that were NOT in the Nifty 500
# backtest. Each passed 4 validation gates before being added:
#   - yfinance has >= 200 daily rows in last 1y
#   - 20-day average volume >= 1,00,000 shares
#   - market cap >= 500 cr  (>= 5e9 INR)
#   - mappable to a Dhan security_id (so live LTP polling works)
# Tag in alerts: ❓ UNTESTED — treat with caution until they accumulate enough
# alert history to be re-categorized via _categorize.py.
STRATEGY_UNTESTED = [
    "AARTIDRUGS",
    "ACI",
    "ADVENZYMES",
    "AETHER",
    "AKUMS",
    "ALKYLAMINE",
    "ALOKINDS",
    "APLLTD",
    "APOLLO",
    "ARVIND",
    "ARVINDFASN",
    "ASHAPURMIN",
    "ASHOKA",
    "ASKAUTOLTD",
    "AURIONPRO",
    "AVALON",
    "AVL",
    "AWFIS",
    "AXISCADES",
    "AZAD",
    "BAJAJELEC",
    "BALAMINES",
    "BALUFORGE",
    "BANCOINDIA",
    "BBOX",
    "BECTORFOOD",
    "BLACKBUCK",
    "BLUESTONE",
    "BORORENEW",
    "CAMPUS",
    "CCAVENUE",
    "CELLO",
    "CENTURYPLY",
    "CMSINFO",
    "CRIZAC",
    "CSBBANK",
    "CUPID",
    "DATAMATICS",
    "DBREALTY",
    "DIACABS",
    "EDELWEISS",
    "EIEL",
    "ELECTCAST",
    "ELLEN",
    "EMBDL",
    "EMIL",
    "ENTERO",
    "EPL",
    "EQUITASBNK",
    "EUREKAFORB",
    "FEDFINA",
    "FINPIPE",
    "GAEL",
    "GHCL",
    "GMMPFAUDLR",
    "GNFC",
    "GODREJAGRO",
    "GOKEX",
    "GOKULAGRO",
    "GPPL",
    "GREAVESCOT",
    "GSFC",
    "HCC",
    "HEMIPROP",
    "HERITGFOOD",
    "HGINFRA",
    "ICIL",
    "IFBIND",
    "IIFLCAPS",
    "IMFA",
    "INDIGOPNTS",
    "INOXGREEN",
    "IONEXCHANG",
    "IXIGO",
    "JAIBALAJI",
    "JAMNAAUTO",
    "JAYNECOIND",
    "JKLAKSHMI",
    "JKPAPER",
    "JSFB",
    "JSLL",
    "JUSTDIAL",
    "JYOTHYLAB",
    "KANSAINER",
    "KIRLPNU",
    "KITEX",
    "KNRCON",
    "KPIGREEN",
    "KRBL",
    "KRN",
    "KSB",
    "KTKBANK",
    "LLOYDSENGG",
    "LLOYDSENT",
    "LOTUSDEV",
    "LUMAXTECH",
    "MAHSEAMLES",
    "MANORAMA",
    "MANYAVAR",
    "MARKSANS",
    "MEDPLUS",
    "METROPOLIS",
    "MOIL",
    "MSTCLTD",
    "MTARTECH",
    "NAZARA",
    "NEOGEN",
    "NETWORK18",
    "NFL",
    "OPTIEMUS",
    "ORIENTCEM",
    "OSWALPUMPS",
    "PARAS",
    "PCJEWELLER",
    "PFOCUS",
    "PICCADIL",
    "PNCINFRA",
    "PNGJL",
    "PRAJIND",
    "PRICOLLTD",
    "PRSMJOHNSN",
    "PTC",
    "PURVA",
    "QPOWER",
    "QUESS",
    "RAIN",
    "RALLIS",
    "RATEGAIN",
    "RCF",
    "REDTAPE",
    "REFEX",
    "RELAXO",
    "RELIGARE",
    "RENUKA",
    "RTNINDIA",
    "RTNPOWER",
    "SAFARI",
    "SAMHI",
    "SANDUMA",
    "SENCO",
    "SFL",
    "SHAILY",
    "SHAKTIPUMP",
    "SHARDACROP",
    "SHAREINDIA",
    "SHILPAMED",
    "SKIPPER",
    "SKYGOLD",
    "SOUTHBANK",
    "SPARC",
    "STAR",
    "STARCEMENT",
    "SUDARSCHEM",
    "SUNTECK",
    "SUPRIYA",
    "SURYAROSNI",
    "SWSOLAR",
    "TARC",
    "TEXRAIL",
    "THANGAMAYL",
    "THOMASCOOK",
    "THYROCARE",
    "TI",
    "TIMETECHNO",
    "TIPSMUSIC",
    "TMB",
    "TRANSRAILL",
    "TRIVENI",
    "TVSSCS",
    "UJJIVANSFB",
    "V2RETAIL",
    "VAIBHAVGBL",
    "VARROC",
    "VGUARD",
    "VIKRAMSOLR",
    "VIPIND",
    "VIYASH",
    "VMART",
    "WAAREERTL",
    "WABAG",
    "WEBELSOLAR",
    "WELENT",
    "YATHARTH",
]

# ───────────── MERGE — what the scanner actually uses ─────────────
ALL_SYMBOLS = STRATEGY_WORKS + STRATEGY_DOES_NOT_WORK + STRATEGY_UNTESTED

# Backwards-compat alias
NIFTY_50 = ALL_SYMBOLS
