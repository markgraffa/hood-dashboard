"""
Downloads OCC weekly options volume CSVs for the past N years and parses
them into a flat DataFrame with one row per (date, class, option_type, section).
"""

import re
import time
import requests
import pandas as pd
from io import StringIO
from pathlib import Path
from datetime import date, timedelta

DATA_DIR = Path(__file__).parent / "data"
RAW_DIR = DATA_DIR / "raw"
PARSED_CACHE = DATA_DIR / "combined.parquet"
BASE_URL = "https://marketdata.theocc.com/weekly-volume-reports"
REPORT_CLASSES = ["equity", "index", "etf"]


# ── date helpers ─────────────────────────────────────────────────────────────

def last_friday(d: date) -> date:
    return d - timedelta(days=(d.weekday() - 4) % 7)


def all_fridays(years_back: int = 2) -> list[date]:
    today = date.today()
    end = last_friday(today)
    start = last_friday(today - timedelta(weeks=52 * years_back))
    out, d = [], start
    while d <= end:
        out.append(d)
        d += timedelta(weeks=1)
    return out


# ── parsing ───────────────────────────────────────────────────────────────────

def _clean_num(s: str):
    """'$1,234,567.89' or '1,234,567' → float. Returns None on failure."""
    s = re.sub(r'[$",]', "", s.strip())
    try:
        return float(s)
    except ValueError:
        return None


def parse_report(text: str, report_class: str, report_date: date) -> list[dict]:
    """
    Parse one OCC weekly volume CSV into a list of flat summary dicts.
    Each dict represents one (section, option_type) combination, e.g.
    standard-calls, standard-puts, flex-combined, etc.
    """
    rows = []
    lines = text.replace("\r\n", "\n").splitlines()

    section = None        # 'standard' or 'flex'
    block = None          # 'calls', 'puts', 'combined', 'by_exchange'
    section_data: dict = {}

    def flush(s, d):
        if s and d:
            rows.append(d)

    for i, raw_line in enumerate(lines):
        line = raw_line.strip()

        # Skip the report title line (first non-blank line)
        if i == 0 and line.startswith("Weekly Volume Report"):
            continue

        # ── section header detection ──────────────────────────────────────
        # Only treat standalone section headers (short lines without "Week Ending")
        if re.search(r"Flex Options", line, re.I) and "Week Ending" not in line:
            flush(section, section_data)
            section = "flex"
            section_data = {"report_date": report_date.isoformat(),
                            "report_class": report_class, "section": section}
            block = None
            continue
        if re.search(r"(Equity|Index|ETF)\s+Options", line, re.I) and "Flex" not in line:
            flush(section, section_data)
            section = "standard"
            section_data = {"report_date": report_date.isoformat(),
                            "report_class": report_class, "section": section}
            block = None
            continue

        if section is None:
            continue

        # ── sub-block detection ───────────────────────────────────────────
        if line == "CALLS":
            block = "calls"
            continue
        if line == "PUTS":
            block = "puts"
            continue
        if line == "COMBINED":
            block = "combined"
            continue
        if line.startswith("Premiums by Exchange"):
            block = "by_exchange"
            continue
        if line.startswith("Average Premium"):
            block = "avg_premium"
            continue

        # ── data rows ────────────────────────────────────────────────────
        if block in ("calls", "puts", "combined"):
            # TOTAL CALLS / TOTAL PUTS / TOTAL COMB rows
            m = re.match(r'^TOTAL (CALLS|PUTS|COMB)', line)
            if m:
                parts = re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', line)
                # cols: [label, total_contracts, ob_contracts, ob_premiums,
                #         cb_contracts, cb_premiums, os_contracts, os_premiums,
                #         cs_contracts, cs_premiums]
                prefix = block  # calls / puts / combined
                if len(parts) > 1:
                    section_data[f"{prefix}_total_contracts"] = _clean_num(parts[1])
                if len(parts) > 3:
                    section_data[f"{prefix}_open_buy_premiums"] = _clean_num(parts[3])
                if len(parts) > 5:
                    section_data[f"{prefix}_close_buy_premiums"] = _clean_num(parts[5])
                if len(parts) > 7:
                    section_data[f"{prefix}_open_sell_premiums"] = _clean_num(parts[7])
                if len(parts) > 9:
                    section_data[f"{prefix}_close_sell_premiums"] = _clean_num(parts[9])

            # Also capture by account type for CUST/FIRM/M-M rows
            for acct in ["CUST (ALL)", "FIRM (ALL)", "M-M (ALL)"]:
                if line.startswith(acct):
                    tag = acct.replace(" ", "_").replace("(", "").replace(")", "").lower()
                    parts = re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', line)
                    if len(parts) > 1:
                        section_data[f"{block}_{tag}_total_contracts"] = _clean_num(parts[1])

        elif block == "by_exchange":
            if line.startswith("OCC TOTALS"):
                parts = line.split(",")
                if len(parts) >= 4:
                    section_data["prem_calls"] = _clean_num(parts[1])
                    section_data["prem_puts"] = _clean_num(parts[2])
                    section_data["prem_combined"] = _clean_num(parts[3])

        elif block == "avg_premium":
            if line.startswith("OCC TOTALS"):
                parts = line.split(",")
                if len(parts) >= 4:
                    section_data["avg_prem_calls"] = _clean_num(parts[1])
                    section_data["avg_prem_puts"] = _clean_num(parts[2])
                    section_data["prem_ratio"] = _clean_num(parts[3])

    flush(section, section_data)
    return rows


# ── network ───────────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"})
    return s


