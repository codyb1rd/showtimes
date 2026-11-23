#!/usr/bin/env python3
"""Build a Google-style movie showtimes page for a city.

Source: cinemaclock.com. The city page lists nearby theaters (with coordinates);
each theater page carries several days of showtimes, and every showtime has a
/buy-tickets/<id> link that redirects to the chain's own seat map / checkout.

Usage: showtimes [city state] [--radius MILES] [--out FILE] [--no-open] [--json]
       showtimes                 -> Reno, NV
       showtimes sacramento ca --radius 10
"""
import argparse
import base64
import concurrent.futures as cf
import datetime as dt
import html
import json
import math
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

import gfill

BASE = "https://www.cinemaclock.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template.html"
OUT_DIR = Path.home() / "Claude" / "Personal" / "diagrams"
RETRY_CODES = {429, 500, 502, 503, 504}
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def fetch(url, binary=False, timeout=20, tries=3):
    """GET with retries: cinemaclock sometimes cuts a page off mid-transfer (IncompleteRead).
    "Too many requests" (429) and server-side errors (5xx) are temporary, so they are retried
    too. A 404 or other HTTP error is a real answer and is not retried."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            return data if binary else data.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_CODES or attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def text(s):
    """Strip tags, unescape entities, collapse whitespace."""
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def city_slug(words):
    raw = " ".join(words).replace(",", " ").strip().lower()
    if not raw:
        return "reno-nv"
    parts = raw.split()
    if len(parts) < 2 or len(parts[-1]) != 2:
        sys.exit("Add the two-letter state after the city, e.g.  showtimes reno nv")
    return "-".join(parts)


def miles(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def parse_theaters(page):
    """City page -> theaters in the site's own order (nearest the city first)."""
    out = []
    pat = re.compile(
        r"\{kino:'((?:[^'\\]|\\.)*)',tid:(\d+),lat:([-\d.]+),lon:([-\d.]+),descr:'(.*?)'\}", re.S)
    for name, tid, lat, lon, descr in pat.findall(page):
        slug = re.search(r'href="/movie-theaters/([^"]+)"', descr)
        addr = re.search(r'class="address">([^<]*)', descr)
        if not slug:
            continue
        out.append({
            "id": tid, "name": html.unescape(name.replace("\\'", "'")),
            "slug": slug.group(1), "address": html.unescape(addr.group(1)) if addr else "",
            "lat": float(lat), "lon": float(lon),
        })
    return out


def resolve_date(label_date, earliest):
    """'Oct 19' -> 'YYYY-MM-DD', using the block's earliest date to pick the year."""
    mon, day = label_date.split()
    y = earliest.year
    d = dt.date(y, MONTHS[mon[:3]], int(day))
    if d < earliest - dt.timedelta(days=180):
        d = d.replace(year=y + 1)
    return d.isoformat()


def film_kind(slug):
    """cinemaclock slugs end in the release year ('clue-1985') or '-event' for special
    screenings. New = released this year or last; anything older is a re-release."""
    y = re.search(r"-((?:19|20)\d\d)$", slug)
    if not y:
        return "event"
    return "new" if int(y.group(1)) >= dt.date.today().year - 1 else "rerelease"


