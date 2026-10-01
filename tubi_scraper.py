#!/usr/bin/env python3
"""
Tubi live-TV scraper (Playwright / headless-browser edition).

Why a browser instead of plain requests:
  Tubi's guest-token mint endpoint
  (account.production-public.tubi.io/device/anonymous/token) is now
  cryptographically SIGNED ("INVALID_SIGNATURE_PARAMS" without a valid sig).
  Letting Tubi's own JavaScript load and mint the token sidesteps the
  signature entirely, and also carries the correct Origin + geo automatically.

Connectivity:
  Tubi is US-geo and blocks datacenter IPs. The runner reaches Tubi through a
  Proton WireGuard US tunnel (set up by the workflow), so the connection is a
  single stable US exit -- no free-proxy pool. The script refuses to run unless
  it confirms a US exit IP first (guardrail against a wrong-country tunnel).

Flow:
  1. Confirm the exit IP is US (ipinfo.io/country).
  2. Launch Chromium (direct, no proxy), load tubitv.com/live so Tubi's JS
     mints the guest `at` token.
  3. From inside the page, call the tensor-cdn homescreen + per-container
     endpoints with `Authorization: Bearer <at>` and collect the linear
     channels (type "l") -- each already carries its .m3u8 and schedule.
  4. Build tubi_playlist.m3u + tubi_epg.xml (flat XML for IPTVBoss).

Outputs (same names as before, so the workflow's commit step is unchanged):
  tubi_playlist.m3u
  tubi_epg.xml
"""

import sys
import requests
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# EPG URL advertised inside the M3U. Point this at YOUR repo's raw path.
EPG_TVG_URL = "https://raw.githubusercontent.com/s-digweed/Tubi/main/tubi_epg.xml"

BASE = "https://tensor-cdn.production-public.tubi.io"

# Known linear container slugs (used as a fallback / seed; the script also
# discovers whatever the live homescreen returns). Add new ones here if Tubi
# introduces more linear categories.
SEED_SLUGS = [
    "sports_on_tubi",
    "comedy_channels",
    "lifestyle_channels",
    "reality",
    "true_crime_channels",
    "news_channels",
    "kids_channels",
    "entertainment_channels",
    "espanol_channels",
]

ATTEMPTS       = 3          # how many times to try the whole browser flow
NAV_TIMEOUT_MS = 60000
TOKEN_WAIT_S   = 45         # direct US connection mints fast; 45s is generous

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---------------------------------------------------------------------------
# Geo guardrail
# ---------------------------------------------------------------------------

def exit_country():
    try:
        r = requests.get("https://ipinfo.io/country", timeout=10)
        if r.status_code == 200:
            return r.text.strip()
    except Exception as e:
        print(f"  country check error: {e}")
    return "?"

# ---------------------------------------------------------------------------
# In-page scraper (runs inside Tubi's origin, with the minted token)
# ---------------------------------------------------------------------------

# Collects every linear channel (type "l") that has a playable manifest, then
# pulls the FULL multi-day schedule from the still-alive /oz/epg/programming
# endpoint. Returns {channels:[...], epg:[...rows...]}.
JS_SCRAPE = r"""
async ({ base, slugs }) => {
  const at = (document.cookie.match(/(?:^|;\s*)at=([^;]+)/) || [])[1];
  if (!at) return { error: "no_token" };
  const token = decodeURIComponent(at);
  const headers = { "Authorization": "Bearer " + token, "Accept": "application/json" };

  const IMG = [
    "images[posterarts]=w256h368_poster",
    "images[landscape_images]=w504h283_landscape",
    "images[hero_16x9]=w1280h720_hero",
    "images[title_art]=w430h180_title",
  ].join("&");

  // 1) homescreen -- discover which linear containers exist right now
  const discovered = new Set(slugs);
  try {
    const hsUrl = base + "/api/v8/homescreen?include_channels=true&contents_limit=10"
      + "&content_mode=linear&is_kids_mode=false&" + IMG;
    const hs = await fetch(hsUrl, { headers, credentials: "omit" }).then(r => r.json());
    const scan = (o) => {
      if (!o || typeof o !== "object") return;
      if (typeof o.slug === "string" && o.type === "linear") discovered.add(o.slug);
      for (const k in o) scan(o[k]);
    };
    scan(hs);
  } catch (e) { /* fall back to seed slugs */ }

  // 2) walk each container, collect linear channels with a playable manifest
  const channels = {};
  for (const slug of discovered) {
    try {
      const url = base + "/api/v7/containers/" + slug
        + "?contents_limit=100&cursor=0&content_mode=linear&include_channels=true"
        + "&is_kids_mode=false&" + IMG;
      const j = await fetch(url, { headers, credentials: "omit" }).then(r => r.json());
      if (!j || !j.contents) continue;
      const group = (j.container && j.container.title) || slug;
      for (const id in j.contents) {
        const c = j.contents[id];
        if (!c || c.type !== "l") continue;
        if (!Array.isArray(c.video_resources) || !c.video_resources.length) continue;
        const man = c.video_resources[0] && c.video_resources[0].manifest;
        const streamUrl = man && man.url;
        if (!streamUrl) continue;
        const imgs = c.images || {};
        const pick = (a) => (Array.isArray(a) && a.length ? a[0] : "");
        const logo = pick(c.landscape_images) || pick(imgs.landscape_images)
                   || pick(c.posterarts) || pick(imgs.posterarts) || pick(c.thumbnails);
        channels[id] = {
          id, title: c.title || ("Channel " + id), group,
          logo: logo || "", stream: streamUrl,
        };
      }
    } catch (e) { /* skip a bad container */ }
  }

  // 3) FULL EPG -- /oz/epg/programming is still alive and returns multi-day
  //    schedules for a batch of content_ids. Same-origin here, so the `at`
  //    cookie rides along automatically (credentials: include).
  const ids = Object.keys(channels);
  const epg = [];
  for (let i = 0; i < ids.length; i += 100) {
    const batch = ids.slice(i, i + 100).join(",");
    try {
      const u = "https://tubitv.com/oz/epg/programming?content_id=" + batch;
      const j = await fetch(u, { credentials: "include" }).then(r => r.json());
      if (j && Array.isArray(j.rows)) {
        for (const row of j.rows) epg.push(row);
      }
    } catch (e) { /* skip a bad batch */ }
  }

  return { channels: Object.values(channels), epg };
}
"""

