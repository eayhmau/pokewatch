"""
PokeWatch / stock watcher (GitHub Actions edition)
--------------------------------------------------
Runs from GitHub's servers every ~10 min, 24/7. Two kinds of source:

1. WALL-FINGERPRINT sources (Pokemon Center): bots never see the real site
   (Imperva bot wall), but the WALL changes mode around releases, so we
   fingerprint it and alert when the mode changes.
     level 0 = normal wall (nothing happening)
     level 1 = wall changed mode (maintenance/queue/lockdown - a drop may be near)
     level 2 = real page reached (wall down)

2. JSON-API sources (Weeztix/OpenTicket ticket shop): the shop exposes a public
   data endpoint with each ticket's real status ("sold_out" / "available"), so
   we read it directly and alert the moment a ticket is buyable.
     level 0 = all sold out (baseline)
     level 2 = at least one ticket AVAILABLE (go go go)

Alerts fire only when a source's state CHANGES between runs.
"""

import json
import os
import sys
import time
from datetime import datetime
import re
import urllib.request

# --- Wall-fingerprint sources (fetch HTML, classify the bot wall) ---
# We watch the new-releases category (where any drop first appears) plus the home
# page. From GitHub's datacenter IPs the bot wall blocks the real content, so this
# detects the wall changing mode (maintenance/lockdown = a drop is likely near),
# not individual products.
POKEMON_LINK = "https://www.pokemoncenter.com/en-gb/category/new-releases"
WALL_URLS = {
    "pokemon-newreleases": POKEMON_LINK,
    "pokemon-home": "https://www.pokemoncenter.com/",
}

# --- JSON-API sources (Weeztix/OpenTicket shops: name -> (data_url, shop_url)) ---
WEEZTIX_SHOPS = {
    "weeztix-bcgfest-utrecht": (
        "https://shop.api.openticket.tech/2267989d-8cc0-11f0-a9cb-7e126431635e/data",
        "https://shop.weeztix.com/2267989d-8cc0-11f0-a9cb-7e126431635e/tickets",
    ),
}

# --- HTML sources (organizedplay.events: server-rendered pages, name -> url) ---
# Pre-sale the ticket shows text "Not Available"; when a buyable quantity opens,
# a <select id="quantity-..."> dropdown is rendered - that's the availability signal.
OPE_EVENTS = {
    "ope-opcg-grandbattle-bristol": "https://tickets.organizedplay.events/Event/Index/179",
    "ope-opcg-regionals-bristol": "https://tickets.organizedplay.events/Event/Index/178",
    "ope-opcg-regionals-london": "https://tickets.organizedplay.events/Event/Index/175",
}

# --- Bandai Fest pages: registration links show "<Link Coming Soon>" until they
# go live (announced Oct 25, 9:00am GMT). We alert when a placeholder becomes a
# real link, i.e. the "coming soon" count drops below the current baseline.
BANDAI_PAGES = {
    "bandai-london-prereg": (
        "https://www.bandaicardgames-fest.com/26-27/en/london/news/london-pre-registrationd.html",
        2,  # baseline count of "coming soon" placeholders
    ),
}

# Extra text appended to a source's alert (e.g. your TCG+ ID ready to paste at checkout).
SOURCE_NOTES = {
    "ope-opcg-regionals-london": "🪪 Your TCG+ ID (copy at checkout): `0000777121`",
}

STATE_FILE = "state.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, "FETCH-ERROR: " + str(e)