def parse_theater_page(page, tid):
    movies, groups = {}, []
    blocks = re.split(r'<div id="moviecin', page)[1:]
    for b in blocks:
        b = b.split("<!-- endsb -->")[0]
        mid = re.search(r"\bmovie(\d+)\b", b[:400])
        title = re.search(r"<h3 class='movietitle[^']*'[^>]*>\s*<a[^>]*href='/movies/([^']+)'[^>]*>(.*?)</a>", b, re.S)
        if not (mid and title):
            continue
        mid = mid.group(1)
        if mid not in movies:
            genre_p = re.search(r"<p class='moviegenre'>(.*?)</p>", b, re.S)
            gp = genre_p.group(1) if genre_p else ""
            rating = re.search(r"<span class='rtUS'>([^<]+)</span>", gp)
            cast = re.search(r"<span class='avec'>(.*?)</span>", gp, re.S)
            after = re.sub(r"<span class='(rt|avec)[^']*'>.*?</span>", "", gp, flags=re.S)
            after = text(after)
            runtime = re.search(r"\b(\d+h\d*m?|\d+m)\b", after)
            week = re.search(r"(\d+(?:st|nd|rd|th) week|New|Opening[^&]*)", after)
            genre = after
            for m in (runtime, week):
                if m:
                    genre = genre.replace(m.group(0), " ")
            genre = re.sub(r"\s+", " ", genre).strip()
            year = re.match(r"((?:19|20)\d\d) ", genre)
            if year:
                genre = genre[len(year.group(0)):]
            score = re.search(r"class='valmain[^']*'>(\d+)<span class='decimal'>(\.\d)", b)
            poster = re.search(r"data-src='([^']+poster[^']*)'", b)
            movies[mid] = {
                "title": text(title.group(2)), "slug": title.group(1),
                "rating": rating.group(1).strip() if rating else "",
                "runtime": runtime.group(1) if runtime else "",
                "genre": genre, "year": year.group(1) if year else "",
                "cast": text(cast.group(1)) if cast else "",
                "score": score.group(1) + score.group(2) if score else "",
                "week": week.group(1) if week else "",
                "poster": poster.group(1) if poster else "",
                "kind": film_kind(title.group(1)),
            }
        for fb in re.split(r'<div data-earliest-date="', b)[1:]:
            earliest = dt.datetime.strptime(fb[:8], "%Y%m%d").date()
            notes = [text(n) for n in re.findall(r'<p class="timesalso[^"]*">(.*?)</p>', fb, re.S)]
            fmt = notes[0] if notes else ""
            extra = [n for n in notes[1:] if n]
            days = {}
            for label, items in re.findall(
                    r'<u>[^<]*<span class="timesdate">([^<]+)</span></u><i>(.*?)</i>', fb, re.S):
                date = resolve_date(label.strip(), earliest)
                for cls, t, tix in re.findall(
                        r'<span class="([^"]*)" data-time="(\d+)"(?: id="([^"]+)")?', items):
                    days.setdefault(date, []).append([int(t), tix or ""])
            if days:
                groups.append({"mid": mid, "tid": tid, "fmt": fmt, "notes": extra, "days": days})
    return movies, groups


CACHE = Path.home() / "Library" / "Caches" / "showtimes" / "ratings.json"
# Builds run twice a day; 20 h means a new release's score refreshes once a day (on the morning
# build), a re-release's once a week. A miss is retried daily.
TTL = {"new": 20 * 3600, "rerelease": 6.5 * 86400, "event": 6.5 * 86400}
MISS_TTL = 20 * 3600


def slugify(title, sep):
    """Film title -> address slug as the score sites write it. Accents are dropped
    ("Amélie" -> amelie), as are apostrophes and full stops."""
    t = html.unescape(title).lower().replace("&", "and")
    t = re.sub(r"['’.]", "", t)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", sep, t).strip(sep)


def year_ok(found, want):
    return found.isdigit() and abs(int(found) - want) <= 1


# A score site that blocks or rate-limits the build (403, 429, or a 5xx that outlasted fetch's
# retries) is skipped for the rest of that build rather than asked hundreds more times. Its
# lookups raise, so add_ratings caches nothing and the next build tries again.
DOWN = set()


def page_or_none(site, url):
    """The page, or None when that address doesn't exist (404/410). Any other failure raises."""
    if site in DOWN:
        raise RuntimeError(f"{site} skipped for this build")
    try:
        return fetch(url)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return None
        DOWN.add(site)
        raise


def rt_lookup(title, year):
    """Rotten Tomatoes Tomatometer, plus RT's Fandango link (every theater showing the film).
    RT's robots.txt disallows /search, so like Letterboxd this tries the film address with the
    year suffix, then without, and accepts only when the page's release year matches (±1)."""
    base = slugify(title, "_")
    bare = re.sub(r"^(the|a|an)_", "", base)   # RT often drops a leading article: /m/killing
    tries = [f"{base}_{year}", base, bare, f"{bare}_{year}", f"{base}_{year - 1}", f"{base}_{year + 1}"]
    for slug in dict.fromkeys(tries):           # in order, without repeats
        url = f"https://www.rottentomatoes.com/m/{slug}"
        page = page_or_none("rt", url)
        if page is None:
            continue
        # releaseYear is in the Fandango block (films on Fandango) and can be a re-release's year;
        # dateCreated is the film's own release date, in every page's schema.org data. Either fits.
        years = [m.group(1) for m in (re.search(r'"releaseYear":"(\d{4})"', page),
                                      re.search(r'"dateCreated":"(\d{4})-', page)) if m]
        if not any(year_ok(y, year) for y in years):
            continue
        s = re.search(r'"criticsScore":\{[^}]*?"scorePercent":"(\d+)%"', page)
        canon = re.search(r'<link rel="canonical" href="([^"]+)"', page)
        tix = re.search(r'"showtimesUrl":"(https://www\.fandango\.com/[^"?]+)', page)
        released = re.search(r'"dateCreated":"(\d{4}-\d\d-\d\d)', page)   # the page uses it for "weeks out"
        return {"score": int(s.group(1)) if s else None, "url": canon.group(1) if canon else url,
                "tix": tix.group(1) if tix else "", "img": poster_in("rt", page),
                "released": released.group(1) if released else ""}
    return None


