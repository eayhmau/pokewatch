"""
PokeWatch (GitHub Actions edition)
----------------------------------
Polls pokemoncenter.com from GitHub's servers. Bots never see the real site
(Imperva bot wall), but the WALL ITSELF changes mode around releases:

  * normal times  -> tiny ~212-byte JS challenge page
  * maintenance   -> bigger iframe challenge / maintenance page  (observed 2026-06-10)
  * big drops     -> queue pages, hard blocks, other shifts

So this script fingerprints the wall and pings Discord when the mode changes.
Signal levels:
  0 = NORMAL  (usual bot wall - nothing happening)
  1 = SIGNAL  (wall changed mode - maintenance/queue/lockdown: a release may be near)
  2 = OPEN    (real product page reached - the wall is down for bots, definitely go look)
"""

import json
import os
import re
import urllib.request

URLS = {
    "product": (
        "https://www.pokemoncenter.com/en-gb/en-gb/product/10-10416-109/"
        "pokemon-tcg-mega-evolution-pitch-black-pokemon-center-elite-trainer-box/"
    ),
    "home": "https://www.pokemoncenter.com/",
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


def classify(status, body):
    """Returns (state_name, signal_level)."""
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
    if status == 0:
        return "fetch-error", 0
    return "other-http-" + str(status), 1


def send_discord(content):
    webhook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not webhook:
        print("WARNING: DISCORD_WEBHOOK secret not set - cannot notify!")
        return
    data = json.dumps({"content": content, "username": "PokeWatch GitHub"}).encode()
    req = urllib.request.Request(
        webhook, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "PokeWatch"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("Discord:", r.status)
    except Exception as e:
        print("Discord send failed:", e)


def main():
    old = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            old = json.load(f)

    new, lines = {}, []
    for name, url in URLS.items():
        status, body = fetch(url)
        state, level = classify(status, body)
        new[name] = {"state": state, "level": level, "http": status, "bytes": len(body)}
        print(f"{name}: HTTP {status}, {len(body)} bytes -> {state} (level {level})")

        o = old.get(name, {})
        if o and (o.get("level") != level or o.get("state") != state):
            lines.append(
                f"**{name}**: `{o.get('state')}` -> `{state}` (HTTP {status})"
            )

    if lines:
        max_level = max(v["level"] for v in new.values())
        if max_level >= 2:
            head = "🚨 @everyone **THE WALL IS DOWN — real page visible to bots! GO CHECK NOW!**"
        elif max_level == 1:
            head = "🛡️ @everyone **DROP SIGNAL — Pokemon Center's bot wall changed mode.** Go check the site manually!"
        else:
            head = "👀 **PokeWatch:** wall returned to normal mode."
        send_discord(head + "\n" + "\n".join(lines) + "\n" + URLS["product"])

    with open(STATE_FILE, "w") as f:
        json.dump(new, f, indent=2)


if __name__ == "__main__":
    main()