def fetch_raw(report_date: date, report_class: str, session: requests.Session):
    cache = RAW_DIR / f"{report_date.strftime('%Y%m%d')}_{report_class}.txt"
    if cache.exists():
        return cache.read_text()

    params = {
        "reportDate": report_date.strftime("%Y%m%d"),
        "reportType": "options",
        "reportClass": report_class,
        "format": "csv",
    }
    try:
        r = session.get(BASE_URL, params=params, timeout=15)
        if r.status_code != 200 or not r.text.strip() or r.text.strip().startswith("<"):
            return None
        if "invalid" in r.text.lower()[:50]:
            return None
        cache.write_text(r.text)
        return r.text
    except Exception as e:
        print(f"  Network error {report_date} {report_class}: {e}")
        return None


# ── main loader ───────────────────────────────────────────────────────────────

def load_all(years_back: int = 2, delay: float = 0.25, force_refresh: bool = False) -> pd.DataFrame:
    DATA_DIR.mkdir(exist_ok=True)
    RAW_DIR.mkdir(exist_ok=True)

    if not force_refresh and PARSED_CACHE.exists():
        print(f"Loading from cache: {PARSED_CACHE}")
        return pd.read_parquet(PARSED_CACHE)

    fridays = all_fridays(years_back)
    session = _make_session()
    all_rows = []

    total = len(fridays) * len(REPORT_CLASSES)
    done = 0
    for d in fridays:
        for rc in REPORT_CLASSES:
            done += 1
            cached = (RAW_DIR / f"{d.strftime('%Y%m%d')}_{rc}.txt").exists()
            if not cached:
                print(f"[{done}/{total}] Fetching {d} {rc}...")
                time.sleep(delay)
            text = fetch_raw(d, rc, session)
            if text:
                rows = parse_report(text, rc, d)
                all_rows.extend(rows)

    if not all_rows:
        raise RuntimeError("No data retrieved — check network access.")

    df = pd.DataFrame(all_rows)
    df["report_date"] = pd.to_datetime(df["report_date"])
    df = df.sort_values(["report_date", "report_class", "section"]).reset_index(drop=True)
    df.to_parquet(PARSED_CACHE, index=False)
    print(f"Saved {len(df):,} rows to {PARSED_CACHE}")
    return df


RBHD_DIR = Path(__file__).parent / "data" / "robinhood"

_MONTH_NUMS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _parse_rbhd_file(path: Path) -> pd.DataFrame:
    """
    Parse one Robinhood metrics Excel file. Handles two formats:
      - Monthly file:            sheet 'Monthly Metrics',  year row 5, month row 6
      - Quarterly supplement:   sheet 'Monthly KPIs',     year row 2, month row 3
    Returns DataFrame: year_month (Period[M]), rbhd_contracts (float).
    """
    xl = pd.ExcelFile(path)
    if "Monthly Metrics" in xl.sheet_names:
        sheet, yr_idx, mo_idx = "Monthly Metrics", 5, 6
    elif "Monthly KPIs" in xl.sheet_names:
        sheet, yr_idx, mo_idx = "Monthly KPIs", 2, 3
    else:
        return pd.DataFrame(columns=["year_month", "rbhd_contracts"])

    raw = xl.parse(sheet, header=None)
    year_row = raw.iloc[yr_idx]
    month_row = raw.iloc[mo_idx]

    col_to_ym: dict = {}
    cur_year = None
    for col in raw.columns:
        yr = year_row.iloc[col]
        mo = month_row.iloc[col]
        if pd.notna(yr):
            try:
                cur_year = int(float(yr))
            except (ValueError, TypeError):
                pass
        if pd.notna(mo) and str(mo).strip() in _MONTH_NUMS and cur_year is not None:
            col_to_ym[col] = pd.Period(
                f"{cur_year}-{_MONTH_NUMS[str(mo).strip()]:02d}", freq="M"
            )

    label_col = 1
    in_total_trading = False
    options_row = None
    for i in range(len(raw)):
        lbl = raw.iloc[i, label_col]
        if pd.isna(lbl):
            continue
        lbl = str(lbl).strip()
        if "Total Trading Volumes" in lbl:
            in_total_trading = True
        elif in_total_trading and "Options Contracts" in lbl and "(M)" in lbl:
            options_row = raw.iloc[i]
            break
        elif in_total_trading and "Average Daily" in lbl:
            break

    if options_row is None:
        return pd.DataFrame(columns=["year_month", "rbhd_contracts"])

    records = []
    for col, ym in col_to_ym.items():
        val = options_row.iloc[col]
        if pd.notna(val):
            try:
                records.append({"year_month": ym, "rbhd_contracts": float(val) * 1_000_000})
            except (ValueError, TypeError):
                pass

    return pd.DataFrame(records)


def load_robinhood_monthly(folder: Path = None) -> pd.DataFrame:
    """
    Parse all Robinhood metrics Excel files in the drop folder.
    For months present in multiple files, keeps the value from the most recently
    modified file (later files may contain revised figures).
    Returns DataFrame: year_month (Period[M]), rbhd_contracts (float).
    """
    if folder is None:
        folder = RBHD_DIR
    folder = Path(folder)
    files = sorted(folder.glob("*.xlsx"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise FileNotFoundError(f"No .xlsx files found in {folder}")

    frames = []
    for f in files:
        df = _parse_rbhd_file(f)
        if not df.empty:
            df["_mtime"] = f.stat().st_mtime
            frames.append(df)

    if not frames:
        raise ValueError("No options contract data could be parsed from files in the drop folder")

    combined = pd.concat(frames, ignore_index=True)
    return (
        combined.sort_values("_mtime")
        .drop_duplicates(subset="year_month", keep="last")
        .drop(columns="_mtime")
        .sort_values("year_month")
        .reset_index(drop=True)
    )


if __name__ == "__main__":
    df = load_all(force_refresh=True)
    print(f"\nShape: {df.shape}")
    print(df.dtypes)
    print(df.head(6).to_string())