def poster_in(site, page):
    """A film page's portrait poster: RT's og:image (RT's own placeholder ignored); Letterboxd's
    schema.org image (its og:image is a wide backdrop, not a poster)."""
    if site == "rt":
        m = re.search(r'<meta property="og:image" content="([^"]+)"', page)
        return m.group(1) if m and "poster_default" not in m.group(1) else ""
    m = re.search(r'"image":"(https://a\.ltrbxd\.com/resized/film-poster/[^"]+)"', page)
    return m.group(1) if m else ""


def fallback_poster(m):
    """A poster for a film cinemaclock has none for: from its RT or Letterboxd match, reading the
    page again only for a match cached before matches kept the poster address."""
    for site in ("rt", "lb"):
        v = m.get(site) or {}
        if v.get("img"):
            return v["img"]
    for site in ("rt", "lb"):
        v = m.get(site) or {}
        if v.get("url") and "img" not in v:
            try:
                v["img"] = poster_in(site, fetch(v["url"]))
            except Exception:
                continue
            if v["img"]:
                return v["img"]
    return ""


def lb_lookup(title, year):
    """Letterboxd average (out of 5). Search is blocked, so try the film address with the
    year suffix, then without, and accept only when the page's year matches (±1)."""
    base = slugify(title, "-")
    for slug in (f"{base}-{year}", base):
        url = f"https://letterboxd.com/film/{slug}/"
        page = page_or_none("lb", url)
        if page is None:
            continue
        y = re.search(r'og:title" content="[^"]*\((\d{4})\)', page)
        if not (y and year_ok(y.group(1), year)):
            continue
        r = re.search(r'twitter:data2" content="([\d.]+) out of 5', page)
        return {"score": float(r.group(1)) if r else None, "url": url, "img": poster_in("lb", page)}
    return None


# Cache key prefix per site. Changing one makes every cached entry for that site look up again on
# the next build: "rt2" since RT entries gained the Fandango link ("tix").
CACHE_KEY = {"rt": "rt2", "lb": "lb"}


def add_ratings(movies, ex):
    """Attach rt / lb to each movie, using a small on-disk cache so rebuilds are quick."""
    try:
        cache = json.loads(CACHE.read_text())
    except Exception:
        cache = {}
    now = dt.datetime.now().timestamp()
    jobs = {}
    for mid, m in movies.items():
        y = re.search(r"-((?:19|20)\d\d)$", m["slug"])
        if not y:
            continue  # "-event" listings (opera, anime events) have no film to match
        year = int(y.group(1))
        for site, fn, args in (("rt", rt_lookup, (m["title"], year)),
                               ("lb", lb_lookup, (m["title"], year))):
            c = cache.get(f"{CACHE_KEY[site]}:{m['slug']}")
            if c and now - c["t"] < (TTL.get(m.get("kind"), TTL["new"]) if c["v"] else MISS_TTL):
                m[site] = c["v"]
            else:
                jobs[ex.submit(fn, *args)] = (mid, site)
    for f in cf.as_completed(jobs):
        mid, site = jobs[f]
        try:
            v = f.result()
        except Exception:
            continue  # network hiccup: leave blank, don't cache
        movies[mid][site] = v
        cache[f"{CACHE_KEY[site]}:{movies[mid]['slug']}"] = {"t": now, "v": v}
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache))
    except Exception:
        pass


POSTERS = CACHE.parent / "posters"


def poster_for(mid, m):
    """A film's poster: one already saved; else cinemaclock's; else its RT or Letterboxd poster.
    An event with no match anywhere gets none, and the page draws a title card instead."""
    f = POSTERS / f"{mid}.jpg"
    if f.exists():
        return f.read_bytes()
    url = m["poster"] or fallback_poster(m)
    return poster_bytes(mid, url) if url else b""


def poster_bytes(mid, url):
    """Poster image, cached on disk by movie id (posters don't change once a film is listed)."""
    f = POSTERS / f"{mid}.jpg"
    if f.exists():
        return f.read_bytes()
    try:
        raw = fetch(BASE + url if url.startswith("/") else url, binary=True, timeout=15)
    except Exception:
        return b""
    try:
        POSTERS.mkdir(parents=True, exist_ok=True)
        f.write_bytes(raw)
    except Exception:
        pass
    return raw


