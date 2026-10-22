# Morning papers

A page that updates itself every morning with the new papers from your feeds, grouped by day,
with a search box. It runs free on GitHub: no server, no cost.

## Setup (about 10 minutes, once)

1. **Create a GitHub repo.** New repository, name it anything (e.g. `morning-papers`), set it to
   **Public** (GitHub Pages on private repos needs a paid plan). The page will only contain paper
   titles and abstracts from public feeds.
2. **Put these files in it.** The `.github` folder is hidden on Mac and must be included. From a terminal:

       cd morning-papers
       git init -b main
       git add .
       git commit -m "Initial commit"
       git remote add origin https://github.com/YOUR-USERNAME/morning-papers.git
       git push -u origin main

   (GitHub Desktop works too: add the folder as a local repository and publish it.)
3. **Turn on Pages.** Repo > Settings > Pages > Source: *Deploy from a branch* > Branch: `main`, folder `/docs` > Save.
4. **Run it once now.** Repo > Actions > "Update morning papers" > Run workflow. Wait ~1-2 minutes.
   If Actions asks you to enable workflows, click the green button.
5. **Open your page:** `https://YOUR-USERNAME.github.io/morning-papers/` and bookmark it
   (add it to your phone's home screen too).

After that it refreshes every day around 7:15 AM Pacific. Scheduled runs can lag by up to an hour.

## Using it

- Newest day is on top; today's new papers are labeled "New today".
- "New" means new to this page, not the publisher's date, so weekends and late-indexed papers
  never fall through the cracks.
- Papers stay for 14 days. Use the search box to filter by keyword.
- The first run only looks back 3 days, so the first page is not flooded.

## Check it after the first run

Scroll to the bottom of the page and open **Feed status**. Any feed that says
"no entries (bad URL, blocked, or empty)" is not working. Several publisher URLs (Science, Cell Press,
Wiley, Springer, Lancet) could not be verified when this was built, and some publishers block
automated requests. For a broken feed, find its working URL in your feed reader and replace it
in `build_digest.py` (the lists near the top).

## Tuning

- Add or remove feeds: edit `UNFILTERED` in `build_digest.py` (shown unfiltered) and `FEEDS` in
  `filter_feeds.py` (keyword-filtered).
- Change what the filter keeps: edit the keyword lists at the top of `filter_feeds.py`.
- Change how many days are kept: `--keep-days` in `.github/workflows/daily.yml`'s run step
  (e.g. `python3 build_digest.py --keep-days 30`).
- Run it locally: `pip install feedparser && python3 build_digest.py`, then open `docs/index.html`.

## If it stops updating

GitHub pauses scheduled workflows in repos with no activity for 60 days and emails you when it does.
The daily commits normally keep it active; if it is paused anyway, re-enable it in the Actions tab.
