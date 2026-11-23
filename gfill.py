"""Top up cinemaclock with Google's showtimes (via SerpApi, or SearchApi.io as a backup) for a few cities.

Why: cinemaclock misses things. Measured 2026-11-22 against Google on 11 theaters in Houston, Denver
and Reno: whole days missing at most Regals checked; new or expanding films (e.g. "Club Kid")
missing at most theaters of every chain; special screenings missing at smaller theaters; ~15% of
Regal times different. AMC, Cinemark and Century times matched Google exactly where both listed a
film, so those chains are only searched when cinemaclock has dropped their listings.

What it does each build, for the cities in FILL:
  1. Theater fills. Each target theater (see is_target) gets one Google search when its stored
     result is stale (see _stale); a result is 7 days of showtimes and is kept in the state file.
     A search that finds nothing usable is stored as a miss and retried after MISS_DAYS, waiting
     longer after each repeat miss (museums and closed theaters never get Google showtimes).
  2. Merge. For a filled theater, on each day Google covers: a day cinemaclock has nothing for takes
     Google's schedule; a film both list takes Google's times while the result is at most
     OVERRIDE_DAYS old (cinemaclock's ticket link kept where a time matches); a film only Google
     lists is added.
Searches are capped per build (RUN_CAP) and per calendar month (MONTH_CAP), and counted before the
response is read, so a search that errors still counts. A city where cinemaclock looks broken (most
theaters with no listings) is not searched: that is an outage, not a gap. With no API key the step
does nothing. The workflow passes keys only to builds of main, and saves the state file even when a
build fails, so a spent search is never forgotten.

Keys: environment SERPAPI_KEY / SEARCHAPI_KEY only, which CI sets from GitHub secrets. A laptop
build has neither, so it never searches (the month's count lives in CI's cache, not the laptop's)."""
import datetime as dt
import hashlib
import html
import json
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

# City slug -> (location as the search APIs want it, the city's time zone).
FILL = {
    "houston-tx": ("Houston, Texas, United States", "America/Chicago"),
    "reno-nv": ("Reno, Nevada, United States", "America/Los_Angeles"),
}
REFRESH_DAYS = 7      # a search covers 7 days; schedules change little within a published week
SHORT_DAYS = 2        # ...but search again sooner (once it is this old) if Google covered < 5 days ahead
MISS_DAYS = (3, 7, 14, 30)   # days before retrying a search that found nothing: after the 1st miss,
                             # the 2nd, the 3rd, and every later one. On the first live build 8 of 18
                             # theaters (museums, a campus cinema, closed theaters) had no Google showtimes.


def miss_wait(entry):
    return MISS_DAYS[min(entry.get("misses", 1), len(MISS_DAYS)) - 1]
OVERRIDE_DAYS = 3     # Google's times replace cinemaclock's for a film only while this fresh
RUN_CAP = 30          # searches one build may spend
MONTH_CAP = 120       # searches a calendar month may spend (free plans: SerpApi 250, SearchApi.io 100)
OUTAGE = 0.5          # skip a city when more than this share of its theaters have no listings at all
LARGE = 40            # the page's rule: a typical day (upper median) of this many showings is "major"
CHAINS = re.compile(r"^(amc|cinemark|century|cinearts|tinseltown|marcus|movie tavern|cin[eé]polis)", re.I)
REGAL = re.compile(r"^(regal|edwards|ua )", re.I)
UA = "showtimes-site/1.0 (+https://github.com/codyb1rd/showtimes)"


# ---------- keys, budget, providers ----------

def api_key(provider):
    return os.environ.get(f"{provider.upper()}_KEY", "").strip()


class Budget:
    """Searches spent this build and this calendar month; the month's count is in the state file."""
    def __init__(self, state, today):
        month = today.strftime("%Y-%m")
        if state.get("month") != month:
            state["month"], state["used"] = month, 0
        self.state, self.run = state, 0

    def ok(self):
        return self.run < RUN_CAP and self.state["used"] < MONTH_CAP

    def spend(self):
        self.run += 1
        self.state["used"] += 1


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