def city_area(slug, radius, cap):
    """City page -> (display name, theaters within radius of the nearest one, at most `cap`)."""
    page = fetch(f"{BASE}/{slug}/movie-theaters")
    theaters = parse_theaters(page)
    if not theaters:
        raise ValueError("no theaters on the city page — the site layout may have changed")
    name = re.search(r"<title>[^<]*?(?:in|-)\s*([^<|]+?,\s*[A-Z]{2})", page)
    name = name.group(1).strip() if name else slug.rsplit("-", 1)[0].replace("-", " ").title() + ", " + slug[-2:].upper()
    centre = (theaters[0]["lat"], theaters[0]["lon"])
    for t in theaters:
        t["dist"] = round(miles(centre, (t["lat"], t["lon"])), 1)
    near = sorted((t for t in theaters if t["dist"] <= radius), key=lambda t: t["dist"])[:cap]
    return name, centre, near


def build_cities(slugs, radius, cap, no_ratings=False, workers=4, log=print):
    """Fetch every city's area, each theater page once, ratings once per movie.
    Returns ({slug: city dict}, all movies, failures)."""
    cities, failed = {}, []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(city_area, s, radius, cap): s for s in slugs}
        for f in cf.as_completed(futs):
            s = futs[f]
            try:
                name, centre, near = f.result()
                cities[s] = {"slug": s, "city": name, "lat": centre[0], "lon": centre[1], "theaters": near}
            except Exception as e:
                failed.append(f"{s}: {e}")
        log(f"{len(cities)} cities, fetching theater pages…")

        unique = {t["slug"]: t for c in cities.values() for t in c["theaters"]}
        parsed, movies = {}, {}
        futs = {ex.submit(fetch, f"{BASE}/movie-theaters/{sl}"): t for sl, t in unique.items()}
        for f in cf.as_completed(futs):
            t = futs[f]
            try:
                m, g = parse_theater_page(f.result(), t["id"])
            except Exception as e:
                failed.append(f"{t['name']}: {e}")
                continue
            parsed[t["slug"]] = g
            for k, v in m.items():
                movies.setdefault(k, v)
        log(f"{len(parsed)} theaters, {len(movies)} movies; ratings and posters…")

        if not no_ratings:
            add_ratings(movies, ex)
        # Top up a few cities with Google's showtimes where cinemaclock falls short (gfill.py). Before
        # posters, so a film only Google listed gets scores and a poster like any other.
        gfill.apply(cities, parsed, movies, CACHE.parent / "gfill.json", log=log)
        if not no_ratings:
            google_only = {mid: {**m, "slug": f"{slugify(m['title'], '-')}-{dt.date.today().year}"}
                           for mid, m in movies.items() if mid.startswith("g")}
            add_ratings(google_only, ex)
            for mid, m in google_only.items():
                movies[mid].update({k: m[k] for k in ("rt", "lb") if k in m})
        posters = {mid: ex.submit(poster_for, mid, m) for mid, m in movies.items()}
        images = {mid: f.result() for mid, f in posters.items()}
        log(f"posters: {sum(1 for v in images.values() if v)}/{len(movies)} films")

    for m in movies.values():
        if m["slug"]:                 # a film only Google listed has no cinemaclock page; gfill set its url
            m["url"] = f"{BASE}/movies/{m['slug']}"
    generated = dt.datetime.now().astimezone().isoformat(timespec="minutes")
    for c in cities.values():
        groups = [g for t in c["theaters"] for g in parsed.get(t["slug"], [])]
        for t in c["theaters"]:
            t["url"] = f"{BASE}/movie-theaters/{t['slug']}"
            t["maps"] = f"https://www.google.com/maps/dir//{t['lat']},{t['lon']}"
        mids = {g["mid"] for g in groups}
        c.update({
            "radius": radius, "generated": generated, "groups": groups,
            "movies": {mid: dict(movies[mid]) for mid in mids},
            "failed": [x for x in failed if any(x.startswith(t["name"] + ":") for t in c["theaters"])],
            "tixBase": f"{BASE}/buy-tickets/",
        })
    return cities, images, failed


def summary(c):
    shows = sum(len(v) for g in c["groups"] for v in g["days"].values())
    rated = sum(1 for m in c["movies"].values() if m.get("rt") or m.get("lb"))
    return (f"{c['city']}: {len(c['theaters'])} theaters, {len(c['movies'])} movies, {shows} showtimes, "
            f"{rated} matched to RT/Letterboxd")


