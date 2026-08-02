import argparse
import csv
import datetime as dt
import json
import math
import re
import time
import urllib.error
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
MUFG_HOST = "https://developer.am.mufg.jp"
DAIWA_CSV_URL = "https://www.daiwa-am.co.jp/funds/detail/csv_out.php?code={code}&type=1"
YAHOO_HISTORY_URL = "https://finance.yahoo.co.jp/quote/{code}/history"
WEALTHADVISOR_NAV_URL = "https://apl.wealthadvisor.jp/webasp/yahoo-fund/fund/download.aspx?type=1&fnc={code}"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{code}?period1={period1}&period2={period2}&interval=1d&events=history"


def load_config(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def monday_series(start, end):
    current = start
    while current.weekday() != 0:
        current += dt.timedelta(days=1)
    while current <= end:
        yield current
        current += dt.timedelta(days=7)


def friday_series(start, end):
    current = start
    while current.weekday() != 4:
        current += dt.timedelta(days=1)
    while current <= end:
        yield current
        current += dt.timedelta(days=7)


def demo_history(seed, start, end):
    rows = []
    base_nav = 23000 + seed * 2200
    base_assets = 42000 + seed * 8500
    for index, day in enumerate(monday_series(start, end)):
        drift = index * (92 + seed * 11)
        wave = math.sin(index / 2.1 + seed) * 210
        dip = -280 if index in (3, 13) else 0
        nav = round(base_nav + drift + wave + dip)
        assets = round(base_assets + index * (630 + seed * 85) + max(wave, -100) * 2)
        rows.append({"date": day.isoformat(), "nav": nav, "assets": assets})
    return rows


def request_json(path):
    url = MUFG_HOST + path
    request = urllib.request.Request(url, headers={"User-Agent": "fund-chart-app/0.1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def first_dataset(payload):
    datasets = payload.get("datasets") or []
    if not datasets:
        return None
    return datasets[0]


def yyyymmdd(day):
    return day.strftime("%Y%m%d")


def parse_base_date(value):
    return dt.datetime.strptime(str(value), "%Y%m%d").date()


def normalize_mufg_point(row):
    if not row or row.get("nav") is None:
        return None
    return {
        "date": parse_base_date(row["base_date"]).isoformat(),
        "nav": float(row["nav"]),
        "assets": float(row["netassets"]) if row.get("netassets") is not None else None,
    }


def fetch_mufg_latest(entry):
    return first_dataset(request_json(f"/fund_information_latest/fund_cd/{entry['fund_cd']}"))


def fetch_mufg_date(entry, day):
    path = f"/fund_information_date/fund_cd/{entry['fund_cd']}/base_date/{yyyymmdd(day)}"
    try:
        return first_dataset(request_json(path))
    except urllib.error.HTTPError as error:
        if error.code in (400, 404):
            return None
        raise


def fetch_mufg_near_date(entry, target_day):
    for offset in range(0, 7):
        day = target_day - dt.timedelta(days=offset)
        if day < dt.date(target_day.year, 1, 1):
            return None
        row = fetch_mufg_date(entry, day)
        point = normalize_mufg_point(row)
        if point:
            return point
        time.sleep(0.05)
    return None


def fetch_mufg_first_business_day(entry, year):
    start = dt.date(year, 1, 1)
    for offset in range(0, 14):
        day = start + dt.timedelta(days=offset)
        row = fetch_mufg_date(entry, day)
        point = normalize_mufg_point(row)
        if point:
            return point
        time.sleep(0.05)
    return None


def mufg_history(entry):
    latest = fetch_mufg_latest(entry)
    if not latest:
        raise ValueError(f"No latest data returned for fund_cd={entry['fund_cd']}")

    latest_point = normalize_mufg_point(latest)
    latest_day = parse_base_date(latest["base_date"])
    start = dt.date(latest_day.year, 1, 1)
    points_by_date = {}

    first_business_point = fetch_mufg_first_business_day(entry, latest_day.year)
    if first_business_point:
        points_by_date[first_business_point["date"]] = first_business_point

    for day in friday_series(start, latest_day):
        point = fetch_mufg_near_date(entry, day)
        if point:
            points_by_date[point["date"]] = point

    if latest_point:
        points_by_date[latest_point["date"]] = latest_point

    return [points_by_date[key] for key in sorted(points_by_date)]


def csv_history(csv_path):
    rows = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(
                {
                    "date": row["date"],
                    "nav": float(row["nav"]),
                    "assets": float(row["assets"]) if row.get("assets") else None,
                }
            )
    return rows


def daiwa_history(entry):
    url = DAIWA_CSV_URL.format(code=entry["code"])
    request = urllib.request.Request(url, headers={"User-Agent": "fund-chart-app/0.1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        text = response.read().decode("cp932")

    reader = csv.DictReader(text.splitlines())
    points = []
    for row in reader:
        raw_date = row.get("基準日")
        raw_nav = row.get("基準価額")
        if not raw_date or not raw_nav:
            continue
        day = dt.datetime.strptime(raw_date, "%Y%m%d").date()
        nav = float(raw_nav)
        assets = row.get("純資産総額")
        points.append(
            {
                "date": day.isoformat(),
                "nav": nav,
                "assets": float(assets) if assets not in (None, "") else None,
            }
        )

    if not points:
        raise ValueError(f"No Daiwa data returned for code={entry['code']}")

    latest_day = dt.date.fromisoformat(points[-1]["date"])
    start = dt.date(latest_day.year, 1, 1)
    weekly = []
    latest_seen = None
    point_by_day = {dt.date.fromisoformat(point["date"]): point for point in points}

    first_business_point = next(
        (point for point in points if dt.date.fromisoformat(point["date"]) >= start),
        None,
    )
    if first_business_point:
        weekly.append(first_business_point)
        latest_seen = first_business_point

    for day in friday_series(start, latest_day):
        for offset in range(0, 7):
            candidate = day - dt.timedelta(days=offset)
            if candidate in point_by_day:
                point = point_by_day[candidate]
                if point is not latest_seen:
                    weekly.append(point)
                    latest_seen = point
                break

    if not weekly or weekly[-1]["date"] != points[-1]["date"]:
        weekly.append(points[-1])

    return weekly


def clean_html_text(html):
    html = re.sub(r"<!--\s*-->", "", html)
    html = re.sub(r"<script[\s\S]*?</script>", "", html)
    html = re.sub(r"<style[\s\S]*?</style>", "", html)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text)


def yahoo_history(entry):
    url = YAHOO_HISTORY_URL.format(code=entry["code"])
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        text = clean_html_text(response.read().decode("utf-8"))

    pattern = re.compile(
        r"(\d{4})年(\d{1,2})月(\d{1,2})日\s+([\d,]+)\s+[-+−]?[\d,]+\s+([\d,]+|---)"
    )
    points = []
    for year, month, day, nav, assets in pattern.findall(text):
        date_value = dt.date(int(year), int(month), int(day))
        points.append(
            {
                "date": date_value.isoformat(),
                "nav": float(nav.replace(",", "")),
                "assets": None if assets == "---" else float(assets.replace(",", "")) * 1_000_000,
            }
        )

    if not points:
        raise ValueError(f"No Yahoo history data returned for code={entry['code']}")

    points = sorted({point["date"]: point for point in points}.values(), key=lambda point: point["date"])
    latest_day = dt.date.fromisoformat(points[-1]["date"])
    start = dt.date(latest_day.year, 1, 1)

    first_business_point = next(
        (point for point in points if dt.date.fromisoformat(point["date"]) >= start),
        points[0],
    )
    weekly = [first_business_point]
    latest_seen_date = first_business_point["date"]
    point_by_day = {dt.date.fromisoformat(point["date"]): point for point in points}

    for day in friday_series(start, latest_day):
        for offset in range(0, 7):
            candidate = day - dt.timedelta(days=offset)
            if candidate in point_by_day:
                point = point_by_day[candidate]
                if point["date"] != latest_seen_date:
                    weekly.append(point)
                    latest_seen_date = point["date"]
                break

    if weekly[-1]["date"] != points[-1]["date"]:
        weekly.append(points[-1])

    return weekly


def wealthadvisor_history(entry):
    url = WEALTHADVISOR_NAV_URL.format(code=entry["code"])
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        text = response.read().decode("cp932")

    rows = list(csv.DictReader(text.splitlines()))
    points = []
    for row in rows:
        raw_date = row.get("日付")
        raw_nav = row.get("基準価額")
        if not raw_date or not raw_nav:
            continue
        day = dt.datetime.strptime(raw_date, "%Y%m%d").date()
        points.append({"date": day.isoformat(), "nav": float(raw_nav), "assets": None})

    if not points:
        raise ValueError(f"No WealthAdvisor data returned for code={entry['code']}")

    latest_day = dt.date.fromisoformat(points[-1]["date"])
    start = dt.date(latest_day.year, 1, 1)
    weekly = []
    latest_seen = None
    point_by_day = {dt.date.fromisoformat(point["date"]): point for point in points}

    first_business_point = next(
        (point for point in points if dt.date.fromisoformat(point["date"]) >= start),
        points[0],
    )
    weekly.append(first_business_point)
    latest_seen = first_business_point

    for day in friday_series(start, latest_day):
        for offset in range(0, 7):
            candidate = day - dt.timedelta(days=offset)
            if candidate in point_by_day:
                point = point_by_day[candidate]
                if point is not latest_seen:
                    weekly.append(point)
                    latest_seen = point
                break

    if weekly[-1]["date"] != points[-1]["date"]:
        weekly.append(points[-1])

    return weekly


def yahoo_chart_etf_history(entry):
    today = dt.date.today()
    start = dt.date(today.year, 1, 1)
    period1 = int(dt.datetime.combine(start, dt.time.min, tzinfo=dt.timezone.utc).timestamp())
    period2 = int(dt.datetime.combine(today + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc).timestamp())
    url = YAHOO_CHART_URL.format(code=entry["code"], period1=period1, period2=period2)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))

    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise ValueError(f"No Yahoo chart data returned for code={entry['code']}")

    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    points = []

    explicit_start = dt.date.fromisoformat(entry["start_date"]) if entry.get("start_date") else None

    for timestamp, close, volume in zip(timestamps, closes, volumes):
        if close is None:
            continue
        day = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).date()
        if explicit_start and day < explicit_start:
            continue
        points.append({"date": day.isoformat(), "nav": float(close), "assets": float(volume or 0)})

    if not points:
        raise ValueError(f"No ETF close data returned for code={entry['code']}")

    latest_day = dt.date.fromisoformat(points[-1]["date"])
    weekly = []
    latest_seen = None
    point_by_day = {dt.date.fromisoformat(point["date"]): point for point in points}

    effective_start = explicit_start or start
    first_business_point = next(
        (point for point in points if dt.date.fromisoformat(point["date"]) >= effective_start),
        points[0],
    )
    weekly.append(first_business_point)
    latest_seen = first_business_point

    for day in friday_series(start, latest_day):
        for offset in range(0, 7):
            candidate = day - dt.timedelta(days=offset)
            if candidate in point_by_day:
                point = point_by_day[candidate]
                if point is not latest_seen:
                    weekly.append(point)
                    latest_seen = point
                break

    if weekly[-1]["date"] != points[-1]["date"]:
        weekly.append(points[-1])

    return weekly


def build_fund(entry, index):
    provider = entry.get("provider", "demo")
    today = dt.date.today()
    start = dt.date(today.year, 1, 1)

    if provider == "demo":
        history = demo_history(index + 1, start, today)
    elif provider == "mufg":
        history = mufg_history(entry)
    elif provider == "daiwa":
        history = daiwa_history(entry)
    elif provider == "yahoo":
        history = yahoo_history(entry)
    elif provider == "wealthadvisor":
        history = wealthadvisor_history(entry)
    elif provider == "yahoo_chart_etf":
        history = yahoo_chart_etf_history(entry)
    elif provider == "csv":
        csv_path = BASE_DIR / entry["csv"]
        history = csv_history(csv_path)
    else:
        raise ValueError(f"Unsupported provider: {provider}")

    return {
        "id": entry["id"],
        "name": entry["name"],
        "company": entry.get("company", ""),
        "source": provider,
        "fund_cd": entry.get("fund_cd"),
        "code": entry.get("code"),
        "history": history,
    }


def embed_payload_in_html(payload):
    embedded_json = json.dumps(payload, ensure_ascii=False, indent=6)
    replacement = "const fallbackData = " + embedded_json + ";"
    pattern = r"const fallbackData = [\s\S]*?\n\s*const byId ="
    updated = []

    for filename in ["fund-etf-chart-app.html", "fund-etf-chart-app-x.html", "index.html"]:
        html_path = BASE_DIR / filename
        if not html_path.exists():
            continue

        html = html_path.read_text(encoding="utf-8-sig")
        next_html, count = re.subn(pattern, replacement, html, count=1)
        if count != 1:
            raise ValueError(f"Could not find fallbackData block in {filename}")
        next_html = next_html.replace(replacement, replacement + "\n\n    const byId =")
        next_html = next_html.replace(
            "ローカルファイルとして開いているため、内蔵デモデータのみ表示しています。HTTPサーバー経由で開くとfund-data.jsonを読み込みます。",
            "ローカルファイルとして開いているため、HTMLに埋め込んだ最新取得データを表示しています。",
        )
        html_path.write_text(next_html, encoding="utf-8")
        updated.append(html_path)

    return updated


def main():
    parser = argparse.ArgumentParser(description="Update weekly investment trust and ETF chart data.")
    parser.add_argument("--config", default="fund_etf_config.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-embed-html", action="store_true")
    args = parser.parse_args()

    config_path = BASE_DIR / args.config
    config = load_config(config_path)
    output_path = BASE_DIR / config.get("output", "fund-data.json")
    previous_payload = {}
    previous_funds = {}

    if output_path.exists():
        try:
            previous_payload = load_config(output_path)
            previous_funds = {fund.get("id"): fund for fund in previous_payload.get("funds", [])}
        except Exception as error:
            print(f"Warning: could not read previous data: {error}")

    funds = []
    errors = []
    for index, entry in enumerate(config["funds"]):
        try:
            fund = build_fund(entry, index)
            print(f"Fetched: {fund['name']} ({fund.get('code') or fund.get('fund_cd') or fund['source']})")
        except Exception as error:
            previous = previous_funds.get(entry["id"])
            if not previous:
                raise
            fund = previous
            errors.append({"id": entry["id"], "name": entry["name"], "error": str(error)})
            print(f"Warning: reused previous data for {entry['name']}: {error}")
        funds.append(fund)

    payload = {
        "updatedAt": dt.date.today().isoformat(),
        "funds": funds,
        "fetchErrors": errors,
        "previousUpdatedAt": previous_payload.get("updatedAt"),
    }

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])
        print(f"\nDry run only. Would write: {output_path}")
        return

    with output_path.open("w", encoding="utf-8-sig") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(f"Updated {len(funds)} funds: {output_path}")
    if errors:
        print(f"Completed with {len(errors)} reused fund(s).")
    if not args.no_embed_html:
        for html_path in embed_payload_in_html(payload):
            print(f"Embedded latest data: {html_path}")


if __name__ == "__main__":
    main()