EXHAUSTED = set()   # providers that ran out of searches or rejected the key this build


def google(q, location, budget):
    """One Google search through the first provider with a key and searches left. Counted before
    the answer is read, so a timeout still counts (the provider may have charged it).
    Returns (provider, response); response is {} for an empty or failed search, and
    (None, None) means nothing was searched (no key, no budget, or every provider out)."""
    for provider, endpoint in (("serpapi", "https://serpapi.com/search"),
                               ("searchapi", "https://www.searchapi.io/api/v1/search")):
        key = api_key(provider)
        if not key or provider in EXHAUSTED or not budget.ok():
            continue
        params = {"engine": "google", "q": q, "location": location, "hl": "en", "gl": "us", "api_key": key}
        budget.spend()
        try:
            data = _get_json(endpoint + "?" + urllib.parse.urlencode(params))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):          # bad key, or plan used up: try the other provider
                EXHAUSTED.add(provider)
                continue
            return provider, {}
        except Exception:                          # timeout, connection reset, bad JSON
            return provider, {}
        if data.get("error"):
            if re.search(r"run out|limit|quota|credits|invalid api key", str(data["error"]), re.I):
                EXHAUSTED.add(provider)
                continue
            return provider, {}
        return provider, data
    return None, None


# ---------- reading Google's showtimes ----------

STRIP = r"\(.*?\)|\b3d\b|\bimax\b|\b4k\b|re-release|\d+(st|nd|rd|th) anniversary"


def words(title):
    """Title -> comparable words: no accents, punctuation, articles or format tags."""
    t = unicodedata.normalize("NFKD", html.unescape(title)).encode("ascii", "ignore").decode().lower()
    t = re.sub(STRIP, " ", t.replace("&", " and "))
    return [w for w in re.findall(r"[a-z0-9]+", t) if w not in ("the", "a", "an")]


def norm(title):
    return "".join(words(title))


def hhmm(t):
    """'6:45pm' -> 1845, '7pm' -> 1900; after-midnight shows (before 4am) belong to the evening
    before, as on cinemaclock: '12:05am' -> 2405."""
    m = re.match(r"\s*(\d{1,2})(?::(\d\d))?\s*([ap])\.?m", t.lower())
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "p" else 0)
    v = h * 100 + int(m.group(2) or 0)
    return v + 2400 if v < 400 else v


DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def day_date(day, today):
    """A showtimes day object -> its date. SearchApi gives the date; SerpApi gives a label."""
    if day.get("date") and re.match(r"\d{4}-\d\d-\d\d$", day["date"]):
        return dt.date.fromisoformat(day["date"])
    label = (day.get("day") or "").strip()
    if label.startswith("Today"):
        return today
    if label.startswith("Tomorrow"):
        return today + dt.timedelta(1)
    for i in range(2, 8):
        d = today + dt.timedelta(i)
        if label[:3] == DOW[d.weekday()]:
            return d
    return None


def showings(entry):
    """[(type, hhmm), ...] from one movie entry, either API's shape."""
    out = []
    for sh in entry.get("showing") or entry.get("showings") or []:
        kind = sh.get("type") or "Standard"
        for t in sh.get("time") or sh.get("times") or []:
            v = hhmm(t.get("time", "") if isinstance(t, dict) else t)
            if v is not None:
                out.append((kind, v))
    return out


def theater_schedule(resp, today):
    """Theater query -> {"days": {date iso: {film title: [(type, hhmm)]}}, "links": {title: url}}."""
    days, links = {}, {}
    for day in resp.get("showtimes") or []:
        d = day_date(day, today)
        if not d:
            continue
        for m in day.get("movies") or []:
            title = m.get("name") or m.get("title")
            if title:
                days.setdefault(d.isoformat(), {}).setdefault(title, []).extend(showings(m))
                link = m.get("link", "")
                links.setdefault(title, link if link.startswith("https://") else "")
    return {"days": days, "links": links}


