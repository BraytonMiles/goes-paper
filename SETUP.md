# GOES → EE02, hosted for $0

Renders a live GOES-West frame every 10 minutes, chroma-boosted and dithered for
the 13.3" Spectra 6 panel, and publishes it as a static file your ePaper board polls.

No server, no credit card, no VM to patch. GitHub Actions does the rendering;
GitHub Pages serves the result.

---

## 1. Create the repo — it must be public

```bash
mkdir goes-epaper && cd goes-epaper
git init -b main
# copy in: goes_epaper.py, .github/workflows/render.yml, SETUP.md
git add -A
git commit -m "GOES ePaper renderer"
gh repo create goes-epaper --public --source=. --push
```

**Public is not optional.** Public repos get unlimited Actions minutes. A private
repo would burn roughly 4,300 minutes a month against a 2,000-minute allowance and
stop partway through the month.

## 2. Turn on Pages

Repo → **Settings → Pages → Source: Deploy from a branch → `gh-pages` / `root`**.

The branch won't exist until the first run, so either wait for the schedule or
kick it off now: **Actions → render → Run workflow**. Set the Pages source once
the branch appears.

## 3. Confirm it published

```
https://<user>.github.io/<repo>/latest.sha           # 16 hex chars
https://<user>.github.io/<repo>/latest.bin           # exactly 960000 bytes
https://<user>.github.io/<repo>/latest_preview.png   # what it should look like
```

Check the preview PNG before wiring up hardware. If the framing or colour is off,
adjust and re-push rather than debugging through a 20-second panel refresh.

## 4. Point the board at it

In `goes_epaper_ee02.ino`:

```c
static const char *WIFI_SSID = "...";
static const char *WIFI_PASS = "...";
static const char *HOST = "https://<user>.github.io/<repo>";
```

The sketch detects `https://` and switches to TLS automatically.

## 5. Freshness

Frames come from CIRA SLIDER by default (~10-20 min behind real time). NASA GIBS
carries ~40 min of latency and is kept only as an automatic fallback, so a change
to CIRA's tile layout degrades the picture rather than blanking the panel.

Force one or the other with `--source slider` / `--source gibs`.

## 6. Tune the look

Edit the render step in `.github/workflows/render.yml`:

```yaml
- run: python3 goes_epaper.py --once --outdir out --saturation 1.7 --contrast 1.25
```

Useful range is 1.4–2.0. Remember the published preview shows *canonical* palette
colours, which look more garish on a monitor than the panel's actual inks will.

---

## What you're trading away

**Cron drift.** GitHub's scheduler is best-effort. Runs land 5–20 minutes late
under load. For a weather frame this is invisible; if you need a true 10-minute
metronome, see below.

**The 60-day rule.** GitHub disables scheduled workflows after 60 days with no
repository activity, with an email first. Any commit resets it. Re-enabling is
one click.

**Everything is public.** The repo, the imagery, the URL. Fine here — it's NASA
data with no credentials involved — but don't extend this pattern to anything private.

---

## If the drift bothers you

An always-free VM gives you a real 10-minute cadence and runs the container
unchanged:

- **Oracle Cloud Always Free** — an ARM VM (2 OCPU / 12 GB as of the August 2026
  reduction, still far more than this needs), free indefinitely. Requires a card
  for identity verification but is not charged. Two gotchas that catch everyone:
  open the port in the VCN **Security List** *and* in the instance's own iptables,
  which Oracle's images lock down by default.
- **Google Cloud e2-micro** — always-free in us-west1/us-central1, also card-verified.

On either, it's just:

```bash
docker compose up -d
# then HOST = "http://<vm-ip>:8080"
```

And if you already own a Raspberry Pi or a NAS, use that instead — same
`docker compose up -d`, genuinely free, nothing exposed to the internet.