def write_site(out_dir, cities, images, order):
    """Multi-city site: index.html (the app), cities.json, data/<slug>.json, posters/<id>.jpg."""
    out_dir = Path(out_dir)
    (out_dir / "data").mkdir(parents=True, exist_ok=True)
    (out_dir / "posters").mkdir(exist_ok=True)
    for mid, raw in images.items():
        if raw:
            (out_dir / "posters" / f"{mid}.jpg").write_bytes(raw)
    index = []
    for slug in order:
        c = cities.get(slug)
        if not c:
            continue
        for mid, m in c["movies"].items():
            m["poster"] = f"posters/{mid}.jpg" if images.get(mid) else ""
        (out_dir / "data" / f"{slug}.json").write_text(json.dumps(c, separators=(",", ":")))
        index.append({"slug": slug, "city": c["city"], "lat": c["lat"], "lon": c["lon"],
                      "theaters": len(c["theaters"]), "movies": len(c["movies"])})
    (out_dir / "cities.json").write_text(json.dumps(
        {"generated": dt.datetime.now().astimezone().isoformat(timespec="minutes"), "cities": index},
        separators=(",", ":")))
    (out_dir / "index.html").write_text(TEMPLATE.read_text())


def read_city_list(path):
    slugs = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            slugs.append(city_slug(line.replace(",", " ").split()))
    return slugs


def main():
    ap = argparse.ArgumentParser(description="Movie showtimes page for a city (default Reno, NV).")
    ap.add_argument("city", nargs="*", help="city and two-letter state, e.g. reno nv")
    ap.add_argument("--radius", type=float, default=20, help="miles from the city's nearest theater (default 20)")
    ap.add_argument("--max-theaters", type=int, default=25, help="cap on theaters per city, nearest first (default 25)")
    ap.add_argument("--out", help="output HTML path")
    ap.add_argument("--no-open", action="store_true", help="don't open the page in the browser")
    ap.add_argument("--json", action="store_true", help="print the data as JSON instead of building a page")
    ap.add_argument("--no-ratings", action="store_true", help="skip Rotten Tomatoes / Letterboxd lookups")
    ap.add_argument("--site", metavar="DIR", help="build the multi-city site into DIR (cities from --cities)")
    ap.add_argument("--min-cities", type=int, metavar="N",
                    help="with --site: fail without writing anything if fewer than N cities load "
                         "(default: 90%% of the list), so a broken scrape can't replace a good site")
    ap.add_argument("--cities", default=str(HERE / "cities.txt"), help="city list for --site, one 'City, ST' per line")
    a = ap.parse_args()

    if a.site:
        order = read_city_list(a.cities)
        cities, images, failed = build_cities(order, a.radius, a.max_theaters, a.no_ratings)
        for f in failed:
            print(f"  could not read {f}")
        # A city counts only if it came back with showtimes: a theater-page redesign or block
        # leaves every city listed but empty, which is the failure this guard is for.
        need = a.min_cities if a.min_cities is not None else math.ceil(0.9 * len(order))
        loaded = sum(1 for c in cities.values() if c["groups"])
        if loaded < need:
            sys.exit(f"Only {loaded}/{len(order)} cities loaded with showtimes (need {need}); "
                     f"nothing written. The source site's layout may have changed.")
        write_site(a.site, cities, images, order)
        for s in order:
            if s in cities:
                print(summary(cities[s]))
        print(f"{len(cities)}/{len(order)} cities written to {a.site}")
        return

    slug = city_slug(a.city)
    try:
        cities, images, failed = build_cities([slug], a.radius, a.max_theaters, a.no_ratings, log=lambda *_: None)
    except urllib.error.HTTPError as e:
        sys.exit(f"Could not reach cinemaclock for '{slug}' ({e.code}).")
    if slug not in cities:
        sys.exit(f"No theater list for '{slug}' on cinemaclock. Check the city/state spelling.\n  {'; '.join(failed)}")
    data = cities[slug]
    for mid, m in data["movies"].items():
        raw = images.get(mid)
        m["poster"] = "data:image/jpeg;base64," + base64.b64encode(raw).decode() if raw else ""
    if a.json:
        print(json.dumps(data, indent=1))
        return
    out = Path(a.out) if a.out else OUT_DIR / f"Showtimes - {data['city'].replace(',', '')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    out.write_text(TEMPLATE.read_text().replace("/*__DATA__*/null", payload))
    print(summary(data))
    for f in data["failed"]:
        print(f"  could not read {f}")
    print(out)
    if not a.no_open:
        subprocess.run(["open", str(out)])


if __name__ == "__main__":
    main()
