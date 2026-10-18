# Showtimes

A Google-style movie showtimes page: every nearby theater and movie, a week of times,
Rotten Tomatoes and Letterboxd scores, and a tap-to-buy link on each showing.

A GitHub Actions job rebuilds the Reno page twice a day and publishes it with GitHub Pages.

Run locally (Python 3, standard library only):

```
python3 showtimes.py                      # Reno, NV; opens in the browser
python3 showtimes.py sacramento ca --radius 10
python3 showtimes.py --json               # parsed data, no page
```

Showtimes and ticket links come from cinemaclock.com; scores from Rotten Tomatoes and
Letterboxd. The parser reads those sites' page markup, so a redesign on their side breaks it.
