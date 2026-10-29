# News Minimalist Crawler

Fetches items from:

https://www.newsminimalist.com/?category=all&from=5&to=10&sort=latest

For every item on the target UTC date, it writes:

- title
- News Minimalist time
- original source link
- 100-word English summary from the source article

If the first source article cannot be fetched, it tries the remaining source links
listed under the same News Minimalist item, in page order.

## Run Manually

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\newsminimalist_crawler.py --once
```

Force a rerun for the current UTC date:

```powershell
.\.venv\Scripts\python.exe .\newsminimalist_crawler.py --force
```

Outputs are written to `D:\Git\News\output`.