def scrape_via_browser(pw):
    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    try:
        ctx = browser.new_context(user_agent=UA, locale="en-US")
        page = ctx.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        page.goto("https://tubitv.com/live", wait_until="domcontentloaded")

        # wait for Tubi's JS to mint the guest `at` cookie
        token_seen = False
        for _ in range(TOKEN_WAIT_S):
            if any(c["name"] == "at" for c in ctx.cookies()):
                token_seen = True
                break
            page.wait_for_timeout(1000)
        if not token_seen:
            # diagnostics: is this a block page, a slow load, or a changed flow?
            try:
                names = sorted(c["name"] for c in ctx.cookies())
                body = page.evaluate(
                    "() => document.body ? document.body.innerText.slice(0,300) : ''")
                print(f"  no token cookie after {TOKEN_WAIT_S}s")
                print(f"    url={page.url}")
                print(f"    title={page.title()!r}")
                print(f"    cookies={names}")
                print(f"    body[:300]={body!r}")
            except Exception as e:
                print(f"  no token cookie; diag failed: {e}")
            return None

        data = page.evaluate(JS_SCRAPE, {"base": BASE, "slugs": SEED_SLUGS})
        if not data or data.get("error"):
            print(f"  page error {data}")
            return None
        chans = data.get("channels", [])
        if not chans:
            print("  0 channels returned")
            return None
        print(f"  {len(chans)} channels")
        return data
    finally:
        browser.close()

# ---------------------------------------------------------------------------
# Output builders
# ---------------------------------------------------------------------------

def xmltv_time(iso):
    # "2026-09-22T03:41:00.000Z" -> "20260922034100 +0000"
    try:
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s).astimezone(timezone.utc)
        return dt.strftime("%Y%m%d%H%M%S +0000")
    except Exception:
        return ""

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))

def build_m3u(channels):
    lines = [f'#EXTM3U url-tvg="{EPG_TVG_URL}"',
             f"# Generated {datetime.now(timezone.utc).isoformat()}"]
    for ch in sorted(channels, key=lambda c: c["title"].lower()):
        lines.append(
            f'#EXTINF:-1 tvg-id="{ch["id"]}" tvg-name="{esc(ch["title"])}" '
            f'tvg-logo="{ch["logo"]}" group-title="{esc(ch["group"])}",{esc(ch["title"])}'
        )
        lines.append(ch["stream"])
    return "\n".join(lines) + "\n"

def build_epg(channels, epg_rows):
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<tv>"]
    # channel entries from the scraped channel list
    for ch in channels:
        out.append(
            f'<channel id="{ch["id"]}"><display-name>{esc(ch["title"])}</display-name>'
            + (f'<icon src="{ch["logo"]}" />' if ch["logo"] else "")
            + "</channel>"
        )
    # full programme schedule from /oz/epg/programming rows
    for row in epg_rows:
        cid = str(row.get("content_id", ""))
        if not cid:
            continue
        for p in row.get("programs", []):
            start = xmltv_time(p.get("start_time", ""))
            stop = xmltv_time(p.get("end_time", ""))
            if not start or not stop:
                continue
            title = p.get("title") or ""
            desc = p.get("description") or ""
            line = (f'<programme start="{start}" stop="{stop}" channel="{cid}">'
                    f"<title>{esc(title)}</title>")
            if desc:
                line += f"<desc>{esc(desc)}</desc>"
            line += "</programme>"
            out.append(line)
    out.append("</tv>")
    return "\n".join(out) + "\n"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    country = exit_country()
    print(f"Exit IP country: {country}")
    if country != "US":
        print("ERROR: exit IP is not US - the WireGuard tunnel is down or wrong-country. "
              "Aborting so we don't scrape the wrong catalog.")
        sys.exit(1)

    data = None
    with sync_playwright() as pw:
        for n in range(1, ATTEMPTS + 1):
            print(f"Browser attempt {n}/{ATTEMPTS} ...")
            try:
                data = scrape_via_browser(pw)
            except Exception as e:
                print(f"  attempt {n} error: {e}")
                data = None
            if data:
                break

    if not data:
        print("ERROR: all attempts failed; leaving previous files untouched.")
        sys.exit(1)

    channels = data["channels"]
    epg_rows = data.get("epg", [])
    prog_count = sum(len(r.get("programs", [])) for r in epg_rows)

    with open("tubi_playlist.m3u", "w", encoding="utf-8") as f:
        f.write(build_m3u(channels))
    with open("tubi_epg.xml", "w", encoding="utf-8") as f:
        f.write(build_epg(channels, epg_rows))

    print(f"Done. {len(channels)} channels, {prog_count} programmes written.")

if __name__ == "__main__":
    main()
