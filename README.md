# PokeWatch — GitHub Actions edition

Watches pokemoncenter.com **from GitHub's servers** around the clock — your PC
can be off. Bots can't see the real site (Imperva wall), but the wall itself
changes mode around releases, so this watches the wall:

| What bots see | Meaning | Discord |
|---|---|---|
| tiny JS challenge / hard block | normal day | silence |
| iframe challenge / maintenance / queue page | **release may be near** | 🛡️ @everyone drop signal |
| actual product page | wall is down — definitely go look | 🚨 @everyone |

It checks every ~10 minutes and only messages when the mode **changes**.

## Setup (one time, ~3 minutes)

1. Create a **new private repo** on github.com (name it e.g. `pokewatch`).
2. Upload everything in this folder to the repo, keeping the folder structure
   (`check.py`, `README.md`, and `.github/workflows/pokewatch.yml`).
   Easiest way: on the repo page, **Add file → Upload files**, drag the files in.
   NOTE: the `.github/workflows/` path matters — if uploading via the web UI,
   create the file by going to **Add file → Create new file** and typing
   `.github/workflows/pokewatch.yml` as the name, then paste the content.
3. Add your Discord webhook: repo **Settings → Secrets and variables →
   Actions → New repository secret**, name it `DISCORD_WEBHOOK`, paste the
   webhook URL as the value.
4. Go to the **Actions** tab → enable workflows → open **PokeWatch** →
   **Run workflow** to do a first manual run and confirm it's green.

That's it. It now runs every ~10 minutes forever, for free.

## Notes

* The first run establishes the baseline silently; alerts start from the
  second run onward (only on changes).
* GitHub pauses scheduled workflows after ~60 days without repo activity —
  if you get a "workflow disabled" email, just click re-enable.
* To watch a different product, change the URL at the top of `check.py`.
* To test Discord wiring: Actions tab → PokeWatch → Run workflow, then check
  the run logs for "Discord: 204".
