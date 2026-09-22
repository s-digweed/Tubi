#!/usr/bin/env python3
"""
Tubi live-TV scraper (Playwright / headless-browser edition).

Why a browser instead of plain requests:
  Tubi's guest-token mint endpoint
  (account.production-public.tubi.io/device/anonymous/token) is now
  cryptographically SIGNED ("INVALID_SIGNATURE_PARAMS" without a valid sig).
  Letting Tubi's own JavaScript load and mint the token sidesteps the
  signature entirely, and also carries the correct Origin + geo automatically.

Flow:
  1. Fetch US socks4 proxies, keep only ones whose exit IP is really US.
  2. Launch Chromium through a US proxy, load tubitv.com/live so Tubi's JS
     mints the guest `at` token.
  3. From inside the page, call the tensor-cdn homescreen + per-container
     endpoints with `Authorization: Bearer <at>` and collect the linear
     channels (type "l") — each already carries its .m3u8 and schedule.
  4. Build tubi_playlist.m3u + tubi_epg.xml (flat XML for IPTVBoss).

Outputs (same names as the old scraper, so the workflow's commit step is unchanged):
  tubi_playlist.m3u
  tubi_epg.xml
"""

import json
import os
import sys
import time
import uuid
import random
import requests
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# EPG URL advertised inside the M3U. Point this at YOUR repo's raw path.
EPG_TVG_URL = "https://raw.githubusercontent.com/s-digweed/Tubi/main/tubi_epg.xml"

PROXY_API = (
    "https://api.proxyscrape.com/v2/?request=displayproxies"
    "&protocol=socks4&timeout=10000&country=US&ssl=all&anonymity=elite"
)

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

WANT_US_PROXIES = 8      # how many verified-US proxies to shortlist
MAX_PROXY_TRIES = 8      # how many to actually drive a browser through
NAV_TIMEOUT_MS = 60000
TOKEN_WAIT_S = 30

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---------------------------------------------------------------------------
# Proxy handling
# ---------------------------------------------------------------------------

def get_proxies():
    try:
        r = requests.get(PROXY_API, timeout=20)
        if r.status_code == 200:
            return [p.strip() for p in r.text.splitlines() if p.strip()]
    except Exception as e:
        print(f"proxy fetch error: {e}")
    return []

def is_us_exit(hostport):
    """Fast check: does this socks4 proxy egress from a US IP?"""
    proxy = f"socks4://{hostport}"
    try:
        r = requests.get(
            "https://ipinfo.io/country",
            proxies={"http": proxy, "https": proxy},
            timeout=6,
        )
        return r.status_code == 200 and r.text.strip() == "US"
    except Exception:
        return False

def shortlist_us(proxies, want):
    random.shuffle(proxies)
    good = []
    for hp in proxies:
        if is_us_exit(hp):
            good.append(hp)
            print(f"  US proxy ok: {hp}")
            if len(good) >= want:
                break
    return good

# ---------------------------------------------------------------------------
# In-page scraper (runs inside Tubi's origin, with the minted token)
# ---------------------------------------------------------------------------

# Collects every linear channel (type "l") that has a playable manifest,
# plus a program map for EPG titles. Returns {channels:[...], programs:{...}}.
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

  // 1) homescreen — discover which linear containers exist right now
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

  // 2) walk each container, collect channels + programs
  const channels = {};
  const programs = {};
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
        if (!c) continue;
        if (c.type === "l" && Array.isArray(c.video_resources) && c.video_resources.length) {
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
            schedules: Array.isArray(c.schedules) ? c.schedules : [],
          };
        } else if (c.type === "v") {
          programs[id] = { title: c.title || "", description: c.description || "" };
        }
      }
    } catch (e) { /* skip a bad container */ }
  }
  return { channels: Object.values(channels), programs };
}
"""

def scrape_via_browser(pw, hostport):
    proxy = {"server": f"socks4://{hostport}"}
    browser = pw.chromium.launch(
        headless=True,
        proxy=proxy,
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
            print(f"  {hostport}: no token cookie after {TOKEN_WAIT_S}s")
            return None

        data = page.evaluate(JS_SCRAPE, {"base": BASE, "slugs": SEED_SLUGS})
        if not data or data.get("error"):
            print(f"  {hostport}: page error {data}")
            return None
        chans = data.get("channels", [])
        if not chans:
            print(f"  {hostport}: 0 channels returned")
            return None
        print(f"  {hostport}: {len(chans)} channels")
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

def build_epg(channels, programs):
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<tv>"]
    for ch in channels:
        out.append(
            f'<channel id="{ch["id"]}"><display-name>{esc(ch["title"])}</display-name>'
            + (f'<icon src="{ch["logo"]}" />' if ch["logo"] else "")
            + "</channel>"
        )
    for ch in channels:
        for s in ch.get("schedules", []):
            start = xmltv_time(s.get("start_time", ""))
            stop = xmltv_time(s.get("end_time", ""))
            if not start or not stop:
                continue
            prog = programs.get(str(s.get("program_id", "")), {})
            title = prog.get("title") or ch["title"]
            desc = prog.get("description") or ""
            line = (f'<programme start="{start}" stop="{stop}" channel="{ch["id"]}">'
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
    print("Fetching proxies...")
    proxies = get_proxies()
    print(f"  {len(proxies)} raw proxies")
    if not proxies:
        print("No proxies; aborting.")
        sys.exit(1)

    print("Verifying US exits...")
    us = shortlist_us(proxies, WANT_US_PROXIES)
    if not us:
        print("No verified-US proxies; aborting.")
        sys.exit(1)

    data = None
    with sync_playwright() as pw:
        for hp in us[:MAX_PROXY_TRIES]:
            print(f"Trying browser via {hp} ...")
            try:
                data = scrape_via_browser(pw, hp)
            except Exception as e:
                print(f"  {hp}: {e}")
                data = None
            if data:
                break

    if not data:
        print("ERROR: all attempts failed; leaving previous files untouched.")
        sys.exit(1)

    channels = data["channels"]
    programs = data.get("programs", {})

    with open("tubi_playlist.m3u", "w", encoding="utf-8") as f:
        f.write(build_m3u(channels))
    with open("tubi_epg.xml", "w", encoding="utf-8") as f:
        f.write(build_epg(channels, programs))

    print(f"Done. {len(channels)} channels written.")

if __name__ == "__main__":
    main()
