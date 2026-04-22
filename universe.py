"""
universe.py — The eligible trading universe for the EMA / trend strategy.

Methodology
-----------
~250 liquid US-traded symbols spanning all major sectors and asset classes,
selected to be representative of the investable universe AS OF early 2020.

Inclusion principles (in priority order):
1. Was an S&P 500 component or major liquid stock as of 2020-01-01.
2. Survived the 2020-2024 period (still trading) — we ARE introducing some
   survivorship bias by definition (delisted names aren't here), but this is
   the practical constraint of using yfinance / Alpaca free tier.
3. Daily volume > $50M typical (excludes thin names that suffer slippage).
4. Spans all 11 GICS sectors with deliberate over-weight on cyclicals
   (where momentum effects are historically strongest).

Deliberate INCLUSIONS of names that subsequently underperformed (INTC, GE,
IBM, T, XOM, energy mid-caps, etc.) so the universe isn't biased toward
2024-known winners.

Deliberate EXCLUSIONS:
- Stocks that IPO'd after 2020 (no consistent history).
- Penny stocks, OTC, ADRs of thinly-traded foreign issuers.
- Special-purpose vehicles (SPACs, leveraged ETFs).
"""

# ------------------------------------------------------------------ #
# Information Technology (~35 names)                                 #
# ------------------------------------------------------------------ #
TECH = [
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "AMD",
    "INTC", "ORCL", "CSCO", "ADBE", "CRM", "IBM", "QCOM", "AVGO",
    "TXN", "MU", "ACN", "NOW", "INTU", "ADP", "PYPL", "SNPS",
    "CDNS", "KLAC", "AMAT", "LRCX", "MCHP", "ADI", "CTSH", "NTAP",
    "FIS", "GLW", "HPE", "DELL",
]

# ------------------------------------------------------------------ #
# Communication Services (~12)                                       #
# ------------------------------------------------------------------ #
COMM = [
    "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS", "CHTR", "EA",
    "TTWO", "ROKU", "OMC", "IPG",
]

# ------------------------------------------------------------------ #
# Financials (~30)                                                   #
# ------------------------------------------------------------------ #
FINANCIALS = [
    "JPM", "BAC", "WFC", "C", "GS", "MS", "BLK", "SCHW", "AXP",
    "V", "MA", "COF", "USB", "PNC", "BK", "STT", "TRV", "AIG",
    "MET", "PRU", "ALL", "AFL", "HIG", "PGR", "MMC", "AON",
    "CINF", "CME", "ICE", "MCO", "SPGI",
]

# ------------------------------------------------------------------ #
# Healthcare (~28)                                                   #
# ------------------------------------------------------------------ #
HEALTHCARE = [
    "JNJ", "PFE", "MRK", "UNH", "LLY", "ABBV", "BMY", "AMGN",
    "GILD", "ABT", "TMO", "DHR", "MDT", "CVS", "CI", "HUM",
    "REGN", "VRTX", "BIIB", "ISRG", "ZTS", "BSX", "SYK", "BDX",
    "EW", "ILMN", "IDXX", "RMD",
]

# ------------------------------------------------------------------ #
# Consumer Discretionary (~22)                                       #
# ------------------------------------------------------------------ #
CONSUMER_DISC = [
    "HD", "NKE", "MCD", "SBUX", "LOW", "TGT", "EBAY", "YUM",
    "CMG", "ROST", "TJX", "BBY", "LULU", "MAR", "HLT", "BKNG",
    "EXPE", "F", "GM", "ORLY", "AZO", "ULTA",
]

# ------------------------------------------------------------------ #
# Consumer Staples (~15)                                             #
# ------------------------------------------------------------------ #
CONSUMER_STAPLES = [
    "PG", "KO", "PEP", "WMT", "COST", "KMB", "CL", "EL",
    "GIS", "K", "HSY", "MDLZ", "STZ", "SYY", "MO", "PM",
]

# ------------------------------------------------------------------ #
# Industrials (~22)                                                  #
# ------------------------------------------------------------------ #
INDUSTRIALS = [
    "BA", "HON", "GE", "CAT", "UPS", "FDX", "MMM", "RTX",
    "LMT", "NOC", "GD", "DE", "EMR", "ETN", "ITW", "PH",
    "CSX", "NSC", "UNP", "WM", "FAST", "CMI",
]

