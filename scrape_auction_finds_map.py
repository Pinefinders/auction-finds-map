import os, re, json, time, hashlib, logging, requests, subprocess
from pathlib import Path
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin

SEARCH_TERMS = ["pine"]

# Words that mark a lot as NOT antique. Matched as whole words,
# case-insensitive, against the lot title.
EXCLUDE_WORDS = [
    "new",
    "modern",
    "contemporary",
    "reproduction",
    "repro",
    "mexican",         # almost always 1990s-2000s mass-produced pine
    "ikea",
    "flatpack", "flat-pack", "flat pack",
]
_EXCLUDE_RE = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in EXCLUDE_WORDS) + r")\b", re.IGNORECASE)


def is_excluded(title):
    """Return the matched exclude word, or None if the title is fine."""
    if not title:
        return None
    m = _EXCLUDE_RE.search(title)
    return m.group(0) if m else None


LOCAL_HOUSES = [
    "churchill", "overture", "amersham",
    "bourne end", "jones & jacob", "jones and jacob", "tring market",
    "psp",
]

EASYLIVE_BASE = "https://www.easyliveauction.com"
SEARCH_URL    = f"{EASYLIVE_BASE}/catalogue/"
REPO_DIR      = Path(os.environ.get("REPO_DIR", os.path.expanduser("~/auction-finds-map")))
IMAGES_DIR    = REPO_DIR / "images"
SEEN_FILE     = REPO_DIR / "seen_lots.json"
POSTCODES_FILE = Path(os.environ.get("POSTCODES_FILE",
    REPO_DIR / "house_postcodes.json"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

REQUEST_DELAY = 1.5
MAX_PAGES     = 30   # safety cap; pine typically returns ~16 pages
MAX_LOTS      = 200

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def is_local(house_name):
    name = house_name.lower()
    return any(local in name for local in LOCAL_HOUSES)


def image_filename(url):
    ext = url.split("?")[0].rsplit(".", 1)[-1]
    ext = ext if ext in ("jpg", "jpeg", "png", "webp", "gif") else "jpg"
    return hashlib.md5(url.encode()).hexdigest()[:12] + "." + ext


def download_image(url, dest):
    if dest.exists():
        return True
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            dest.write_bytes(r.content)
            return True
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
                continue
            log.warning(f"Image download failed after 3 attempts: {url}  ({e})")
            return False
    return False


def parse_card(card):
    # Image
    img_el  = card.select_one("img.lot-image")
    img_url = img_el.get("src", "") if img_el else ""
    if img_url.startswith("//"):
        img_url = "https:" + img_url
    elif img_url.startswith("/"):
        img_url = EASYLIVE_BASE + img_url

    # Link + lot ID
    link_el = card.select_one("div.grid-catalogue-thumb-container a[href]")
    href    = link_el["href"] if link_el else ""
    url     = urljoin(EASYLIVE_BASE, href) if href else ""
    lot_id  = hashlib.md5(url.encode()).hexdigest()[:12] if url else hashlib.md5(img_url.encode()).hexdigest()[:12]

    # Auction ID (shared across all lots in the same sale). It lives on a
    # child <a data-id="..."> inside the card, not on the .grid-lot div itself.
    auction_id = ""
    aid_el = card.find(attrs={"data-id": True})
    if aid_el:
        auction_id = aid_el.get("data-id", "")

    # Title — the <p> inside a.no-hover
    title_el = card.select_one("a.no-hover p")
    title    = title_el.get_text(strip=True) if title_el else ""
    if not title:
        return None

    # Estimate — find <p> containing "Estimate"
    estimate = ""
    for p in card.select("a.no-hover p"):
        txt = p.get_text(" ", strip=True)
        if "Estimate" in txt:
            estimate = txt.replace("Estimate", "").strip()
            break

    # Current bid
    bid = ""
    for p in card.select("a.no-hover p"):
        txt = p.get_text(" ", strip=True)
        if "Current Bid" in txt:
            bid = txt.replace("Current Bid:", "").strip()
            break

    # Auction house — a.blue-text inside small
    house_el = card.select_one("small a.blue-text")
    house    = house_el.get_text(strip=True).replace("by ", "") if house_el else "Unknown"

    # Time left
    time_left = ""
    small = card.select_one("small")
    if small:
        for p in small.select("p"):
            txt = p.get_text(" ", strip=True)
            if "Time Left" in txt:
                time_left = txt.replace("Time Left:", "").strip()
                break

    # Lot number - extract from URL like "...-lot-409/"
    lot_number = ""
    if url:
        lot_match = re.search(r'-lot-(\d+)/?', url)
        if lot_match:
            lot_number = lot_match.group(1)

    return {
        "id":         lot_id,
        "auction_id": auction_id,
        "title":      title,
        "house":      house,
        "estimate":   estimate,
        "bid":        bid,
        "time_left":  time_left,
        "sale_date":  "",        # populated after auction-level fetch
        "sale_dates_raw": "",    # full block, for the v2 tooltip / future per-lot parsing
        "url":        url,
        "img_url":    img_url,
        "img_file":   image_filename(img_url) if img_url else "",
        "local":     is_local(house),
        "lot_number": lot_number,
    }


def scrape_term(session, term):
    lots, seen_ids = [], set()
    excluded_total = 0
    excluded_samples = []  # (word, title) tuples for log
    for page in range(1, MAX_PAGES + 1):
        params = {"searchTerm": term, "searchOption": 3, "currentPage": page}
        try:
            r = session.get(SEARCH_URL, params=params, headers=HEADERS, timeout=20)
            if r.status_code == 404:
                log.info(f"  '{term}' page {page}: 404 (past last page) — stopping")
                break
            r.raise_for_status()
        except Exception as e:
            log.warning(f"Request failed for '{term}' page {page}: {e}")
            break

        soup  = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("div.grid-lot")

        if not cards:
            log.info(f"  No cards on '{term}' page {page} — stopping")
            break

        new = 0
        page_excluded = 0
        for card in cards:
            try:
                lot = parse_card(card)
            except Exception as e:
                log.debug(f"Parse error: {e}")
                continue
            if not lot or lot["id"] in seen_ids:
                continue
            seen_ids.add(lot["id"])
            bad = is_excluded(lot["title"])
            if bad:
                excluded_total += 1
                page_excluded += 1
                if len(excluded_samples) < 8:
                    excluded_samples.append((bad, lot["title"][:80]))
                continue
            lot["search_term"] = term
            lots.append(lot)
            new += 1

        log.info(f"  '{term}' page {page}: {len(cards)} cards, {new} kept, {page_excluded} excluded, {len(lots)} total")
        time.sleep(REQUEST_DELAY)
        if len(cards) < 10:
            break

    if excluded_total:
        log.info(f"  '{term}' excluded {excluded_total} lots by EXCLUDE_WORDS; samples:")
        for word, title in excluded_samples:
            log.info(f"    [{word}] {title}")

    return lots


# --- Seen-lots tracking ---------------------------------------------------
def load_seen():
    """Return set of lot IDs we've seen in previous runs."""
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(lot_ids):
    SEEN_FILE.write_text(json.dumps(sorted(lot_ids)), encoding="utf-8")


# --- House postcode lookup / fuzzy matching -------------------------------
_COMPANY_SUFFIXES = [
    " ltd", " limited", " llp", " plc",
    " and valuers", " & valuers",
]


def _normalize(name):
    n = (name or "").strip().lower()
    n = n.rstrip(".,;:·- ")
    changed = True
    while changed:
        changed = False
        for suf in _COMPANY_SUFFIXES:
            if n.endswith(suf):
                candidate = n[: -len(suf)].strip()
                if len(candidate.split()) >= 2:
                    n = candidate
                    changed = True
    return n


def load_postcodes():
    if not POSTCODES_FILE.exists():
        return {}, {}
    try:
        data = json.loads(POSTCODES_FILE.read_text())
    except Exception:
        return {}, {}
    raw = {k: v for k, v in data.items() if not k.startswith("_")}
    norm = {}
    for name, info in raw.items():
        key = _normalize(name)
        if key and key not in norm:
            norm[key] = info
    return raw, norm


def _find_truncated(name, raw):
    if not name or not name.endswith("..."):
        return None
    stem = name[:-3].strip().lower()
    if len(stem) < 6:
        return None
    matches = [info for full, info in raw.items() if full.lower().startswith(stem)]
    if len(matches) == 1:
        return matches[0]
    nstem = _normalize(name)
    if nstem and len(nstem) >= 6:
        nmatches = [info for full, info in raw.items() if _normalize(full).startswith(nstem)]
        if len(nmatches) == 1:
            return nmatches[0]
        rev = [info for full, info in raw.items() if nstem.startswith(_normalize(full)) and len(_normalize(full)) >= 6]
        if len(rev) == 1:
            return rev[0]
    return None


def house_meta(house, postcodes):
    raw, norm = postcodes
    info = (
        raw.get(house)
        or norm.get(_normalize(house))
        or _find_truncated(house, raw)
    )
    if not info:
        return {"postcode": None, "location": None, "map_url": None, "known": False}
    pc = info.get("postcode", "")
    loc = info.get("location") or ""
    if not loc and info.get("address"):
        addr = info["address"]
        if pc and pc in addr:
            addr = addr.replace(pc, "").strip().rstrip(",")
        loc = addr
    map_url = f"https://www.google.com/maps/search/?api=1&query={pc.replace(' ', '+')}" if pc else None
    return {"postcode": pc, "location": loc, "map_url": map_url, "known": True}


# --- HTML rendering -------------------------------------------------------
def _today_date_str(d=None):
    """Return EasyLive's date format for `d` (default today), e.g.
    'Sun 24th May 2026'. Used to match against `sale_date` / `sale_dates_raw`.
    """
    from datetime import date as _date
    d = d or _date.today()
    DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d.weekday()]
    def _suffix(n):
        if 10 <= n % 100 <= 20: return "th"
        return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{DOW} {d.day}{_suffix(d.day)} {d.strftime('%b')} {d.year}"


def _is_today(lot, today_str):
    """True if the lot's sale_date or sale_dates_raw mentions today.
    Matches both 'Ends Sun 24th May 2026 ...' (timed) and multi-day live
    bands like 'Sun 24th May 2026 9am BST (Lots 1 to 765) Mon 25th ...'.
    """
    blob = (lot.get("sale_date") or "") + " || " + (lot.get("sale_dates_raw") or "")
    return today_str in blob


def _card_html(lot, is_new, postcodes):
    img_src = f"images/{lot['img_file']}" if lot.get("img_file") else ""
    img_tag = (
        f'<img src="{img_src}" alt="{lot["title"]}" width="500" height="500" loading="lazy">'
        if img_src else '<div class="no-img">No image</div>'
    )
    bid      = f'<span class="bid">Bid {lot["bid"]}</span>'           if lot.get("bid")       else ""
    estimate = f'<span class="estimate">Est {lot["estimate"]}</span>' if lot.get("estimate") else ""

    sale_date = lot.get("sale_date") or ""
    sale_raw  = (lot.get("sale_dates_raw") or "").replace('"', "'")
    if sale_date:
        tip = f' data-tip="📅 {sale_raw}"' if sale_raw and sale_raw != sale_date else ''
        saledate_html = f'<span class="saledate"{tip}>📅 {sale_date}</span>'
    elif lot.get("time_left"):
        saledate_html = f'<span class="timeleft">⏱ {lot["time_left"]}</span>'
    else:
        saledate_html = ""
    new_badge = '<span class="new-badge">NEW</span>' if is_new else ""

    h = house_meta(lot.get("house", ""), postcodes)
    if h["known"] and h["map_url"]:
        tooltip = f'📍 {h["postcode"]}'
        if h["location"]:
            tooltip += f' · {h["location"]}'
        tooltip += ' · click for map'
        house_html_str = (
            f'<span class="house" data-tip="{tooltip}" '
            f'onclick="event.preventDefault(); event.stopPropagation(); '
            f"window.open('{h['map_url']}','_blank'); "
            f'">{lot["house"]} <span class="pc">{h["postcode"]}</span></span>'
        )
    elif h["known"]:
        loc = h["location"] or "location on file"
        house_html_str = f'<span class="house" data-tip="🌍 {loc}">{lot["house"]} <span class="pc pc-intl">{loc}</span></span>'
    else:
        house_html_str = f'<span class="house unknown" data-tip="📍 postcode unknown">{lot["house"]} <span class="pc-unknown">?</span></span>'

    lot_num_html = f'<span class="lot-number">Lot {lot["lot_number"]}</span>' if lot.get("lot_number") else ""

    return f"""
    <a class="card" href="{lot['url']}" target="_blank" rel="noopener">
      <div class="card-img">{img_tag}{new_badge}</div>
      <div class="card-body">
        <p class="title">{lot['title']}</p>
        <p class="house-line">{house_html_str}</p>
        <div class="meta">{lot_num_html}{bid}{estimate}{saledate_html}</div>
      </div>
    </a>"""


def _section_html(title, lots, anchor, seen, postcodes, css_class=""):
    if not lots:
        return f'<section id="{anchor}" class="{css_class}"><h2>{title}</h2><p class="empty">No results found.</p></section>'
    cards = "\n".join(_card_html(l, l["id"] not in seen, postcodes) for l in lots)
    new_count = sum(1 for l in lots if l["id"] not in seen)
    new_pill = f' <span class="new-count">{new_count} new</span>' if new_count else ""
    return f"""
    <section id="{anchor}" class="{css_class}">
      <h2>{title} <span class="count">{len(lots)} lots</span>{new_pill}</h2>
      <div class="masonry">{cards}</div>
    </section>"""


def build_html(local_lots, wide_lots, seen=None, postcodes=None):
    """Generate the combined auction-finds + map HTML."""
    if seen is None:
        seen = set()
    if postcodes is None:
        postcodes = ({}, {})
    now       = datetime.now().strftime("%A %d %B %Y, %H:%M")
    terms_str = ", ".join(SEARCH_TERMS)
    total     = len(local_lots) + len(wide_lots)
    new_total = sum(1 for l in local_lots + wide_lots if l["id"] not in seen)

    today_str = _today_date_str()
    local_today = [l for l in local_lots if _is_today(l, today_str)]
    local_later = [l for l in local_lots if not _is_today(l, today_str)]
    wide_today  = [l for l in wide_lots  if _is_today(l, today_str)]
    wide_later  = [l for l in wide_lots  if not _is_today(l, today_str)]
    today_total = len(local_today) + len(wide_today)

    # Build PC_MAP from postcodes data (only houses with lat/lng)
    raw_pc, _ = postcodes
    pc_entries = []
    for name, info in raw_pc.items():
        if not isinstance(info, dict):
            continue
        pc = info.get("postcode", "")
        lat = info.get("lat")
        lng = info.get("lng")
        if pc and lat and lng:
            pc_key = pc.replace(" ", "").upper()
            n_esc = info["name"] if "name" in info else name
            n_esc = json.dumps(n_esc)
            pc_entries.append(f'  "{pc_key}":{{name:{n_esc},lat:{lat},lng:{lng}}}')
    pc_map_js = "const PC_MAP = {\n" + ",\n".join(pc_entries) + "\n};"

    # Render card sections
    local_html = _section_html("📍 Local auctions", local_lots, "local", seen, postcodes, "local-section")
    today_html = _section_html(f"🔥 UK-Wide · selling today ({today_str})", wide_today, "today", seen, postcodes, "today-section") if wide_today else ""
    later_html = _section_html("🇬🇧 UK-Wide · later", wide_later, "uk-wide", seen, postcodes, "")

    local_local_count = len(local_lots)
    wide_today_count = len(wide_today)
    wide_later_count = len(wide_later)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Pinefinders — Auction Finds with Map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    :root {{
      --bg: #faf7f2;
      --panel: #ffffff;
      --ink: #2a241d;
      --muted: #8a7e6f;
      --accent: #a8743a;
      --accent-soft: #f4ead8;
      --local-bg: #fdf6e8;
      --local-border: #d9a85a;
      --shadow: 0 1px 3px rgba(40,30,15,0.06), 0 4px 12px rgba(40,30,15,0.05);
      --shadow-hover: 0 4px 10px rgba(40,30,15,0.10), 0 10px 28px rgba(40,30,15,0.10);
      --radius: 10px;
      --new-bg: #2c6e2c;
      --highlight: #d9531e;
    }}
    @media (prefers-color-scheme: dark) {{
      :root:not([data-theme="light"]) {{
        --bg: #14110d;
        --panel: #1f1b15;
        --ink: #ede4d2;
        --muted: #8a7e6f;
        --accent: #d9a85a;
        --accent-soft: #2a2218;
        --local-bg: #2a2218;
        --local-border: #d9a85a;
        --shadow: 0 1px 3px rgba(0,0,0,0.4), 0 4px 12px rgba(0,0,0,0.3);
        --shadow-hover: 0 4px 10px rgba(0,0,0,0.5), 0 10px 28px rgba(0,0,0,0.4);
      }}
    }}
    :root[data-theme="dark"] {{
      --bg: #14110d;
      --panel: #1f1b15;
      --ink: #ede4d2;
      --muted: #8a7e6f;
      --accent: #d9a85a;
      --accent-soft: #2a2218;
      --local-bg: #2a2218;
      --local-border: #d9a85a;
      --shadow: 0 1px 3px rgba(0,0,0,0.4), 0 4px 12px rgba(0,0,0,0.3);
      --shadow-hover: 0 4px 10px rgba(0,0,0,0.5), 0 10px 28px rgba(0,0,0,0.4);
    }}

    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{ background: var(--bg); color: var(--ink); height: 100%; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      -webkit-font-smoothing: antialiased;
      display: flex;
      flex-direction: column;
    }}

    header {{
      background: var(--panel);
      border-bottom: 1px solid var(--accent-soft);
      padding: 14px 24px 12px;
      position: sticky; top: 0; z-index: 1000;
      backdrop-filter: blur(8px);
      display: flex; align-items: center; gap: 16px 20px; flex-wrap: wrap;
    }}
    header nav.jump {{ flex-basis: 100%; margin: 0; padding: 0; }}
    .brand {{ display: flex; align-items: baseline; gap: 12px; }}
    .brand h1 {{ font-size: 1.25rem; font-weight: 800; letter-spacing: -0.01em; }}
    .brand .logo {{ font-size: 1.5rem; }}
    .meta {{ font-size: 0.78rem; color: var(--muted); margin-left: auto; }}
    .meta strong {{ color: var(--ink); }}
    .theme-toggle {{
      background: var(--accent-soft); color: var(--ink);
      border: none; cursor: pointer;
      padding: 7px 12px; border-radius: 6px;
      font-size: 0.85rem; font-family: inherit;
    }}
    .theme-toggle:hover {{ background: var(--accent); color: var(--panel); }}
    .courier-link {{
      background: var(--accent); color: var(--panel);
      border: none; cursor: pointer;
      padding: 7px 14px; border-radius: 6px;
      font-size: 0.85rem; font-family: inherit;
      text-decoration: none;
      display: inline-block;
      font-weight: 500;
      transition: all 0.15s;
    }}
    .courier-link:hover {{ background: var(--ink); color: var(--panel); box-shadow: var(--shadow-hover); }}
    .map-link {{
      background: var(--accent-soft); color: var(--ink);
      border: none; cursor: pointer;
      padding: 7px 12px; border-radius: 6px;
      font-size: 0.85rem; font-family: inherit; font-weight: 500;
      text-decoration: none;
      transition: all 0.15s;
    }}
    .map-link:hover {{ background: var(--accent); color: var(--panel); }}
    .search-box {{
      flex: 1;
      min-width: 200px;
      max-width: 400px;
      position: relative;
    }}
    .search-box input {{
      width: 100%;
      padding: 8px 36px 8px 12px;
      border: 1px solid var(--accent-soft);
      border-radius: 8px;
      background: var(--panel);
      color: var(--ink);
      font-size: 0.85rem;
      font-family: inherit;
      outline: none;
    }}
    .search-box input:focus {{ border-color: var(--accent); }}
    .search-box .clear-btn {{
      display: none;
      position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
      background: var(--accent-soft); border: none;
      font-size: 0.7rem; cursor: pointer;
      width: 20px; height: 20px; border-radius: 50%;
      align-items: center; justify-content: center; color: var(--muted);
    }}
    .search-box.has-text .clear-btn {{ display: flex; }}
    .search-results {{ font-size: 0.78rem; color: var(--muted); }}
    nav.jump {{ display: flex; gap: 6px; flex-wrap: wrap; }}
    nav.jump a {{
      font-size: 0.74rem;
      padding: 3px 12px;
      background: var(--accent-soft);
      border-radius: 999px;
      color: var(--ink);
      text-decoration: none;
      font-weight: 500;
      transition: 0.15s;
    }}
    nav.jump a:hover {{ background: var(--accent); color: var(--panel); }}
    nav.jump .today-pill {{ background: #d9531e; color: #fff; }}
    nav.jump .today-pill:hover {{ background: #b84319; }}
    nav.jump .new-pill {{ background: var(--new-bg); color: #fff; }}
    nav.jump .new-pill:hover {{ background: #1f5a1f; }}
    .new-badge {{
      position: absolute; top: 10px; left: 10px;
      background: var(--new-bg); color: #fff;
      font-size: 0.65rem; font-weight: 800;
      padding: 4px 8px; border-radius: 4px;
      letter-spacing: 0.06em;
      box-shadow: 0 2px 6px rgba(0,0,0,0.2);
    }}
    footer {{
      text-align: center;
      font-size: 0.8rem;
      color: var(--muted);
      padding: 32px 24px 24px;
      border-top: 1px solid var(--accent-soft);
    }}
    footer a {{ color: var(--accent); text-decoration: none; }}

    #main-layout {{
      display: flex;
      flex: 1;
      min-height: 0;
    }}
    #map-panel {{
      width: 320px;
      min-width: 320px;
      position: relative;
      border-right: 1px solid var(--accent-soft);
      flex-shrink: 0;
    }}
    #map-panel #map {{ width: 100%; height: 100%; }}
    #map-panel .map-label {{
      position: absolute;
      top: 8px; left: 50%;
      transform: translateX(-50%);
      z-index: 1000;
      background: rgba(0,0,0,0.6);
      color: #fff;
      font-size: 0.7rem;
      padding: 5px 14px;
      border-radius: 8px;
      pointer-events: none;
      white-space: nowrap;
      text-align: center;
    }}
    #cards-area {{
      flex: 1;
      overflow-y: auto;
      padding: 0 24px 40px;
    }}
    #cards-area section {{
      max-width: 1400px;
      margin: 36px auto 0;
      padding: 0;
    }}
    #cards-area section.local-section {{
      background: var(--local-bg);
      border-left: 4px solid var(--local-border);
      padding: 28px 24px 32px;
      border-radius: var(--radius);
      margin: 36px 0 0;
    }}
    #cards-area section.today-section {{
      background: linear-gradient(180deg, rgba(217, 83, 30, 0.08), rgba(217, 83, 30, 0.02));
      border-left: 4px solid #d9531e;
      padding: 28px 24px 32px;
      border-radius: var(--radius);
      margin: 36px 0 0;
    }}
    #cards-area section h2 {{
      font-size: 1.1rem;
      margin-bottom: 18px;
      font-weight: 700;
    }}
    #cards-area section h2 .count {{
      font-size: 0.82rem;
      font-weight: 500;
      color: var(--muted);
    }}
    .masonry {{ column-count: 4; column-gap: 18px; }}
    @media (max-width: 1300px) {{ .masonry {{ column-count: 3; }} }}
    @media (max-width: 1000px) {{ .masonry {{ column-count: 2; }} }}
    @media (max-width: 600px)  {{ .masonry {{ column-count: 1; }} }}
    .local-section .masonry {{ column-count: 3; }}
    @media (max-width: 1300px) {{ .local-section .masonry {{ column-count: 2; }} }}
    @media (max-width: 800px)  {{ .local-section .masonry {{ column-count: 1; }} }}
    .card {{
      display: inline-block; width: 100%;
      margin: 0 0 18px;
      background: var(--panel);
      border-radius: var(--radius);
      overflow: hidden;
      text-decoration: none; color: inherit;
      box-shadow: var(--shadow);
      transition: transform 0.18s ease, box-shadow 0.18s ease;
      break-inside: avoid;
    }}
    .card:hover {{ transform: translateY(-3px); box-shadow: var(--shadow-hover); }}
    .card-img {{ position: relative; width: 100%; line-height: 0; background: var(--accent-soft); }}
    .card-img img {{ width: 100%; height: auto; display: block; }}
    .no-img {{
      aspect-ratio: 4/3; display: flex; align-items: center;
      justify-content: center; font-size: 0.8rem; color: var(--muted);
    }}
    .card-body {{ padding: 12px 14px 14px; }}
    .card-body .title {{
      font-size: 0.82rem; font-weight: 600;
      line-height: 1.4; margin-bottom: 6px;
      display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
      overflow: hidden;
    }}
    .house-line {{ font-size: 0.74rem; color: var(--muted); line-height: 1.3; }}
    .house {{
      position: relative; cursor: pointer;
      border-bottom: 1px dotted var(--muted);
    }}
    .house:hover {{ color: var(--accent); border-bottom-color: var(--accent); }}
    .house.highlighted {{ color: var(--highlight); border-bottom-color: var(--highlight); }}
    .house.unknown {{ opacity: 0.7; }}
    .pc {{
      display: inline-block; margin-left: 4px;
      background: var(--accent-soft); color: var(--accent);
      padding: 1px 6px; border-radius: 4px;
      font-size: 0.65rem; font-weight: 600;
    }}
    .card-body .meta {{
      display: flex; align-items: center;
      gap: 10px; margin-top: 6px;
      font-size: 0.72rem;
    }}
    .lot-number {{
      background: var(--ink); color: var(--panel);
      padding: 1px 8px; border-radius: 4px;
      font-weight: 600; font-size: 0.65rem;
    }}
    .estimate {{ font-weight: 600; color: var(--accent); }}
    .saledate {{ color: var(--muted); }}

    /* ── MAP PIN STYLES ── */
    .pin-default {{
      width: 8px; height: 8px;
      background: transparent;
      border: none;
      border-radius: 50%;
      opacity: 0;
      transition: all 0.2s;
    }}
    .pin-highlighted {{
      width: 16px; height: 16px;
      background: #d9531e;
      border: 3px solid #fff;
      border-radius: 50%;
      box-shadow: 0 0 12px rgba(217,83,30,.6);
    }}

    @media (max-width: 800px) {{
      #map-panel {{ display: none; }}
      #cards-area {{ padding: 0 12px 40px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="brand"><span class="logo">🩵</span><h1>Pinefinders Auction Finds</h1></div>
    <div class="search-box" id="searchBox">
      <input type="text" id="searchInput" placeholder="🔍 Search items (e.g. bedside cupboard, chest of drawers...)" oninput="searchItems()">
      <button class="clear-btn" onclick="clearSearch()" title="Clear search">✕</button>
    </div>
    <span class="search-results" id="searchResults"></span>
    <span class="meta"><strong>{total} lots</strong> · {today_total} today · {new_total} new since yesterday · Updated {now}</span>
    <a href="https://my.proovia.delivery/dashboard" target="_blank" rel="noopener" class="courier-link">🚚 Proovia Couriers</a>
    <a href="https://pinefinders.github.io/auction-map/" target="_blank" rel="noopener" class="map-link">🗺 Map</a>
    <button class="theme-toggle" onclick="toggleTheme()" id="themeBtn">🌙 Dark</button>
    <nav class="jump">
      <a href="#local">📍 Local · {local_local_count}</a>
      {f'<a class="today-pill" href="#today">🔥 UK Today · {wide_today_count}</a>' if wide_today_count else ''}
      <a href="#uk-wide">🇬🇧 UK Later · {wide_later_count}</a>
      {f'<a class="new-pill" href="#" onclick="filterNew(); return false;">✨ {new_total} new</a>' if new_total else ''}
    </nav>
  </header>

  <div id="main-layout">
    <div id="map-panel">
      <div id="map"></div>
      <div class="map-label">Hover name for map location</div>
    </div>
    <div id="cards-area">
      {local_html}
      {today_html}
      {later_html}
    </div>
  </div>

  <footer>
    Pinefinders Old Pine Furniture Warehouse · search terms: {terms_str}<br>
    <a href="https://pinefinders.github.io/auction-finds-map">pinefinders.github.io/auction-finds-map</a>
  </footer>

  <script>
{pc_map_js}

    // ── THEME / SEARCH ──
    function toggleTheme() {{
      const html = document.documentElement;
      const cur = html.getAttribute('data-theme') ||
        (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
      const next = cur === 'dark' ? 'light' : 'dark';
      html.setAttribute('data-theme', next);
      localStorage.setItem('pf-theme', next);
      updateThemeBtn();
    }}
    function updateThemeBtn() {{
      const btn = document.getElementById('themeBtn');
      const isDark = (document.documentElement.getAttribute('data-theme') === 'dark') ||
        (!document.documentElement.getAttribute('data-theme') &&
         window.matchMedia('(prefers-color-scheme: dark)').matches);
      btn.textContent = isDark ? '☀️ Light' : '🌙 Dark';
    }}
    const saved = localStorage.getItem('pf-theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
    updateThemeBtn();

    let newOnly = false;
    function filterNew() {{
      newOnly = !newOnly;
      document.querySelectorAll('.card').forEach(c => {{
        const isNew = c.querySelector('.new-badge');
        c.style.display = (newOnly && !isNew) ? 'none' : '';
      }});
    }}

    function normalizeSearch(text) {{
      return text
        .replace(/cupboards?/gi, 'cupboard')
        .replace(/chests?/gi, 'chest')
        .replace(/drawers?/gi, 'drawer')
        .replace(/tables?/gi, 'table')
        .replace(/chairs?/gi, 'chair')
        .replace(/cabinets?/gi, 'cabinet')
        .replace(/bedsides?/gi, 'bedside')
        .replace(/wardrobes?/gi, 'wardrobe')
        .replace(/dressers?/gi, 'dresser')
        .replace(/shelves?/gi, 'shelf')
        .replace(/bookcase(s)?/gi, 'bookcase');
    }}

    function searchItems() {{
      const input = document.getElementById('searchInput');
      const query = input.value.toLowerCase().trim();
      const searchBox = document.getElementById('searchBox');
      const resultsEl = document.getElementById('searchResults');
      if (query) {{ searchBox.classList.add('has-text'); }}
      else {{ searchBox.classList.remove('has-text'); }}
      if (!query) {{
        document.querySelectorAll('.card').forEach(c => {{
          if (newOnly && !c.querySelector('.new-badge')) {{ c.style.display = 'none'; }}
          else {{ c.style.display = ''; }}
        }});
        resultsEl.textContent = '';
        return;
      }}
      const normalizedQuery = normalizeSearch(query);
      let visibleCount = 0, totalCount = 0;
      document.querySelectorAll('.card').forEach(card => {{
        totalCount++;
        const titleEl = card.querySelector('.title');
        if (!titleEl) return;
        const title = titleEl.textContent.toLowerCase();
        const normalizedTitle = normalizeSearch(title);
        const queryWords = normalizedQuery.split(/\s+/);
        const matches = queryWords.every(word => normalizedTitle.includes(word));
        if (matches) {{
          if (newOnly && !card.querySelector('.new-badge')) {{ card.style.display = 'none'; }}
          else {{ card.style.display = ''; visibleCount++; }}
        }} else {{ card.style.display = 'none'; }}
      }});
      resultsEl.innerHTML = '<strong>' + visibleCount + '</strong> of ' + totalCount + ' lots';
    }}
    function clearSearch() {{
      document.getElementById('searchInput').value = '';
      searchItems();
      document.getElementById('searchInput').focus();
    }}

    // ── MAP ──
    const map = L.map('map', {{ center: [54.2, -2.5], zoom: 6, zoomControl: true }});
    L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
      attribution: '&copy; OSM &copy; CARTO',
      subdomains: 'abcd',
      maxZoom: 19
    }}).addTo(map);

    let markers = {{}};
    let highlightedMarker = null;

    for (const pc in PC_MAP) {{
      const h = PC_MAP[pc];
      const m = L.marker([h.lat, h.lng], {{
        icon: L.divIcon({{
          className: '',
          html: '<div class="pin-default"></div>',
          iconSize: [8, 8],
          iconAnchor: [4, 4]
        }})
      }});
      m.bindTooltip(h.name, {{ direction: 'top', offset: [0, -8] }});
      m.addTo(map);
      markers[pc] = m;
    }}

    function highlightMarker(pc) {{
      if (highlightedMarker) {{
        highlightedMarker.setIcon(L.divIcon({{
          className: '',
          html: '<div class="pin-default"></div>',
          iconSize: [8, 8],
          iconAnchor: [4, 4]
        }}));
        const prev = document.querySelector('.house.highlighted');
        if (prev) prev.classList.remove('highlighted');
      }}
      if (!pc || !markers[pc]) {{ highlightedMarker = null; return; }}
      const m = markers[pc];
      m.setIcon(L.divIcon({{
        className: '',
        html: '<div class="pin-highlighted"></div>',
        iconSize: [16, 16],
        iconAnchor: [8, 8]
      }}));
      highlightedMarker = m;
    }}

    document.querySelectorAll('.house').forEach(el => {{
      const pcRaw = el.querySelector('.pc');
      if (!pcRaw) return;
      const pc = pcRaw.textContent.trim().replace(/\s+/g, '').toUpperCase();
      el.addEventListener('mouseenter', () => {{
        el.classList.add('highlighted');
        highlightMarker(pc);
      }});
      el.addEventListener('mouseleave', () => {{
        el.classList.remove('highlighted');
        highlightMarker(null);
      }});
    }});

    const allLats = Object.values(PC_MAP).map(h => h.lat);
    const allLngs = Object.values(PC_MAP).map(h => h.lng);
    const bounds = [[Math.min(...allLats), Math.min(...allLngs)], [Math.max(...allLats), Math.max(...allLngs)]];
    map.fitBounds(bounds, {{ padding: [30, 30] }});
    if (map.getZoom() > 8) map.setZoom(8);
  </script>
</body>
</html>"""


def sweep_orphan_images(all_lots):
    """Delete image files in IMAGES_DIR not referenced by any current lot.
    A file only becomes orphaned AFTER its lot has left the data (i.e. the
    auction ended and the scrape no longer returns it), so this keeps
    images/ aligned with live lots. seen_lots.json is never touched.
    """
    referenced = {lot["img_file"] for lot in all_lots.values() if lot.get("img_file")}
    removed = 0
    freed = 0
    for p in IMAGES_DIR.iterdir():
        if p.is_file() and p.name not in referenced:
            try:
                freed += p.stat().st_size
                p.unlink()
                removed += 1
            except OSError as e:
                log.warning(f"Could not remove orphan {p.name}: {e}")
    log.info(f"Orphan sweep: removed {removed} images ({freed/1e6:.1f} MB freed)")


def git_push(repo_dir):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    for cmd in [
        ["git", "-C", str(repo_dir), "add", "-A"],
        ["git", "-C", str(repo_dir), "commit", "-m", f"Auto update: {now_str}"],
        ["git", "-C", str(repo_dir), "push"],
    ]:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            if "nothing to commit" in result.stdout + result.stderr:
                log.info("Git: nothing to commit")
                return
            log.warning(f"Git failed: {' '.join(cmd)}\n{result.stderr}")
            return
    log.info("Git: pushed successfully")


# --- Sale-date enrichment -------------------------------------------------
# Sale-date strings come in several flavours:
#   Timed:  "Ends Sun 24th May 2026 from 2pm BST"
#   Live:   "Mon 25th May 2026 10am BST (Lots 1001 to 1502) Tue 26th May 2026 10am BST ..."
# We capture the full block for the future, and a short summary for display.
_SALE_DATE_RE = re.compile(
    r'((?:Ends\s+)?(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{1,2}\w{0,2}\s+'
    r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2}'
    r'(?:\s+(?:from\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:GMT|BST)?)?)',
    re.IGNORECASE,
)


def fetch_sale_dates(session, sample_lot_url):
    """Fetch one lot page from an auction, return (summary, raw_block).
    summary = first date string, e.g. 'Sun 24th May 2026 from 2pm BST'
    raw_block = the entire 'Sale Dates: ...' text, for the tooltip.
    """
    try:
        r = session.get(sample_lot_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.debug(f"sale_dates fetch failed: {e}")
        return ("", "")

    soup = BeautifulSoup(r.text, "html.parser")
    label = soup.find(string=re.compile(r'Sale Dates?:', re.IGNORECASE))
    if not label:
        return ("", "")
    block = label.parent.parent if label.parent else None
    if not block:
        return ("", "")
    raw = re.sub(r'\s+', ' ', block.get_text(' ', strip=True))
    raw = re.sub(r'^Sale Dates?:\s*', '', raw, flags=re.IGNORECASE).strip()

    # First date string from the block
    m = _SALE_DATE_RE.search(raw)
    summary = m.group(1).strip() if m else raw[:80]
    return (summary, raw)


def enrich_with_sale_dates(session, all_lots):
    """For each unique auction_id, fetch one lot's page and apply the sale-date
    info to every lot in that auction."""
    # Group lots by auction_id
    by_auction = {}
    for lot in all_lots.values():
        aid = lot.get("auction_id") or ""
        if not aid:
            continue
        by_auction.setdefault(aid, []).append(lot)

    log.info(f"Fetching sale dates for {len(by_auction)} auctions…")
    for i, (aid, lots) in enumerate(by_auction.items(), 1):
        sample = lots[0]
        summary, raw = fetch_sale_dates(session, sample["url"])
        for lot in lots:
            lot["sale_date"] = summary
            lot["sale_dates_raw"] = raw
        if i % 25 == 0:
            log.info(f"  sale-dates progress: {i}/{len(by_auction)}")
        time.sleep(REQUEST_DELAY)


def main():
    log.info("=== Pinefinders Auction Finds — starting ===")
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    session  = requests.Session()
    all_lots = {}

    for term in SEARCH_TERMS:
        log.info(f"Searching: '{term}'")
        for lot in scrape_term(session, term):
            if lot["id"] not in all_lots:
                all_lots[lot["id"]] = lot
        if len(all_lots) >= MAX_LOTS:
            log.info(f"Cap reached ({MAX_LOTS}) — stopping")
            break

    log.info(f"Total unique lots: {len(all_lots)}")

    enrich_with_sale_dates(session, all_lots)

    log.info("Downloading images…")
    for lot in all_lots.values():
        if lot["img_url"] and lot["img_file"]:
            download_image(lot["img_url"], IMAGES_DIR / lot["img_file"])
            time.sleep(0.3)

    local_lots = [l for l in all_lots.values() if l["local"]]
    wide_lots  = [l for l in all_lots.values() if not l["local"]]
    log.info(f"Local: {len(local_lots)}  UK-wide: {len(wide_lots)}")

    # Load previously-seen lot IDs and postcode lookup
    seen = load_seen()
    postcodes = load_postcodes()
    new_count = sum(1 for lot_id in all_lots if lot_id not in seen)
    overlap   = len(seen.intersection(all_lots))
    log.info(f"Seen-before: {overlap}  New since last run: {new_count}")
    log.info(f"Postcode lookup: {len(postcodes[0])} houses")

    (REPO_DIR / "index.html").write_text(
        build_html(local_lots, wide_lots, seen=seen, postcodes=postcodes),
        encoding="utf-8",
    )
    (REPO_DIR / "data.json").write_text(
        json.dumps(list(all_lots.values()), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("HTML written")

    # Update seen_lots.json with this run's lot IDs (union, capped at 5000)
    updated_seen = (seen | set(all_lots.keys()))
    # Keep the file from growing unboundedly: prefer recent IDs
    if len(updated_seen) > 5000:
        updated_seen = set(list(all_lots.keys())) | set(list(seen))[: 5000 - len(all_lots)]
    save_seen(updated_seen)
    log.info(f"Updated seen_lots.json ({len(updated_seen)} ids)")

    # Keep images/ aligned with live lots (delete ended-auction leftovers)
    sweep_orphan_images(all_lots)

    log.info("Pushing to GitHub…")
    git_push(REPO_DIR)
    log.info("=== Done ===")


if __name__ == "__main__":
    main()
