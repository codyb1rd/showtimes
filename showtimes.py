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
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://www.cinemaclock.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template.html"
OUT_DIR = Path.home() / "Claude" / "Personal" / "diagrams"
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def fetch(url, binary=False, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


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
HIT_TTL, MISS_TTL = 3 * 86400, 86400


def norm(title):
    t = html.unescape(title).lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", t)


def lb_slug(title):
    t = html.unescape(title).lower().replace("&", "and")
    t = re.sub(r"['’.]", "", t)
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def year_ok(found, want):
    return found.isdigit() and abs(int(found) - want) <= 1


def rt_lookup(title, year, cast):
    """Rotten Tomatoes Tomatometer via its search page. Match on title + year (±1);
    for a near-miss title, accept only when a lead's surname is in RT's cast list."""
    q = urllib.request.quote(html.unescape(title))
    page = fetch(f"https://www.rottentomatoes.com/search?search={q}").replace("\n", " ")
    surnames = [n.split()[-1].lower() for n in re.split(r" & |, ", cast.replace("by ", "")) if n.split()]
    best = None
    for row in re.findall(r"<search-page-media-row(.*?)</search-page-media-row>", page, re.S):
        y = re.search(r'release-year="(\d*)"', row)
        s = re.search(r'tomatometer-score="(\d*)"', row)
        a = re.search(r'href="(https://www\.rottentomatoes\.com/m/[^"]+)"[^>]*data-qa="info-name"[^>]*>\s*([^<]+)', row)
        if not (y and a and year_ok(y.group(1), year)):
            continue
        rcast = (re.search(r'cast="([^"]*)"', row) or [None, ""])[1].lower()
        exact = norm(a.group(2)) == norm(title)
        if exact or any(n in rcast for n in surnames):
            best = {"score": int(s.group(1)) if s and s.group(1) else None, "url": a.group(1)}
            if exact:
                break
    return best


def lb_lookup(title, year):
    """Letterboxd average (out of 5). Search is blocked, so try the film address with the
    year suffix, then without, and accept only when the page's year matches (±1)."""
    base = lb_slug(title)
    for slug in (f"{base}-{year}", base):
        url = f"https://letterboxd.com/film/{slug}/"
        try:
            page = fetch(url)
        except urllib.error.HTTPError:
            continue
        y = re.search(r'og:title" content="[^"]*\((\d{4})\)', page)
        if not (y and year_ok(y.group(1), year)):
            continue
        r = re.search(r'twitter:data2" content="([\d.]+) out of 5', page)
        return {"score": float(r.group(1)) if r else None, "url": url}
    return None


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
        for site, fn, args in (("rt", rt_lookup, (m["title"], year, m["cast"])),
                               ("lb", lb_lookup, (m["title"], year))):
            c = cache.get(f"{site}:{m['slug']}")
            if c and now - c["t"] < (HIT_TTL if c["v"] else MISS_TTL):
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
        cache[f"{site}:{movies[mid]['slug']}"] = {"t": now, "v": v}
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache))
    except Exception:
        pass


def data_uri(url):
    try:
        raw = fetch(BASE + url if url.startswith("/") else url, binary=True, timeout=15)
    except Exception:
        return ""
    kind = "png" if url.endswith(".png") else "jpeg"
    return f"data:image/{kind};base64," + base64.b64encode(raw).decode()


def build(slug, radius, no_ratings=False):
    try:
        city_page = fetch(f"{BASE}/{slug}/movie-theaters")
    except urllib.error.HTTPError as e:
        sys.exit(f"No theater list for '{slug}' on cinemaclock ({e.code}). Check the city/state spelling.")
    theaters = parse_theaters(city_page)
    if not theaters:
        sys.exit(f"Found the page for '{slug}' but no theaters in it — the site layout may have changed.")
    city_name = re.search(r"<title>[^<]*?(?:in|-)\s*([^<|]+?,\s*[A-Z]{2})", city_page)
    city_name = city_name.group(1).strip() if city_name else slug.rsplit("-", 1)[0].title() + ", " + slug[-2:].upper()
    centre = (theaters[0]["lat"], theaters[0]["lon"])
    for t in theaters:
        t["dist"] = round(miles(centre, (t["lat"], t["lon"])), 1)
    theaters = [t for t in theaters if t["dist"] <= radius]

    movies, groups, failed = {}, [], []
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch, f"{BASE}/movie-theaters/{t['slug']}"): t for t in theaters}
        for f in cf.as_completed(futs):
            t = futs[f]
            try:
                m, g = parse_theater_page(f.result(), t["id"])
            except Exception as e:
                failed.append(f"{t['name']}: {e}")
                continue
            movies.update({k: v for k, v in m.items() if k not in movies})
            groups.extend(g)
            t["count"] = sum(len(v) for x in g for v in x["days"].values())

        posters = {mid: ex.submit(data_uri, m["poster"]) for mid, m in movies.items() if m["poster"]}
        if not no_ratings:
            add_ratings(movies, ex)
        for mid, f in posters.items():
            movies[mid]["poster"] = f.result()

    for t in theaters:
        t["url"] = f"{BASE}/movie-theaters/{t['slug']}"
        t["maps"] = f"https://www.google.com/maps/dir//{t['lat']},{t['lon']}"
    for m in movies.values():
        m["url"] = f"{BASE}/movies/{m['slug']}"
    return {
        "city": city_name, "slug": slug, "radius": radius,
        "generated": dt.datetime.now().astimezone().isoformat(timespec="minutes"),
        "theaters": theaters, "movies": movies, "groups": groups, "failed": failed,
        "tixBase": f"{BASE}/buy-tickets/",
    }


def main():
    ap = argparse.ArgumentParser(description="Movie showtimes page for a city (default Reno, NV).")
    ap.add_argument("city", nargs="*", help="city and two-letter state, e.g. reno nv")
    ap.add_argument("--radius", type=float, default=20, help="miles from the city's nearest theater (default 20)")
    ap.add_argument("--out", help="output HTML path")
    ap.add_argument("--no-open", action="store_true", help="don't open the page in the browser")
    ap.add_argument("--json", action="store_true", help="print the data as JSON instead of building a page")
    ap.add_argument("--no-ratings", action="store_true", help="skip Rotten Tomatoes / Letterboxd lookups")
    a = ap.parse_args()

    slug = city_slug(a.city)
    data = build(slug, a.radius, a.no_ratings)
    if a.json:
        print(json.dumps(data, indent=1))
        return
    out = Path(a.out) if a.out else OUT_DIR / f"Showtimes - {data['city'].replace(',', '')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    out.write_text(TEMPLATE.read_text().replace("/*__DATA__*/null", payload))

    shows = sum(len(v) for g in data["groups"] for v in g["days"].values())
    rated = sum(1 for m in data["movies"].values() if m.get("rt") or m.get("lb"))
    print(f"{data['city']}: {len(data['theaters'])} theaters, {len(data['movies'])} movies, {shows} showtimes, "
          f"{rated} matched to RT/Letterboxd")
    for f in data["failed"]:
        print(f"  could not read {f}")
    print(out)
    if not a.no_open:
        subprocess.run(["open", str(out)])


if __name__ == "__main__":
    main()
