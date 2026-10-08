#!/usr/bin/env python3
"""Job pipeline: collect -> dedupe -> hard filters -> score -> shortlist.

  python jobhunter.py run --from sample_jobs.json      # score jobs from a file (works offline)
  python jobhunter.py run --scrape                      # scrape Eluta + Hiring Cafe (needs playwright)
  python jobhunter.py probe eluta                       # save HTML/screenshot to tune selectors
"""
import argparse, csv, json, re, sys
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).parent
PROFILE = json.loads((HERE / "profile.json").read_text())
STATE = HERE / "seen.json"


@dataclass
class Job:
    title: str = ""
    company: str = ""
    location: str = ""
    posted: str = ""
    url: str = ""
    description: str = ""
    source: str = ""
    # derived
    age_days: int | None = None
    mode: str = ""          # remote | hybrid | onsite | unknown
    salary_note: str = ""
    score: float = 0.0
    why: list = field(default_factory=list)
    flags: list = field(default_factory=list)


# ---------- parsing helpers ----------
def parse_age(text: str, first_seen: str | None) -> int | None:
    t = (text or "").lower().strip()
    if not t:
        pass
    elif re.search(r"today|just posted|hour|minute|moments? ago", t):
        return 0
    elif m := re.search(r"(\d+)\+?\s*day", t):
        return int(m.group(1)) + (999 if "+" in m.group(0) else 0)
    elif m := re.search(r"(\d+)\+?\s*week", t):
        return int(m.group(1)) * 7
    elif re.search(r"month", t):
        return 30
    elif re.search(r"yesterday", t):
        return 1
    else:
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%b %d, %Y", "%B %d, %Y"):
            try:
                return (date.today() - datetime.strptime(t[:len(t)], fmt).date()).days
            except ValueError:
                continue
    # undated: fall back to when *we* first saw it
    if first_seen:
        return (date.today() - date.fromisoformat(first_seen)).days
    return None


def parse_salary(text: str):
    """Return (min, max) annual CAD or None. Handles $75,000-$90,000, $75K, $35/hour."""
    t = (text or "").replace(",", "")
    nums = []
    for m in re.finditer(r"\$\s*(\d+(?:\.\d+)?)\s*(k)?(?:\s*(?:/|per)\s*(hour|hr|h))?", t, re.I):
        v = float(m.group(1))
        if m.group(2):
            v *= 1000
        if m.group(3) or v < 200:          # hourly
            v *= 2080
        if v >= 20000:
            nums.append(v)
    return (min(nums), max(nums)) if nums else None


def detect_mode(job: Job) -> str:
    t = f"{job.title} {job.location} {job.description[:1500]}".lower()
    if re.search(r"hybrid", t):
        return "hybrid"
    if re.search(r"\bremote\b|work from home|wfh", t):
        return "remote"
    if re.search(r"on-?site|in[- ]office|in person", t):
        return "onsite"
    return "unknown"


def cities_in(text: str):
    t = text.lower()
    return [c for c in PROFILE["hybrid_ok_cities"] if c in t]


def job_key(j: Job) -> str:
    if j.url:
        u = urlsplit(j.url)
        return f"{u.netloc}{u.path}".lower().rstrip("/")
    return re.sub(r"\W+", "", f"{j.company}{j.title}{j.location}".lower())


# ---------- filters + scoring ----------
def evaluate(j: Job, first_seen: str | None) -> bool:
    """Fill derived fields. Return False if the job should be dropped."""
    P = PROFILE
    title = f" {j.title.lower()} "
    blob = f"{j.title} {j.company} {j.location} {j.description}".lower()

    if any(x in title for x in P["title_exclude"]):
        return False

    j.age_days = parse_age(j.posted, first_seen)
    if j.age_days is not None and j.age_days > P["max_age_days"]:
        return False
    if j.age_days is None:
        j.flags.append("date unknown")

    # location
    j.mode = detect_mode(j)
    loc_cities = cities_in(f"{j.location} {j.title}")
    loc_score = 0
    if j.mode == "remote":
        loc_score = 1.0
        j.why.append("remote")
        if not any(r in blob for r in P["remote_ok_regions"]):
            j.flags.append("remote region unclear")
    elif j.mode == "hybrid":
        if loc_cities:
            loc_score = 0.9
            j.why.append(f"hybrid in {loc_cities[0].title()}")
        else:
            return False
    else:  # onsite or unknown
        onsite_ok = [c for c in loc_cities if c in P["onsite_ok_cities"]]
        if onsite_ok:
            loc_score = 0.6
            j.why.append(f"on-site in {onsite_ok[0].title()}")
            if j.mode == "unknown":
                j.flags.append("work mode unclear")
        elif loc_cities and not P["drop_onsite_in_other_cities"]:
            loc_score = 0.2
        else:
            return False

    # salary (annual base)
    sal = parse_salary(f"{j.description} {j.salary_note}")
    sal_score = 0.5
    if sal:
        lo, hi = sal
        j.salary_note = f"${lo:,.0f}-${hi:,.0f}" if lo != hi else f"${hi:,.0f}"
        if hi < P["salary_floor_base"]:
            return False
        sal_score = 1.0 if lo >= P["salary_floor_base"] else 0.6
        if lo < P["salary_floor_base"]:
            j.flags.append("range dips below floor")
    else:
        j.flags.append("no salary listed")

    # title
    if any(x in title for x in P["title_strong"]):
        title_score = 1.0
    elif any(x in title for x in P["title_ok"]):
        title_score = 0.6
    else:
        return False

    # industry
    hits = [k for k, words in P["industries"].items() if any(w in blob for w in words)]
    ind_score = min(1.0, 0.6 + 0.2 * (len(hits) - 1)) if hits else 0.0
    if hits:
        j.why.append("industry: " + "/".join(hits))
    else:
        j.flags.append("industry unclear")

    # skills overlap (resume-derived keywords found in the posting)
    sk = [s for s in P["skills"] if s in blob]
    skill_score = min(1.0, len(sk) / 6)
    if sk:
        j.why.append("skills: " + ", ".join(sk[:5]))

    W = P["weights"]
    j.score = round(
        W["title"] * title_score + W["industry"] * ind_score + W["skills"] * skill_score
        + W["location"] * loc_score + W["salary"] * sal_score, 1)
    return True


