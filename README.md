# Showtimes

A Google-style movie showtimes page: every nearby theater and movie, a week of times,
Rotten Tomatoes and Letterboxd scores, and a tap-to-buy link on each showing.

A GitHub Actions job rebuilds every city in `cities.txt` twice a day and publishes the site
with GitHub Pages. Each city covers theaters within 20 miles of its nearest theater, capped at
the nearest 25. Tap the city name in the title to switch to another covered city.

Run locally (Python 3, standard library only):

```
python3 showtimes.py                      # one self-contained Reno page; opens in the browser
python3 showtimes.py sacramento ca --radius 10
python3 showtimes.py --json               # parsed data, no page
python3 showtimes.py --site _site         # the multi-city site, from cities.txt
```

Showtimes and ticket links come from cinemaclock.com; scores from Rotten Tomatoes and
Letterboxd. For the cities in `gfill.py` (Houston and Reno), the CI build also takes Google's
showtimes for theaters cinemaclock gets wrong, through SerpApi or SearchApi.io: about one search
per such theater per week, capped per build and per month in that file, which says what is searched
and why. It needs the `SERPAPI_KEY` /
`SEARCHAPI_KEY` repository secrets and searches nothing without them. The parser reads those sites' page markup, so a redesign on their side breaks it.
