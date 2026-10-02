#!/usr/bin/env python3
"""
Tyzdenny report produktov taishifolie.sk, ktore na frontende hlasia "Momentalne vypredane".

1. Stiahne Heureka feed a vytiahne URL produktov (varianty maju zvycajne vlastne SHOPITEM/URL).
2. Kazdu URL otvori ako zakaznik a precita span.availability.
3. Vypredane ulozi do CSV a (ak je nastavene SMTP) posle emailom.
"""
import csv
import os
import smtplib
import sys
import time
import unicodedata
from datetime import date
from email.message import EmailMessage
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

FEED_URL = os.environ.get("FEED_URL", "https://www.taishifolie.sk/api/v1/feed/heureka.xml")
DELAY = float(os.environ.get("REQUEST_DELAY", "0.7"))  # sekundy medzi poziadavkami
OUT_FILE = os.environ.get("OUT_FILE", f"vypredane-{date.today().isoformat()}.csv")
HEADERS = {"User-Agent": "TaishiFolie-StockCheck/1.0 (internal availability check)"}


def norm(text: str) -> str:
    """Male pismena bez diakritiky, aby sme porovnavali 'Momentalne vypredane' bezpecne."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower().strip()


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].upper()


def load_feed_items():
    r = requests.get(FEED_URL, headers=HEADERS, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items, seen = [], set()
    for shopitem in root.iter():
        if local(shopitem.tag) != "SHOPITEM":
            continue
        data = {local(c.tag): (c.text or "").strip() for c in shopitem}
        url = data.get("URL")
        if not url or url in seen:
            continue
        seen.add(url)
        items.append(
            {
                "id": data.get("ITEM_ID", ""),
                "name": data.get("PRODUCTNAME") or data.get("PRODUCT", ""),
                "code": data.get("PRODUCTNO", ""),
                "url": url,
            }
        )
    return items


def get_page(session, url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in (404, 410):
                return None
        except requests.RequestException:
            pass
        time.sleep(2 * attempt)
    return None


def check_availability(html):
    """Vrati (je_vypredane, text_dostupnosti)."""
    soup = BeautifulSoup(html, "lxml")
    spans = soup.select("span.availability")
    if not spans:
        return False, ""
    texts = [s.get_text(strip=True) for s in spans]
    sold_out = any("vypredan" in norm(t) for t in texts)
    return sold_out, " | ".join(texts)


def send_email(path, count):
    host = os.environ.get("SMTP_HOST")
    if not host:
        print("SMTP nie je nastavene, email sa neposiela.")
        return
    msg = EmailMessage()
    msg["Subject"] = f"TaishiFolie: vypredane produkty ({count}) - {date.today().isoformat()}"
    msg["From"] = os.environ["MAIL_FROM"]
    msg["To"] = os.environ["MAIL_TO"]
    msg.set_content(
        f"V priloženom CSV je {count} produktov/variantov, ktoré e-shop hlási ako "
        "'Momentálne vypredané'. Skontroluj, či nie sú skladom v administrácii."
    )
    with open(path, "rb") as f:
        msg.add_attachment(f.read(), maintype="text", subtype="csv", filename=os.path.basename(path))
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=60) as smtp:
        smtp.starttls()
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)
    print("Email odoslany.")


def main():
    items = load_feed_items()
    print(f"Z feedu nacitanych URL: {len(items)}")
    if not items:
        sys.exit("Feed neobsahuje ziadne produkty - skontroluj FEED_URL / strukturu feedu.")

    session = requests.Session()
    sold, no_marker, failed = [], 0, 0
    for i, item in enumerate(items, 1):
        html = get_page(session, item["url"])
        if html is None:
            failed += 1
            print(f"[{i}/{len(items)}] CHYBA: {item['url']}")
        else:
            is_sold, text = check_availability(html)
            if not text:
                no_marker += 1
            if is_sold:
                sold.append({**item, "availability": text})
        if i % 50 == 0:
            print(f"  spracovanych {i}/{len(items)}, vypredanych zatial {len(sold)}")
        time.sleep(DELAY)

    with open(OUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["id", "code", "name", "url", "availability"], delimiter=";")
        w.writeheader()
        w.writerows(sold)

    print(f"Hotovo. Vypredane: {len(sold)}, stranky bez availability prvku: {no_marker}, chyby nacitania: {failed}")
    # Poistka: ak vacsina stranok nema availability prvok, pravdepodobne sa zmenilo HTML
    if no_marker > len(items) * 0.5:
        sys.exit("Vacsina stranok nema span.availability - zmenil sa HTML? Report nie je dôveryhodny.")

    send_email(OUT_FILE, len(sold))


if __name__ == "__main__":
    main()