# ---------- optional LLM re-rank (needs ANTHROPIC_API_KEY) ----------
def llm_rerank(jobs: list[Job], resume_path: Path, model: str = "claude-sonnet-5-5"):
    import os, urllib.request
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY not set; skipping LLM re-rank", file=sys.stderr)
        return
    resume = resume_path.read_text()
    for j in jobs:
        prompt = (f"Resume:\n{resume}\n\nJob: {j.title} at {j.company} ({j.location})\n"
                  f"{j.description[:3500]}\n\nReturn JSON only: "
                  '{"fit": <1-10 integer>, "reason": "<one sentence>", "gaps": "<one sentence>"}')
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({"model": model, "max_tokens": 300,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            out = json.load(urllib.request.urlopen(req, timeout=60))["content"][0]["text"]
            r = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
            j.score = round(j.score * 0.4 + r["fit"] * 10 * 0.6, 1)
            j.why.insert(0, f"LLM {r['fit']}/10: {r['reason']}")
            if r.get("gaps"):
                j.flags.append("gap: " + r["gaps"])
        except Exception as e:  # keep rule-based score on any failure
            j.flags.append(f"llm failed: {type(e).__name__}")


# ---------- pipeline ----------
def run(jobs_raw: list[dict], use_llm: bool, resume: Path | None):
    seen = json.loads(STATE.read_text()) if STATE.exists() else {}
    today = date.today().isoformat()

    uniq: dict[str, Job] = {}
    for d in jobs_raw:
        j = Job(**{k: v for k, v in d.items() if k in Job.__dataclass_fields__})
        uniq.setdefault(job_key(j), j)          # dedupe across sources/runs

    kept = []
    for k, j in uniq.items():
        seen.setdefault(k, today)
        if evaluate(j, seen[k]):
            kept.append(j)
    STATE.write_text(json.dumps(seen, indent=1))

    kept.sort(key=lambda j: -j.score)
    top = kept[: PROFILE["shortlist_size"]]
    if use_llm and resume:
        llm_rerank(top, resume)
        top.sort(key=lambda j: -j.score)

    write_outputs(top)
    print(f"{len(jobs_raw)} raw -> {len(uniq)} unique -> {len(kept)} passed filters "
          f"-> {len(top)} shortlisted. See shortlist.md / shortlist.csv")


def write_outputs(top: list[Job]):
    with open(HERE / "shortlist.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["score", "title", "company", "location", "mode", "posted", "salary", "url", "why", "flags"])
        for j in top:
            w.writerow([j.score, j.title, j.company, j.location, j.mode, j.posted,
                        j.salary_note, j.url, "; ".join(j.why), "; ".join(j.flags)])
    lines = [f"# Shortlist ({date.today()}) - {len(top)} roles\n"]
    for i, j in enumerate(top, 1):
        lines += [f"## {i}. {j.title} - {j.company}  ({j.score})",
                  f"{j.location} | {j.mode} | posted {j.posted or '?'} | {j.salary_note or 'salary not listed'}",
                  f"{j.url}", f"- Why: {'; '.join(j.why)}"]
        if j.flags:
            lines.append(f"- Check: {'; '.join(j.flags)}")
        lines.append("")
    (HERE / "shortlist.md").write_text("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--from", dest="src", help="JSON file of jobs (e.g. a Maxun export)")
    r.add_argument("--scrape", action="store_true")
    r.add_argument("--llm", action="store_true", help="re-rank with Claude (ANTHROPIC_API_KEY)")
    r.add_argument("--resume", default="resume.txt")
    p = sub.add_parser("probe")
    p.add_argument("source", choices=["eluta", "hiringcafe"])
    a = ap.parse_args()

    if a.cmd == "probe":
        from sources import probe
        probe(a.source)
        return
    raw = []
    if a.src:
        raw += json.loads(Path(a.src).read_text())
    if a.scrape:
        from sources import scrape_all
        raw += scrape_all()
    if not raw:
        sys.exit("No input. Use --from FILE and/or --scrape.")
    run(raw, a.llm, Path(a.resume) if a.llm else None)


if __name__ == "__main__":
    main()
