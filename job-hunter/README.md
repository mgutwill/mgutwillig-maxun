# Job hunter (Ajax/Durham + GTA, account management)

Pipeline: collect -> dedupe -> hard filters (<=7 days, location/work-mode, salary floor, bad titles)
-> score (title 30, industry 25, resume skills 20, location 15, salary 10) -> shortlist.md/csv.

## Run
    python3 jobhunter.py run --from sample_jobs.json          # offline demo (tested)
    pip install playwright && playwright install chromium
    python3 jobhunter.py probe eluta                          # first time: check the scraper sees cards
    python3 jobhunter.py run --scrape                         # daily
    python3 jobhunter.py run --scrape --llm --resume resume.txt   # + Claude re-rank (ANTHROPIC_API_KEY)

Maxun alternative: build robots in Maxun on the same two filtered URLs, export JSON
(title, company, location, posted, url, description) and use `run --from export.json`.

Edit `profile.json` to change floor, cities, keywords, weights.
Status: scoring/filtering tested on sample data. Scrapers + LLM step NOT tested on live services.