# ---------- choosing and matching ----------

def _per_day(groups):
    per_day = {}
    for g in groups:
        for d, ts in g["days"].items():
            per_day[d] = per_day.get(d, 0) + len(ts)
    return per_day


def is_target(theater, groups, today):
    """Theaters cinemaclock gets wrong: every Regal; every smaller theater (not a national chain, and
    a typical day under LARGE showings); and a chain theater whose listings are missing outright
    (none at all, or none today or tomorrow while later days have some)."""
    per_day = _per_day(groups)
    name = theater["name"]
    if REGAL.search(name):
        return True
    if CHAINS.search(name):
        soon = [(today + dt.timedelta(i)).isoformat() for i in (0, 1)]
        later = any(n for d, n in per_day.items() if d > soon[1])
        return not any(per_day.values()) or (later and not all(per_day.get(d) for d in soon))
    counts = sorted(n for n in per_day.values() if n)
    return not counts or counts[len(counts) // 2] < LARGE        # upper median, as the page does


GENERIC = {"the", "and", "theatre", "theatres", "theater", "theaters", "cinema", "cinemas", "cine", "movies",
           "regal", "edwards", "ua", "cinemark", "amc", "dine", "in", "xd", "imax", "rpx", "screenx", "4dx", "dolby",
           "stadium"}


def name_words(name):
    t = re.sub(r"['’′`]", "", html.unescape(name))           # Marq'E / Marq′E -> MarqE
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    return {w for w in re.findall(r"[a-z0-9]+", t.replace("&", " ")) if w not in GENERIC}


def same_theater(a_name, b_name):
    """'Harkins Theatres Northfield' ~ 'Harkins Northfield'; 'Cinemark Century Park Lane 16 and XD' ~
    'Century Park Lane 16': the shorter name's distinctive words all appear in the longer one, and
    at least one of them is a real word (not just a screen count like '12')."""
    a, b = name_words(a_name), name_words(b_name)
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return any(not w.isdigit() for w in small) and small <= big


def same_film(google_title, cc_title):
    """Same film under two listings: the same words, or the shorter title is the start of the longer
    at a word boundary ('Ebenezer' ~ 'Ebenezer: A Christmas Carol'). Never when the longer one only
    adds a number ('Zootopia' vs 'Zootopia 2' are different films), and never from an empty title."""
    a, b = words(google_title), words(cc_title)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    rest = long_[len(short):]
    return long_[:len(short)] == short and not (rest and rest[0].isdigit()) and len(short) >= 1 and len("".join(short)) >= 5


def film_match(title, pools, movies, claimed=()):
    """The cinemaclock film a Google title means, looked for in each pool in turn (the theater's films
    that day, then its films any day this week, so a day cinemaclock dropped still matches). A pool
    with two candidates is ambiguous and skipped. A partial match never takes a film in `claimed`
    (one another Google title names exactly), so a re-release 'The Hunger Games' listed beside
    'The Hunger Games: Sunrise on the Reaping' stays a separate film."""
    for mids in pools:
        hits = [m for m in mids if same_film(title, movies[m]["title"])]
        exact = [m for m in hits if words(movies[m]["title"]) == words(title)]
        if exact:
            return exact[0]
        hits = [m for m in hits if m not in claimed]
        if len(hits) == 1:
            return hits[0]
    return None


def same_times(times, mids, groups, d):
    """A film listed under another name ('Ramayana: Part 1' / 'Ramayana: Rise of a Legend'): the one
    cinemaclock film at this theater that day sharing most of these showtimes (at least 2, and at
    least half of Google's). Two different films almost never share most of their times."""
    want = {t for _, t in times}
    best = []
    for m in mids:
        have = {t for g in groups if g["mid"] == m for t, _ in g["days"].get(d, [])}
        hit = len(want & have)
        if hit >= 2 and hit * 2 >= len(want):
            best.append(m)
    return best[0] if len(best) == 1 else None


def movie_for(title, movies, by_key):
    """A movie id for a film cinemaclock doesn't list here: the same film from another city if
    cinemaclock has it anywhere (keeps its poster and scores), else a new entry with just the title.
    A title with no Latin letters (e.g. all Japanese) gets an id from a hash of the title itself."""
    k = norm(title)
    if k and k in by_key:
        return by_key[k]
    mid = "g" + (k[:40] if k else hashlib.sha1(title.encode()).hexdigest()[:12])
    movies.setdefault(mid, {"title": title, "slug": "", "rating": "", "runtime": "", "genre": "",
                            "year": "", "cast": "", "score": "", "week": "", "poster": "", "kind": "new",
                            "url": "https://www.google.com/search?q=" + urllib.parse.quote(f"{title} movie")})
    if k:
        by_key[k] = mid
    return mid


# ---------- merging ----------

def merge(groups, tid, sched, dates, movies, by_key, override=True):
    """Fold one Google theater schedule into that theater's cinemaclock groups (in place), on `dates`.
    override=False only adds what cinemaclock lacks (days, films) and leaves listed films alone.
    Returns the number of showtimes Google added or changed."""
    changed = 0
    extra = {}                                       # (mid, format) -> new group
    week = {g["mid"] for g in groups}                 # the theater's cinemaclock films, any day
    for d in dates:
        films = sched["days"].get(d)
        if not films:
            continue
        here = {g["mid"] for g in groups if g["days"].get(d)}
        taken = {}                                    # mid -> {time: [ticket ids]} cinemaclock had that day
        claimed = {m for m in week for title in films if words(movies[m]["title"]) == words(title)}
        for title, times in films.items():
            if not times:
                continue
            mid = film_match(title, (here, week), movies, claimed)
            if not mid:
                mid = same_times(times, here - claimed, groups, d)
            if mid in here and not override:
                continue                              # listed that day; trusted, or Google's result is stale
            if mid in here and mid not in taken:      # Google's times replace cinemaclock's for this film
                pool = taken[mid] = {}
                for g in groups:
                    if g["mid"] == mid and d in g["days"]:
                        for t, tix in g["days"].pop(d):
                            pool.setdefault(t, []).append(tix)
            if not mid:
                mid = movie_for(title, movies, by_key)
            pool = taken.get(mid, {})
            link = sched["links"].get(title, "")
            for kind, t in times:
                fmt = "" if kind.lower() == "standard" else kind
                ids = pool.get(t) or []
                tix = ids.pop(0) if ids else link     # each cinemaclock ticket id is used once
                g = extra.setdefault((mid, fmt), {"mid": mid, "tid": tid, "fmt": fmt, "notes": [], "days": {}, "src": "google"})
                g["days"].setdefault(d, []).append([t, tix])
                changed += tix == link
    groups[:] = [g for g in groups if g["days"]] + list(extra.values())
    return changed


# ---------- the build step ----------

def apply(cities, parsed, movies, state_path, log=print):
    """Top up `parsed` ({theater slug: groups}) and `movies` for the cities in FILL. Never raises:
    any failure is logged and the site keeps cinemaclock's data."""
    if not (api_key("serpapi") or api_key("searchapi")):
        log("Google fill: no API key, skipped")
        return
    state, budget = None, None
    try:
        try:
            state = json.loads(state_path.read_text())
            if not isinstance(state, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            log("Google fill: no saved state yet, starting fresh")
            state = {}
        except Exception as e:
            log(f"Google fill: saved state unreadable ({e}); starting fresh")
            state = {}
        state.setdefault("theaters", {})
        by_key = {}
        for mid, m in movies.items():
            if norm(m["title"]):
                by_key.setdefault(norm(m["title"]), mid)
        for slug, (location, tz) in FILL.items():
            city = cities.get(slug)
            if not city:
                continue
            try:
                today = dt.datetime.now(ZoneInfo(tz)).date()
                budget = budget or Budget(state, today)
                _fill_city(city, location, today, parsed, movies, by_key, state, budget, log)
            except Exception as e:                    # a fill problem must never break the build
                log(f"Google fill {slug}: failed ({type(e).__name__}: {e}); cinemaclock data kept")
    except Exception as e:
        log(f"Google fill: failed ({type(e).__name__}: {e}); cinemaclock data kept")
    finally:
        if state is not None:
            _save(state, state_path, log)
        if budget:
            log(f"Google fill: {budget.run} searches this build, {state['used']}/{MONTH_CAP} this month")


def _save(state, path, log):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(path)                             # atomic: a crash never leaves half a file
    except Exception as e:
        log(f"Google fill: could not save state ({e})")


def _age(entry, today):
    return (today - dt.date.fromisoformat(entry["fetched"])).days if entry else None


def _stale(entry, today):
    """Search again when: never searched; a miss older than its wait (miss_wait); a result older than
    REFRESH_DAYS; or a short result (Google gave fewer than 6 days, e.g. before the next week's
    times were posted) older than SHORT_DAYS. A normal result covers its search day + 6."""
    age = _age(entry, today)
    if age is None:
        return True
    if entry.get("none"):
        return age >= miss_wait(entry)
    last = max(entry["days"], default="")
    short = last < (dt.date.fromisoformat(entry["fetched"]) + dt.timedelta(5)).isoformat()
    return age >= REFRESH_DAYS or (short and age >= SHORT_DAYS)


def _fill_city(city, location, today, parsed, movies, by_key, state, budget, log):
    theaters = city["theaters"]
    empty = sum(1 for t in theaters if not any(_per_day(parsed.get(t["slug"], [])).values()))
    if theaters and empty / len(theaters) > OUTAGE:
        log(f"Google fill {city['slug']}: {empty}/{len(theaters)} theaters have no listings; "
            "looks like a cinemaclock outage, not searching")
        return
    dates = [(today + dt.timedelta(i)).isoformat() for i in range(7)]
    targets = [t for t in theaters if is_target(t, parsed.get(t["slug"], []), today)]
    filled = 0
    for t in targets:
        try:
            entry = state["theaters"].get(t["slug"])
            if _stale(entry, today) and budget.ok():
                provider, resp = google(f"{t['name']} showtimes", location, budget)
                if resp is not None:              # a search was spent: store what it found, even nothing
                    kg = (resp.get("knowledge_graph") or {}).get("title", "")
                    if resp.get("showtimes") and kg and same_theater(kg, t["name"]):
                        entry = {"fetched": today.isoformat(), "via": provider, **theater_schedule(resp, today)}
                    else:
                        misses = entry.get("misses", 1) + 1 if entry and entry.get("none") else 1
                        why = (("showtimes for another name: " + kg) if resp.get("showtimes") and kg
                               else (kg + ", no showtimes") if kg else "no showtimes" if resp else "error")
                        entry = {"fetched": today.isoformat(), "via": provider, "none": True, "days": {},
                                 "links": {}, "got": why, "misses": misses}
                        log(f"  Google fill: nothing usable for {t['name']} ({why}); "
                            f"miss {misses}, retry in {miss_wait(entry)} days")
                    state["theaters"][t["slug"]] = entry
            if not entry or entry.get("none") or _age(entry, today) >= REFRESH_DAYS:
                continue
            groups = parsed.setdefault(t["slug"], [])
            n = merge(groups, t["id"], entry, dates, movies, by_key, override=_age(entry, today) <= OVERRIDE_DAYS)
            filled += 1
            log(f"  Google fill: {t['name']}: {n} showtimes added or changed")
        except Exception as e:
            log(f"  Google fill: {t['name']}: failed ({type(e).__name__}: {e}); cinemaclock data kept")
    log(f"Google fill {city['slug']}: {filled}/{len(targets)} target theaters filled")