# ------------------------------------------------------------------ #
# Energy (~10)                                                       #
# ------------------------------------------------------------------ #
ENERGY = [
    "XOM", "CVX", "COP", "SLB", "OXY", "EOG", "MPC", "PSX",
    "VLO", "KMI",
]

# ------------------------------------------------------------------ #
# Materials (~10)                                                    #
# ------------------------------------------------------------------ #
MATERIALS = [
    "LIN", "APD", "ECL", "SHW", "NEM", "FCX", "DD", "DOW",
    "NUE", "PPG",
]

# ------------------------------------------------------------------ #
# Real Estate (~10)                                                  #
# ------------------------------------------------------------------ #
REAL_ESTATE = [
    "AMT", "PLD", "CCI", "EQIX", "PSA", "SPG", "O", "WELL",
    "AVB", "EQR",
]

# ------------------------------------------------------------------ #
# Utilities (~10)                                                    #
# ------------------------------------------------------------------ #
UTILITIES = [
    "NEE", "DUK", "SO", "AEP", "EXC", "XEL", "SRE", "D",
    "ED", "AWK",
]

# ------------------------------------------------------------------ #
# Sector SPDR ETFs (11) — gives the strategy direct sector exposure  #
# without picking individual names. Often outperform stocks during   #
# sector-wide rotations.                                             #
# ------------------------------------------------------------------ #
SECTOR_ETFS = [
    "XLK",  # Tech
    "XLF",  # Financials
    "XLE",  # Energy
    "XLV",  # Healthcare
    "XLY",  # Consumer Discretionary
    "XLP",  # Consumer Staples
    "XLI",  # Industrials
    "XLU",  # Utilities
    "XLB",  # Materials
    "XLRE", # Real Estate
    "XLC",  # Communication Services
]

# ------------------------------------------------------------------ #
# International ADRs / global mega-caps (~15) — diversifies beyond US #
# ------------------------------------------------------------------ #
INTERNATIONAL = [
    "TSM",   # Taiwan Semi (semiconductor giant)
    "ASML",  # Dutch chip equipment
    "NVO",   # Novo Nordisk (Danish pharma)
    "AZN",   # AstraZeneca (UK pharma)
    "SAP",   # German enterprise software
    "BABA",  # Alibaba (Chinese tech)
    "JD",    # JD.com (Chinese e-commerce)
    "BIDU",  # Baidu (Chinese internet)
    "SHOP",  # Shopify (Canadian)
    "BHP",   # BHP (Australian mining)
    "RIO",   # Rio Tinto (UK mining)
    "VALE",  # Vale (Brazilian mining)
    "PBR",   # Petrobras (Brazilian oil)
    "HDB",   # HDFC Bank (Indian bank)
    "INFY",  # Infosys (Indian IT)
]

# ------------------------------------------------------------------ #
# Tesla / autos / pure-momentum names worth their own bucket         #
# ------------------------------------------------------------------ #
MOMENTUM_FAVORITES = [
    "TSLA",  # Most-traded EV / momentum darling
    "NFLX",  # Already in COMM but listed for clarity
]


def get_full_universe():
    """Return the deduplicated full universe as a sorted list."""
    all_names = (
        TECH + COMM + FINANCIALS + HEALTHCARE + CONSUMER_DISC
        + CONSUMER_STAPLES + INDUSTRIALS + ENERGY + MATERIALS
        + REAL_ESTATE + UTILITIES + SECTOR_ETFS + INTERNATIONAL
        + MOMENTUM_FAVORITES
    )
    return sorted(set(all_names))


def universe_size() -> int:
    return len(get_full_universe())


if __name__ == "__main__":
    universe = get_full_universe()
    print(f"Universe size: {len(universe)}")
    print(f"Sectors: TECH={len(TECH)}, COMM={len(COMM)}, "
          f"FIN={len(FINANCIALS)}, HC={len(HEALTHCARE)}")
    print(f"         CD={len(CONSUMER_DISC)}, CS={len(CONSUMER_STAPLES)}, "
          f"IND={len(INDUSTRIALS)}, ENG={len(ENERGY)}")
    print(f"         MAT={len(MATERIALS)}, RE={len(REAL_ESTATE)}, "
          f"UTL={len(UTILITIES)}")
    print(f"         ETFs={len(SECTOR_ETFS)}, INTL={len(INTERNATIONAL)}")
    print(f"\nFirst 20 names: {universe[:20]}")
