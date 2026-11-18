"""Search ss.lv car listings by criteria, record matches and email new ones.

Config:   config.yaml  (searches)
Output:   results.md   (all currently listed matches)
State:    state.json   (ads already notified / rejected by text filter)
Email:    env SMTP_USER, SMTP_PASSWORD, EMAIL_TO [, SMTP_HOST, SMTP_PORT]
          If not set, email is skipped.

Usage:
    python search.py            # search, write results, email new matches
    python search.py --dry-run  # print matches only; no files written, no email
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

BASE = "https://www.ss.lv"
ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.yaml"
STATE_FILE = ROOT / "state.json"
RESULTS_FILE = ROOT / "results.md"
STATE_TTL_DAYS = 90
REQUEST_DELAY = 1.0
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.8",
}
# listing header text -> Ad field
COLUMNS = {"Модель": "model", "Год": "year", "Объём": "engine", "Пробег": "mileage", "Цена": "price"}
FUEL_PATTERNS = {
    "diesel": re.compile(r"^\d+(\.\d+)?\s*D$", re.I),  # "2.0D"
    "petrol": re.compile(r"^\d+(\.\d+)?$"),  # "2.5" (no suffix)
}


@dataclass
class Ad:
    id: str
    url: str
    title: str
    model: str = ""
    year: int | None = None
    engine: str = ""
    mileage: str = ""
    price: int | None = None
    price_text: str = ""
    search: str = ""


def to_int(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def parse_page(page_html: str) -> list[Ad]:
    soup = BeautifulSoup(page_html, "html.parser")
    header = [td.get_text(strip=True) for td in soup.select("tr#head_line td.msg_column_td")]
    fields = [COLUMNS.get(h) for h in header]
    ads = []
    for tr in soup.select('tr[id^="tr_"]'):
        ad_id = tr["id"][3:]
        if not ad_id.isdigit():  # banner rows like tr_bnr_*
            continue
        link = tr.select_one("a.am")
        tds = tr.find_all("td", recursive=False)
        msg = next((i for i, td in enumerate(tds) if "msg2" in td.get("class", [])), None)
        if not link or msg is None:
            continue
        ad = Ad(
            id=ad_id,
            url=BASE + link["href"],
            title=" ".join(link.get_text(" ", strip=True).split())[:150],
        )
        for field, td in zip(fields, tds[msg + 1 :]):
            value = td.get_text(" ", strip=True)
            if field == "year":
                ad.year = to_int(value)
            elif field == "price":
                ad.price_text = value
                ad.price = to_int(value) if "€" in value else None
            elif field:
                setattr(ad, field, value)
        ads.append(ad)
    return ads


def fetch(session: requests.Session, url: str) -> requests.Response:
    resp = session.get(url, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    time.sleep(REQUEST_DELAY)
    return resp


def fetch_listings(session: requests.Session, path: str, pages: int) -> list[Ad]:
    ads: list[Ad] = []
    for n in range(1, pages + 1):
        suffix = "" if n == 1 else f"page{n}.html"
        resp = fetch(session, f"{BASE}/ru/transport/cars/{path.strip('/')}/sell/{suffix}")
        if n > 1 and not resp.url.endswith(suffix):  # past the last page -> redirected to page 1
            break
        page_ads = parse_page(resp.text)
        if not page_ads:
            break
        ads.extend(page_ads)
    return ads


def ad_text(session: requests.Session, url: str) -> str:
    soup = BeautifulSoup(fetch(session, url).text, "html.parser")
    div = soup.select_one("#msg_div_msg")
    return div.get_text(" ", strip=True) if div else ""


def mileage_km(text: str) -> int | None:
    """'297 тыс.' -> 297000, '17' -> 17, '-' -> None."""
    n = to_int(text)
    if n is None:
        return None
    return n * 1000 if "тыс" in text else n


def matches_basic(ad: Ad, s: dict) -> bool:
    fuel = s.get("fuel")
    if fuel and not FUEL_PATTERNS[fuel].match(ad.engine):
        return False
    for key, value, ok in (
        ("year_min", ad.year, lambda v, lim: v >= lim),
        ("year_max", ad.year, lambda v, lim: v <= lim),
        ("price_min", ad.price, lambda v, lim: v >= lim),
        ("price_max", ad.price, lambda v, lim: v <= lim),
    ):
        limit = s.get(key)
        if limit is not None and (value is None or not ok(value, limit)):
            return False
    # mileage is often not given; only filter when it is
    km = mileage_km(ad.mileage)
    if s.get("mileage_max") is not None and km is not None and km > s["mileage_max"]:
        return False
    return True


def load_state() -> dict:
    state = {"first_seen": {}, "notified": {}, "text_rejected": {}}
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return state


def save_state(state: dict) -> None:
    cutoff = (date.today() - timedelta(days=STATE_TTL_DAYS)).isoformat()
    for key in state:
        state[key] = {k: v for k, v in state[key].items() if v >= cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def write_results(found: list[Ad], first_seen: dict[str, str]) -> None:
    lines = ["# Current matches", ""]
    by_search: dict[str, list[Ad]] = {}
    for a in found:
        by_search.setdefault(a.search, []).append(a)
    for name, ads in by_search.items():
        lines += [f"## {name} ({len(ads)})", "",
                  "| Year | Engine | Mileage | Price | First seen | Ad |",
                  "|---|---|---|---|---|---|"]
        for a in sorted(ads, key=lambda a: a.price or 0):
            title = a.title.replace("|", "/")[:80]
            lines.append(f"| {a.year or ''} | {a.engine} | {a.mileage} | {a.price_text} "
                         f"| {first_seen.get(a.id, '')} | [{title}]({a.url}) |")
        lines.append("")
    if not found:
        lines.append("_No matches._")
    RESULTS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def email_configured() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"))


def send_email(ads: list[Ad]) -> None:
    text = "\n\n".join(
        f"[{a.search}] {a.title}\n{a.year} | {a.engine} | {a.mileage} | {a.price_text}\n{a.url}"
        for a in ads
    )
    rows = "".join(
        f"<tr><td>{html.escape(a.search)}</td>"
        f'<td><a href="{html.escape(a.url)}">{html.escape(a.title)}</a></td>'
        f"<td>{a.year or ''}</td><td>{html.escape(a.engine)}</td>"
        f"<td>{html.escape(a.mileage)}</td><td><b>{html.escape(a.price_text)}</b></td></tr>"
        for a in ads
    )
    msg = EmailMessage()
    msg["Subject"] = f"ss.lv: {len(ads)} new car ad(s)"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(text)
    msg.add_alternative(
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">'
        "<tr><th>Search</th><th>Ad</th><th>Year</th><th>Engine</th><th>Mileage</th>"
        f"<th>Price</th></tr>{rows}</table>",
        subtype="html",
    )
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "465")
    with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    searches = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))["searches"]
    state = load_state()
    today = date.today().isoformat()
    session = requests.Session()
    session.headers.update(HEADERS)

    found: dict[str, Ad] = {}
    for s in searches:
        name = s["name"]
        text_re = re.compile(s["text"], re.I) if s.get("text") else None
        listings = fetch_listings(session, s["path"], s.get("pages", 5))
        hits = []
        for a in listings:
            if not matches_basic(a, s):
                continue
            if text_re and not text_re.search(a.title):
                # title is a truncated preview; check the full ad unless already rejected
                if a.id in state["text_rejected"] or not text_re.search(ad_text(session, a.url)):
                    state["text_rejected"].setdefault(a.id, today)
                    continue
            a.search = name
            hits.append(a)
            found.setdefault(a.id, a)
        print(f"[{name}] scanned {len(listings)} ads, {len(hits)} match")

    new = [a for a in found.values() if a.id not in state["notified"]]
    print(f"{len(found)} match(es), {len(new)} new")

    if args.dry_run:
        for a in found.values():
            print(json.dumps(asdict(a), ensure_ascii=False))
        return 0

    for a in found.values():
        state["first_seen"].setdefault(a.id, today)
    write_results(list(found.values()), state["first_seen"])

    # ads are marked notified only after a successful send, so once SMTP is
    # configured the first email contains everything not yet reported
    email_failed = False
    if new and email_configured():
        try:
            send_email(new)
            print(f"Email sent ({len(new)} ads)")
            for a in new:
                state["notified"][a.id] = today
        except Exception as e:
            print(f"::error::Email failed: {e}")
            email_failed = True
    elif new:
        print("::warning::SMTP not configured, email skipped")

    save_state(state)
    return 1 if email_failed else 0


if __name__ == "__main__":
    sys.exit(main())