def classify_wall(status, body):
    """Fingerprint the bot wall. Returns (state_name, signal_level)."""
    b = body.lower()
    if "schema.org" in b and '"availability"' in b:
        m = re.search(r'"availability"\s*:\s*"https?://schema\.org/(\w+)"', b)
        return "REAL-PAGE:" + (m.group(1) if m else "unknown"), 2
    if status == 503 or "scheduled maintenance" in b:
        return "maintenance", 1
    if "queue-it" in b or "queue_it" in b or "softblock" in b:
        return "queue", 1
    if "main-iframe" in b or "request unsuccessful" in b:
        return "challenge-iframe (lockdown mode)", 1
    if "_incapsula_resource" in b or "distil" in b:
        return "challenge-light (normal wall)", 0
    if "error 17" in b or "access denied" in b:
        return "hard-block (normal for bots)", 0
    if status == 403:
        # GitHub runner IPs get a plain 403 block on normal days - that's baseline.
        return "blocked-403 (normal for datacenter IPs)", 0
    if status == 0:
        return "fetch-error", -1
    return "other-http-" + str(status), 1


# Explicit buyable statuses - only these trigger an "available" alert. Anything
# else (sold_out, empty, unknown) does NOT, so degraded API responses can't false-alarm.
BUYABLE_STATUSES = {"available", "on_sale", "onsale", "for_sale", "in_stock"}


def classify_weeztix(status, body):
    """Read the OpenTicket data JSON. Returns (state_name, signal_level).
    level -1 means 'unreliable read' - caller keeps the previous state, no alert."""
    if status != 200:
        return "api-http-" + str(status), -1
    try:
        data = json.loads(body)
    except ValueError:
        return "api-bad-json", -1
    tickets = data.get("tickets", {})
    if not isinstance(tickets, dict):
        return "api-unexpected-shape", -1
    # Only entries with a real name AND status are trustworthy; the queue layer
    # sometimes returns blank/placeholder entries.
    meaningful = [t for t in tickets.values()
                  if isinstance(t, dict) and t.get("name") and t.get("status")]
    if not meaningful:
        return "api-degraded-response", -1
    available = [t["name"] for t in meaningful
                 if str(t.get("status", "")).lower() in BUYABLE_STATUSES]
    if available:
        return "AVAILABLE: " + ", ".join(available), 2
    return f"all sold out ({len(meaningful)} tickets)", 0


def classify_ope(status, body):
    """Read an organizedplay.events ticket page. Returns (state_name, level).
    level -1 means 'unreliable read' - caller keeps the previous state, no alert."""
    if status != 200:
        return "http-" + str(status), -1
    lc = body.lower()
    if "grand battle ticket" not in lc and "ticket-option" not in lc:
        return "page-structure-changed", 1  # markup changed - worth a manual look
    # A rendered quantity <select id="quantity-..."> only appears when buyable stock is open.
    if re.search(r'id=["\']quantity-', lc):
        return "AVAILABLE (buy dropdown live)", 2
    if "sold out" in lc:
        return "sold out", 0
    if "not available" in lc:
        return "not on sale yet", 0
    return "unknown-state", 1


def classify_bandai(baseline):
    """Return a classifier for a Bandai Fest page. Alerts when the number of
    '<Link Coming Soon>' placeholders drops below baseline (a link went live)."""
    def _classify(status, body):
        if status != 200:
            return "http-" + str(status), -1
        lc = body.lower()
        if "pre-registration" not in lc and "bandai card games fest" not in lc:
            return "page-structure-changed", 1
        n = lc.count("coming soon")
        if n < baseline:
            return f"LINKS OPENING ({n} of {baseline} placeholders left)", 2
        return f"pending ({n}x coming soon)", 0
    return _classify


def send_discord(content, webhook_env="DISCORD_WEBHOOK"):
    webhook = os.environ.get(webhook_env, "").strip()
    if not webhook:
        print(f"WARNING: {webhook_env} secret not set - cannot notify!")
        return
    data = json.dumps({"content": content, "username": "StockWatch"}).encode()
    req = urllib.request.Request(
        webhook, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "StockWatch"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("Discord:", r.status)
    except Exception as e:
        print("Discord send failed:", e)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8-sig") as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def collect(old, verbose=True):
    """Fetch every source once; return the new state dict (with carry-forward)."""
    new = {}

    def record(name, url, classify, kind, link):
        status, body = fetch(url)
        state, level = classify(status, body)
        if level < 0:
            # Unreliable read (network error / degraded API): keep last good state,
            # don't alert. Fall back to a neutral entry if we've never had one.
            prev = old.get(name)
            new[name] = prev if prev else {"state": "pending-first-read", "level": 0,
                                           "http": status, "kind": kind, "link": link}
            if verbose:
                print(f"{name}: HTTP {status} -> {state} (unreliable, keeping last state)")
        else:
            new[name] = {"state": state, "level": level, "http": status,
                         "kind": kind, "link": link}
            if verbose:
                print(f"{name}: HTTP {status} -> {state} (level {level})")

    for name, url in WALL_URLS.items():
        record(name, url, classify_wall, "wall", url)
    for name, (data_url, shop_url) in WEEZTIX_SHOPS.items():
        record(name, data_url, classify_weeztix, "weeztix", shop_url)
    for name, url in OPE_EVENTS.items():
        record(name, url, classify_ope, "ope", url)
    for name, (url, baseline) in BANDAI_PAGES.items():
        record(name, url, classify_bandai(baseline), "bandai", url)
    return new


def alert(old, new):
    """Send Discord messages for any source whose state changed vs `old`."""
    wall_lines, wall_max = [], 0
    for name, cur in new.items():
        prev = old.get(name, {})
        if not (prev and (prev.get("state") != cur["state"] or prev.get("level") != cur["level"])):
            continue
        if cur["kind"] in ("weeztix", "ope", "bandai"):
            if cur["level"] >= 2:
                if cur["kind"] == "bandai":
                    head = "📝 @everyone **PRE-REGISTRATION OPENING!** " + cur["state"]
                else:
                    head = "🎟️ @everyone **TICKETS AVAILABLE!** " + cur["state"]
            else:
                head = "👀 **StockWatch:** status changed — " + cur["state"]
            note = SOURCE_NOTES.get(name)
            msg = f"{head}\n(was: `{prev.get('state')}`)\n{cur['link']}"
            if note:
                msg += "\n" + note
            send_discord(msg, webhook_env="OPTCG_WEEZTIX")
        else:  # wall
            wall_lines.append(f"**{name}**: `{prev.get('state')}` -> `{cur['state']}` (HTTP {cur['http']})")
            wall_max = max(wall_max, cur["level"])

    if wall_lines:
        if wall_max >= 2:
            head = "🚨 @everyone **THE WALL IS DOWN — real page visible to bots! GO CHECK NOW!**"
        elif wall_max == 1:
            head = "🛡️ @everyone **DROP SIGNAL — Pokemon Center's bot wall changed mode.** Go check manually!"
        else:
            head = "👀 **StockWatch:** Pokemon wall returned to normal mode."
        send_discord(head + "\n" + "\n".join(wall_lines) + "\n" + POKEMON_LINK)


def run_once():
    old = load_state()
    new = collect(old)
    alert(old, new)
    save_state(new)


def run_loop():
    """Check every INTERVAL seconds in-memory for ~LOOP_SECONDS, then exit so the
    workflow can hand off to a fresh run. State persists via state.json only at
    start (baseline) and end (updated baseline) - not every tick - so no git spam."""
    interval = int(os.environ.get("INTERVAL", "20"))
    total = int(os.environ.get("LOOP_SECONDS", "3300"))  # ~55 min
    state = load_state()
    deadline = time.time() + total
    n = 0
    print(f"Loop mode: every {interval}s for ~{total // 60} min")
    while time.time() < deadline:
        n += 1
        new = collect(state, verbose=False)
        alert(state, new)
        state = new
        # heartbeat every ~5 min so the run log shows progress
        if n % max(1, (300 // interval)) == 1:
            ticket = state.get("ope-opcg-regionals-bristol", {}).get("state", "?")
            print(f"[{datetime.now():%H:%M:%S}] tick {n}: regionals={ticket}", flush=True)
        time.sleep(interval)
    save_state(state)
    print(f"Loop done after {n} checks; state saved.")


if __name__ == "__main__":
    if "--loop" in sys.argv:
        run_loop()
    else:
        run_once()
