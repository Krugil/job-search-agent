"""
Job Search Agent (read-only) - Elijah's engineering technician search
========================================================================
This script does NOT browse websites with a fake mouse or fill out
applications. It:

  1. Queries the Adzuna job API (a licensed aggregator - not scraping)
     for engineering-technician-type roles near (location)
  2. Generates direct search links for company career pages (Biogen,
     Wolfspeed, Lilly, Novartis, Siemens) so you can check those by hand.
  3. Sends the Adzuna results to Gemini ONCE per batch (a normal text
     completion, not computer use) to score them against your profile.
     Uses Gemini's free tier - no billing required.
  4. Saves everything to a text file.

SETUP (one time):
  1. pip install requests google-generativeai
  2. Get a free Adzuna API key: https://developer.adzuna.com/
  3. Use your existing Gemini API key (same one from Voltage Brief)
  4. Set env vars (see bottom of this file), OR paste directly into CONFIG below.

RUN:
  python job_search_agent.py
"""

import os
import sys
import json
import time
import requests
from datetime import datetime

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Ranking backend: "gemini" (cloud, free tier can be flaky/rate-limited)
# or "ollama" (runs locally, no rate limits, needs Ollama running with
# a real model pulled - e.g. qwen3.6 or deepseek-r1). This task is pure
# text reasoning over data Adzuna already fetched, so a local model
# doesn't need live internet access to do it well.
RANKING_BACKEND = os.environ.get("RANKING_BACKEND", "gemini")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.6")
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "180"))

LOCATION = os.environ.get("JOB_SEARCH_ZIP", "27601")
STATE = os.environ.get("JOB_SEARCH_STATE", "NC")
RADIUS_MILES = int(os.environ.get("JOB_SEARCH_RADIUS_MILES", "40"))
CENTER_ADDRESS = os.environ.get("JOB_SEARCH_CENTER_ADDRESS", "")
RESULTS_PER_TITLE = 20

JOB_TITLES = [
    "engineering technician",
    "manufacturing technician",
    "lab technician",
    "process technician",
    "test technician",
    "automation technician",
    "semiconductor technician",
    "pharmaceutical technician",
]

# NOTE: this list does NOT restrict the search in any way - the search
# covers every company Adzuna indexes. This is only used to give a small
# scoring bonus + a "preferred employer" flag when one of these companies
# happens to show up.
PREFERRED_COMPANIES = [
    "Biogen",
    "Wolfspeed",
    "Eli Lilly",
    "Novartis",
    "Siemens",
    "MACOM",
    "Novo Nordisk",
    "Eaton",
    "Amentum",
    "Duke Health",
    "Randstad",
    "Condustrial",
    "Ultimate Staffing",
]


def load_candidate_profile():
    """Loads your profile from candidate_profile.txt (kept out of git
    via .gitignore, never committed). If that file doesn't exist yet,
    falls back to a placeholder so the script still runs and tells you
    what to do."""
    profile_path = os.environ.get("CANDIDATE_PROFILE_PATH", "candidate_profile.txt")
    if os.path.exists(profile_path):
        with open(profile_path, "r", encoding="utf-8") as f:
            return f.read()
    return """
[No candidate_profile.txt found - create one in this folder with your
own background, e.g.:]

Name: Your Name
Location: Your City, State
Education: ...
Skills: ...
Projects: ...
Experience: ...
Target: ...
"""


CANDIDATE_PROFILE = load_candidate_profile()

OUTPUT_FILE = os.environ.get("JOB_SEARCH_OUTPUT", "job_search_results.txt")

COMPANY_CAREER_LINKS = {
    "Biogen": "https://www.biogen.com/careers.html",
    "Wolfspeed": "https://careers.wolfspeed.com/",
    "Eli Lilly": "https://careers.lilly.com/us/en/search-results",
    "Novartis": "https://www.novartis.com/careers/career-search",
    "Siemens": "https://jobs.siemens.com/careers",
    "MACOM": "https://macomtech.csod.com/ux/ats/careersite/4/home?c=macomtech&country=us&state=nc&city=durham_morrisville",
    "Novo Nordisk": "https://www.novonordisk.com/careers/find-a-job/career-search-results.html?stateOrProvinces=North+Carolina+%28NC%29",
    "Eaton": "https://www.eaton.com/us/en-us/company/careers/experienced-professionals/engineering.html",
    "Amentum": "https://www.amentumcareers.com/jobs/search?page=1&country_codes%5B%5D=US&states%5B%5D=North+Carolina",
    "Duke Health": "https://careers.duke.edu/",
    "Randstad": "https://www.randstadusa.com/jobs/",
    "Condustrial": "https://www.indeed.com/cmp/Condustrial-Inc./jobs",
    "Ultimate Staffing": "https://www.indeed.com/cmp/Ultimate-Staffing-Services/jobs",
}


def check_config():
    missing = []
    if not ADZUNA_APP_ID:
        missing.append("ADZUNA_APP_ID")
    if not ADZUNA_APP_KEY:
        missing.append("ADZUNA_APP_KEY")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if missing:
        print("Missing required config: " + ", ".join(missing))
        print("Set these as environment variables before running, or edit")
        print("the CONFIG section at the top of this script directly.")
        sys.exit(1)

    if not CENTER_ADDRESS:
        print("Note: JOB_SEARCH_CENTER_ADDRESS is not set - distance/map")
        print("features will be skipped this run. Set it as an env var to enable them.")
    if not os.path.exists(os.environ.get("CANDIDATE_PROFILE_PATH", "candidate_profile.txt")):
        print("Note: candidate_profile.txt not found - using a placeholder profile.")
        print("Create that file in this folder with your real background for real scoring.\n")


def geocode_center():
    """One-time lookup of CENTER_ADDRESS's lat/long, used only to measure
    how far each job is from that point. Uses OpenStreetMap's free
    Nominatim service - no API key needed."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": CENTER_ADDRESS, "format": "json", "limit": 1},
            headers={"User-Agent": "elijah-job-search-agent"},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as e:
        print(f"  Could not geocode center address ({e}) - distance sorting disabled this run.")
    return None, None


def haversine_miles(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, sqrt, atan2
    R = 3958.8  # Earth radius in miles
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def search_adzuna(job_title):
    url = "https://api.adzuna.com/v1/api/jobs/us/search/1"
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "results_per_page": RESULTS_PER_TITLE,
        "what": job_title,
        "where": f"{LOCATION} {STATE}",
        "distance": RADIUS_MILES,
        "content-type": "application/json",
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])
    except requests.exceptions.RequestException as e:
        print(f"  Adzuna request failed for '{job_title}': {e}")
        return []
    except json.JSONDecodeError:
        print(f"  Adzuna returned unexpected data for '{job_title}'")
        return []


def normalize_job(raw, center_lat, center_lon):
    company = raw.get("company", {}).get("display_name", "Unknown")
    location = raw.get("location", {}).get("display_name", "Unknown")
    salary_min = raw.get("salary_min")
    salary_max = raw.get("salary_max")
    if salary_min and salary_max:
        pay = f"${salary_min:,.0f} - ${salary_max:,.0f} (annualized)"
    else:
        pay = "Not listed"

    distance_miles = None
    job_lat = raw.get("latitude")
    job_lon = raw.get("longitude")
    if center_lat is not None and job_lat is not None and job_lon is not None:
        distance_miles = round(haversine_miles(center_lat, center_lon, job_lat, job_lon), 1)

    return {
        "title": raw.get("title", "Unknown"),
        "company": company,
        "location": location,
        "pay": pay,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "description": (raw.get("description", "") or "")[:1500],
        "url": raw.get("redirect_url", ""),
        "distance_miles": distance_miles,
        "lat": job_lat,
        "lon": job_lon,
    }


def collect_all_jobs():
    center_lat, center_lon = geocode_center()
    if center_lat is not None:
        print(f"Measuring distance from: {CENTER_ADDRESS}\n")

    all_jobs = []
    seen_urls = set()
    for title in JOB_TITLES:
        print(f"Searching Adzuna for '{title}' within {RADIUS_MILES} miles...")
        raw_results = search_adzuna(title)
        for raw in raw_results:
            job = normalize_job(raw, center_lat, center_lon)
            if job["url"] and job["url"] not in seen_urls:
                seen_urls.add(job["url"])
                all_jobs.append(job)
        time.sleep(0.5)
    return all_jobs, center_lat, center_lon


def call_with_timeout(func, timeout_seconds):
    """Runs func() in a daemon thread with a hard timeout. Unlike
    ThreadPoolExecutor, a daemon thread can never block the script from
    exiting even if the underlying call hangs forever."""
    import threading
    result = {}

    def target():
        try:
            result["value"] = func()
        except Exception as e:
            result["error"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout_seconds)

    if thread.is_alive():
        return None, "timeout"
    if "error" in result:
        return None, result["error"]
    return result.get("value"), None


RADAR_AXES = ["pay", "experience", "skills", "location", "growth", "trajectory"]
RADAR_LABELS = {
    "pay": "Pay Floor",
    "experience": "Experience Realism",
    "skills": "Core Skill Match",
    "location": "Location/Commute",
    "growth": "Growth Potential",
    "trajectory": "Engineering Trajectory",
}
PAY_FLOOR_MIN = 47000   # ~$23/hr annualized
PAY_FLOOR_TARGET = 52000  # ~$25/hr annualized


def compute_pay_score(job):
    """Computed directly from Adzuna's own salary fields - no AI
    guessing, since we already have the real numbers."""
    salary_min = job.get("salary_min")
    if not salary_min:
        return 5, "No pay listed - verify on posting"
    if salary_min >= PAY_FLOOR_TARGET:
        return 10, f"${salary_min:,.0f}+ meets your target floor"
    if salary_min >= PAY_FLOOR_MIN:
        return 8, f"${salary_min:,.0f} is right at your floor"
    if salary_min >= 40000:
        return 5, f"${salary_min:,.0f} is below your ${PAY_FLOOR_MIN:,} target"
    if salary_min >= 32000:
        return 3, f"${salary_min:,.0f} is well below your target"
    return 1, f"${salary_min:,.0f} - collapses below your floor"


def compute_location_score(job):
    """Computed directly from real distance already calculated via
    Haversine - no AI guessing about commute distance."""
    dist = job.get("distance_miles")
    if dist is None:
        return 5, "Distance unknown"
    if dist <= 10:
        return 10, f"{dist} mi - very close"
    if dist <= 20:
        return 8, f"{dist} mi - reasonable commute"
    if dist <= 30:
        return 6, f"{dist} mi - moderate commute"
    if dist <= 40:
        return 4, f"{dist} mi - long commute"
    return 2, f"{dist} mi - outside comfortable range"


def build_ranking_prompt(batch):
    listing_text = ""
    for idx, job in enumerate(batch):
        listing_text += (
            f"\n[{idx}] Title: {job['title']}\n"
            f"Company: {job['company']}\n"
            f"Description: {job['description']}\n"
        )
    return f"""Here is a candidate profile:
{CANDIDATE_PROFILE}

Here is a list of job listings, each with an index number in brackets:
{listing_text}

For each listing, score these 4 criteria from 1-10, based ONLY on what
is explicitly stated in the description text above. Do not guess or
invent details that aren't there - if the description doesn't address
a criterion, score it 5 (neutral) and say "not stated in posting"
rather than assuming.

1. experience_score: Does it ask for realistic entry/mid-level
   experience (0-3 years)? Score low (1-3) if it demands 8+ years.
2. skills_score: Does the description mention needing skills the
   candidate actually has (Python, mechanical assembly, basic
   circuits, SOP compliance)? Score low if it requires specific
   tools/software the candidate doesn't list (e.g. specific brand
   PLC software, CAD packages not mentioned in the profile).
3. growth_score: Is this hands-on equipment, robotics, cleanroom, or
   process control work (score high), or basic manual/assembly labor
   with little technical growth (score low)?
4. trajectory_score: Does the posting mention tuition reimbursement,
   internal promotion paths, or engineering-track development? Score
   5 (neutral) if not mentioned at all - don't assume it's absent,
   just note it wasn't stated.

Respond with ONLY a JSON array, no other text, no markdown code fences:
[{{"index": 0, "experience_score": 7, "skills_score": 8, "growth_score": 6,
   "trajectory_score": 5, "reason": "short overall reason"}}, ...]
"""


def parse_ranking_response(result_text, batch, scored_jobs):
    result_text = result_text.strip()
    if result_text.startswith("```"):
        result_text = result_text.strip("`")
        if result_text.startswith("json"):
            result_text = result_text[4:]
    start = result_text.find("[")
    end = result_text.rfind("]") + 1
    scores = json.loads(result_text[start:end])
    for s in scores:
        idx = s.get("index")
        if idx is not None and 0 <= idx < len(batch):
            job = batch[idx]
            pay_score, pay_note = compute_pay_score(job)
            loc_score, loc_note = compute_location_score(job)
            job["radar"] = {
                "pay": pay_score,
                "experience": s.get("experience_score", 5),
                "skills": s.get("skills_score", 5),
                "location": loc_score,
                "growth": s.get("growth_score", 5),
                "trajectory": s.get("trajectory_score", 5),
            }
            job["radar_notes"] = {"pay": pay_note, "location": loc_note}
            job["match_score"] = round(sum(job["radar"].values()) / len(job["radar"]))
            job["reason"] = s.get("reason", "")
            scored_jobs.append(job)


def mark_unscored(batch, scored_jobs, reason):
    for job in batch:
        job["match_score"] = 0
        job["radar"] = {axis: 0 for axis in RADAR_AXES}
        job["radar_notes"] = {}
        job["reason"] = reason
        scored_jobs.append(job)


def process_one_batch(batch, batch_num, total_batches, call_model_fn, max_retries, timeout_seconds):
    """Handles one batch's retry loop. Runs inside a worker thread so
    multiple batches can be in flight to the API at the same time."""
    prompt = build_ranking_prompt(batch)
    scored = []

    for attempt in range(1, max_retries + 1):
        result_text, err = call_with_timeout(lambda: call_model_fn(prompt), timeout_seconds)

        if err == "timeout":
            print(f"\n  Batch {batch_num}: attempt {attempt} timed out after {timeout_seconds}s.")
        elif err is not None:
            print(f"\n  Batch {batch_num}: attempt {attempt} failed ({err}).")
        else:
            try:
                parse_ranking_response(result_text, batch, scored)
                return scored
            except Exception as e:
                print(f"\n  Batch {batch_num}: attempt {attempt} couldn't parse response ({e}).")

    print(f"\n  Batch {batch_num}/{total_batches}: failed after {max_retries} attempts.")
    mark_unscored(batch, scored, "Not scored (failed after retries)")
    return scored


def print_progress_bar(done, total, elapsed_seconds, bar_width=30):
    frac = done / total if total else 1
    filled = int(bar_width * frac)
    bar = "█" * filled + "░" * (bar_width - filled)
    pct = int(frac * 100)
    print(f"\r  [{bar}] {pct:3d}%  ({done}/{total} batches, {elapsed_seconds:.0f}s elapsed)  ", end="", flush=True)


def rank_batches(jobs, call_model_fn, batch_size=20, max_retries=2, timeout_seconds=35, concurrency=4):
    """Runs batches concurrently (not one-at-a-time) since these are
    independent network calls - this is the main speed lever. Each
    batch still has its own retry+timeout handling internally."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    batches = [jobs[i:i + batch_size] for i in range(0, len(jobs), batch_size)]
    total_batches = len(batches)
    scored_jobs = []
    start = time.time()

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                process_one_batch, batch, i + 1, total_batches, call_model_fn, max_retries, timeout_seconds
            ): i
            for i, batch in enumerate(batches)
        }
        done = 0
        print_progress_bar(0, total_batches, 0)
        for future in as_completed(futures):
            scored_jobs.extend(future.result())
            done += 1
            print_progress_bar(done, total_batches, time.time() - start)
    print()  # newline after the bar finishes

    return scored_jobs


def rank_jobs_with_gemini(jobs):
    if not jobs:
        return []
    from google import genai
    client = genai.Client(api_key=GEMINI_API_KEY)

    def call_model(prompt):
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return response.text

    scored_jobs = rank_batches(jobs, call_model, batch_size=20, timeout_seconds=35, concurrency=4)
    return finalize_ranking(scored_jobs)


def rank_jobs_with_ollama(jobs):
    if not jobs:
        return []

    def call_model(prompt):
        resp = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=OLLAMA_TIMEOUT_SECONDS + 5,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")

    scored_jobs = rank_batches(
        jobs, call_model,
        batch_size=10,
        timeout_seconds=OLLAMA_TIMEOUT_SECONDS,
        concurrency=1,  # a single local GPU can't usefully run these in parallel
    )
    return finalize_ranking(scored_jobs)


def build_radar_svg(radar_scores, size=130):
    """Small hexagon radar chart: fuller/rounder = better all-around fit.
    Axis order matches RADAR_AXES. No labels on the mini chart itself
    (kept iconic/quick-glance) - full breakdown is in the tooltip."""
    n = len(RADAR_AXES)
    cx = cy = size / 2
    max_r = size / 2 - 18

    def point_at(axis_index, value_0_to_10):
        angle = -math.pi / 2 + (2 * math.pi / n) * axis_index
        r = (value_0_to_10 / 10) * max_r
        return cx + r * math.cos(angle), cy + r * math.sin(angle)

    # Outer boundary hexagon (max possible, for reference)
    outer_pts = " ".join(f"{point_at(i, 10)[0]:.1f},{point_at(i, 10)[1]:.1f}" for i in range(n))

    # Filled polygon at actual scores
    score_pts = " ".join(
        f"{point_at(i, radar_scores.get(axis, 0))[0]:.1f},{point_at(i, radar_scores.get(axis, 0))[1]:.1f}"
        for i, axis in enumerate(RADAR_AXES)
    )

    dots = ""
    for i, axis in enumerate(RADAR_AXES):
        x, y = point_at(i, radar_scores.get(axis, 0))
        dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="#2dd4bf"/>'

    tooltip = " · ".join(f"{RADAR_LABELS[a]}: {radar_scores.get(a, 0)}/10" for a in RADAR_AXES)

    return f"""<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">
      <title>{tooltip}</title>
      <polygon points="{outer_pts}" fill="none" stroke="#30363d" stroke-width="1"/>
      <polygon points="{score_pts}" fill="#2dd4bf" fill-opacity="0.35" stroke="#2dd4bf" stroke-width="1.5"/>
      {dots}
    </svg>"""


def finalize_ranking(scored_jobs):

    # Preferred-company bonus (never a filter - just nudges score up
    # slightly and flags it, since these are companies Elijah already
    # researched and cares about)
    for job in scored_jobs:
        company_lower = job.get("company", "").lower()
        is_preferred = any(pc.lower() in company_lower for pc in PREFERRED_COMPANIES)
        if is_preferred:
            job["match_score"] = min(10, job.get("match_score", 0) + 1)
            job["reason"] = f"⭐ Preferred employer. {job.get('reason', '')}"

    # Sort by score first, then by distance (closer = better) as a tiebreaker
    scored_jobs.sort(
        key=lambda j: (
            -j.get("match_score", 0),
            j.get("distance_miles") if j.get("distance_miles") is not None else 9999,
        )
    )
    return scored_jobs


import math


def bearing_and_radius(job, center_lat, center_lon, max_miles, max_px):
    """Position a job on the radar: angle = compass bearing from you,
    distance from center = proportional to real miles away."""
    if job.get("lat") is None or job.get("lon") is None or center_lat is None:
        return None
    dx = (job["lon"] - center_lon) * math.cos(math.radians(center_lat))
    dy = job["lat"] - center_lat
    angle_rad = math.atan2(dx, dy)  # 0 = north, clockwise
    dist = job.get("distance_miles") or 0
    radius_px = min(dist / max_miles, 1.0) * max_px
    cx = 250 + radius_px * math.sin(angle_rad)
    cy = 250 - radius_px * math.cos(angle_rad)
    return cx, cy


def write_html_report(jobs, center_lat, center_lon, run_label):
    """Generates one timestamped report file per run, plus refreshes
    an index.html that lists every past run (newest first)."""
    reports_dir = os.path.dirname(OUTPUT_FILE) or "."
    filename = f"job_search_{run_label}.html"
    html_path = os.path.join(reports_dir, filename)
    display_time = datetime.now().strftime("%B %d, %Y — %I:%M %p")

    def score_color(score):
        if score >= 8:
            return "#1a7f37"
        elif score >= 6:
            return "#9a6700"
        else:
            return "#5b6470"

    # --- Marker data for the map (as JSON for the Leaflet script) ---
    map_markers = []
    for job in jobs[:30]:
        if job.get("lat") is None or job.get("lon") is None:
            continue
        map_markers.append({
            "lat": job["lat"],
            "lon": job["lon"],
            "color": score_color(job.get("match_score", 0)),
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "distance": job.get("distance_miles"),
            "score": job.get("match_score", 0),
            "url": job.get("url", ""),
        })
    map_markers_json = json.dumps(map_markers)
    has_center = center_lat is not None and center_lon is not None

    # --- Table rows ---
    rows_html = ""
    for i, job in enumerate(jobs[:30], 1):
        score = job.get("match_score", 0)
        color = score_color(score)
        dist = job.get("distance_miles")
        dist_str = f"{dist} mi" if dist is not None else "—"
        radar_svg = build_radar_svg(job.get("radar", {axis: 0 for axis in RADAR_AXES}))
        rows_html += f"""
        <tr>
          <td class="mono">{i:02d}</td>
          <td><strong>{job.get('title')}</strong><div class="muted">{job.get('company')}</div></td>
          <td>{job.get('location')}<div class="muted mono">{dist_str}</div></td>
          <td class="mono">{job.get('pay')}</td>
          <td><span class="score" style="background:{color}">{score}</span></td>
          <td class="muted">{job.get('reason', '')}</td>
          <td>{radar_svg}</td>
          <td><a class="apply" href="{job.get('url')}" target="_blank">Open →</a></td>
        </tr>"""

    company_links_html = ""
    for company in PREFERRED_COMPANIES:
        link = COMPANY_CAREER_LINKS.get(company, "")
        company_links_html += f'<li><a href="{link}" target="_blank">{company}</a></li>'

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Job Search — {display_time}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root {{
    --bg: #0b0f14;
    --surface: #12171f;
    --line: #212832;
    --text: #e9edf3;
    --muted: #7d8894;
    --signal: #2dd4bf;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: 'Space Grotesk', -apple-system, sans-serif;
    background: var(--bg); color: var(--text);
    margin: 0; padding: 32px; max-width: 1080px; margin-inline: auto;
  }}
  .mono {{ font-family: 'IBM Plex Mono', monospace; }}
  h1 {{ font-size: 22px; font-weight: 700; margin: 0 0 4px 0; letter-spacing: -0.01em; }}
  .subtitle {{ color: var(--muted); font-size: 13px; margin-bottom: 28px; font-family: 'IBM Plex Mono', monospace; }}
  .subtitle a {{ color: var(--signal); text-decoration: none; }}

  #map {{ height: 480px; border-radius: 16px; border: 1px solid var(--line); margin-bottom: 32px; background: var(--surface); }}
  .leaflet-popup-content-wrapper {{ background: var(--surface); color: var(--text); border-radius: 8px; }}
  .leaflet-popup-tip {{ background: var(--surface); }}
  .leaflet-popup-content a {{ color: var(--signal); }}
  .you-label {{ background: var(--signal); color: #05100c; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 700; font-family: 'IBM Plex Mono', monospace; white-space: nowrap; }}

  table {{ width: 100%; border-collapse: collapse; margin-bottom: 32px; }}
  th {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--line); color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; font-family: 'IBM Plex Mono', monospace; font-weight: 500; }}
  td {{ padding: 12px 8px; border-bottom: 1px solid var(--line); font-size: 13px; vertical-align: top; }}
  .muted {{ color: var(--muted); font-size: 12px; }}
  .score {{ color: #05100c; padding: 3px 8px; border-radius: 4px; font-weight: 700; font-size: 12px; font-family: 'IBM Plex Mono', monospace; }}
  .apply {{ color: var(--signal); text-decoration: none; font-family: 'IBM Plex Mono', monospace; font-size: 12px; }}
  .apply:hover {{ text-decoration: underline; }}

  h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin-top: 36px; border-top: 1px solid var(--line); padding-top: 20px; font-family: 'IBM Plex Mono', monospace; font-weight: 500; }}
  .company-list {{ columns: 3; list-style: none; padding: 0; }}
  .company-list li {{ margin-bottom: 8px; }}
  .company-list a {{ color: var(--text); text-decoration: none; font-size: 13px; }}
  .company-list a:hover {{ color: var(--signal); }}
</style>
</head>
<body>
  <h1>Job Search</h1>
  <div class="subtitle">{display_time} · within {RADIUS_MILES}mi of you · {len(jobs)} matches · <a href="index.html">view past runs</a></div>

  <div id="map"></div>

  <table>
    <tr><th>#</th><th>Role</th><th>Location</th><th>Pay</th><th>Score</th><th>Why</th><th>Fit</th><th></th></tr>
    {rows_html}
  </table>

  <h2>Career pages worth checking directly</h2>
  <ul class="company-list">
    {company_links_html}
  </ul>

<script>
  const jobs = {map_markers_json};
  const hasCenter = {str(has_center).lower()};
  const centerLat = {center_lat if has_center else 'null'};
  const centerLon = {center_lon if has_center else 'null'};
  const radiusMiles = {RADIUS_MILES};

  const startView = hasCenter ? [centerLat, centerLon] : (jobs.length ? [jobs[0].lat, jobs[0].lon] : [35.78, -78.64]);
  const map = L.map('map', {{ scrollWheelZoom: true }}).setView(startView, 9);

  L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
    attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
    maxZoom: 19
  }}).addTo(map);

  const bounds = [];

  if (hasCenter) {{
    const milesToMeters = 1609.34;
    [0.25, 0.5, 0.75, 1].forEach(function(frac) {{
      L.circle([centerLat, centerLon], {{
        radius: radiusMiles * frac * milesToMeters,
        color: '#2dd4bf', weight: 1, fillOpacity: 0.02, opacity: 0.35
      }}).addTo(map);
    }});
    L.circleMarker([centerLat, centerLon], {{
      radius: 7, color: '#0b0f14', weight: 2, fillColor: '#2dd4bf', fillOpacity: 1
    }}).addTo(map).bindTooltip('YOU', {{permanent: true, direction: 'top', className: 'you-label'}});
    bounds.push([centerLat, centerLon]);
  }}

  jobs.forEach(function(job) {{
    const marker = L.circleMarker([job.lat, job.lon], {{
      radius: 7, color: '#0b0f14', weight: 1.5, fillColor: job.color, fillOpacity: 0.95
    }}).addTo(map);
    const distText = job.distance !== null ? job.distance + ' mi away' : '';
    marker.bindPopup(
      '<strong>' + job.title + '</strong><br>' +
      job.company + ' · ' + distText + '<br>' +
      'Score: ' + job.score + '/10<br>' +
      '<a href="' + job.url + '" target="_blank">Open listing →</a>'
    );
    bounds.push([job.lat, job.lon]);
  }});

  if (bounds.length > 1) {{
    map.fitBounds(bounds, {{ padding: [30, 30] }});
  }}
</script>
</body>
</html>"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Visual report saved to: {html_path}")
    update_index_page(reports_dir, filename, display_time, len(jobs))
    return html_path


def update_index_page(reports_dir, new_filename, display_time, job_count):
    index_path = os.path.join(reports_dir, "index.html")
    entries = []

    # Load existing entries if index already exists
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                content = f.read()
            start = content.find("<!--ENTRIES:")
            end = content.find(":ENTRIES-->")
            if start != -1 and end != -1:
                entries = json.loads(content[start + len("<!--ENTRIES:"):end])
        except Exception:
            entries = []

    entries.insert(0, {"file": new_filename, "label": display_time, "count": job_count})
    entries = entries[:50]  # keep history bounded

    rows = ""
    for i, e in enumerate(entries):
        badge = ' <span class="latest">latest</span>' if i == 0 else ""
        rows += f'<li><a href="{e["file"]}">{e["label"]}</a>{badge} <span class="muted">— {e["count"]} matches</span></li>'

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Job Search — Run History</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{ --bg: #0b0f14; --surface: #12171f; --line: #212832; --text: #e9edf3; --muted: #7d8894; --signal: #2dd4bf; }}
  body {{ font-family: 'Space Grotesk', sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 32px; max-width: 640px; margin-inline: auto; }}
  h1 {{ font-size: 20px; margin-bottom: 20px; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ padding: 12px 0; border-bottom: 1px solid var(--line); font-family: 'IBM Plex Mono', monospace; font-size: 13px; }}
  a {{ color: var(--text); text-decoration: none; }}
  a:hover {{ color: var(--signal); }}
  .muted {{ color: var(--muted); }}
  .latest {{ background: var(--signal); color: #05100c; font-size: 10px; padding: 2px 6px; border-radius: 4px; font-weight: 700; }}
</style>
</head>
<body>
  <h1>Run History</h1>
  <ul>
    {rows}
    <!--ENTRIES:{json.dumps(entries)}:ENTRIES-->
  </ul>
</body>
</html>"""

    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)


def write_results(jobs):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(f"Job Search Results - {timestamp}\n")
        f.write("=" * 60 + "\n\n")

        f.write("COMPANY CAREER PAGES TO CHECK MANUALLY\n")
        f.write("-" * 60 + "\n")
        for company in PREFERRED_COMPANIES:
            link = COMPANY_CAREER_LINKS.get(company, "")
            f.write(f"  {company}: {link}\n")
        f.write("\n")

        f.write("RANKED JOB MATCHES (from Adzuna)\n")
        f.write("-" * 60 + "\n\n")
        for i, job in enumerate(jobs[:30], 1):
            dist = job.get("distance_miles")
            dist_str = f"{dist} mi away" if dist is not None else "distance unknown"
            f.write(f"{i}. {job.get('title')} at {job.get('company')}\n")
            f.write(f"   Location: {job.get('location')} ({dist_str})\n")
            f.write(f"   Pay: {job.get('pay')}\n")
            f.write(f"   Match Score: {job.get('match_score')}/10 - {job.get('reason')}\n")
            f.write(f"   URL: {job.get('url')}\n")
            f.write("\n")

    print(f"\nResults saved to: {OUTPUT_FILE}")


def prompt_open_tabs(jobs, timeout=60):
    """Ask whether to open top results as browser tabs, with a timeout.
    If the user doesn't respond in time, defaults to NOT opening (the
    links stay in the report - nothing is lost either way)."""
    import webbrowser

    top_n = min(10, len(jobs))
    print(f"\nOpen the top {top_n} results as browser tabs? [y/N]")
    print(f"(Defaults to No if no response in {timeout}s - your results are already saved either way)")

    answer = None
    try:
        import msvcrt
        import time as time_module
        start = time_module.time()
        buffer = ""
        while time_module.time() - start < timeout:
            if msvcrt.kbhit():
                ch = msvcrt.getwche()
                if ch in ("\r", "\n"):
                    answer = buffer.strip().lower()
                    break
                buffer += ch
            time_module.sleep(0.05)
    except ImportError:
        # Not on Windows / msvcrt unavailable - fall back to a plain prompt with no timeout
        answer = input().strip().lower()

    if answer == "y":
        for job in jobs[:top_n]:
            if job.get("url"):
                webbrowser.open_new_tab(job["url"])
        print(f"Opened {top_n} tabs in your default browser.")
    else:
        print("Skipped - all links are saved in your report, open anytime.")


def play_completion_sound():
    """Plays a fanfare-style sound when the run finishes. Uses Windows'
    built-in tada.wav (the classic "task complete" chime) if available,
    falls back to a simple ascending beep sequence, and silently does
    nothing if neither works (e.g. not on Windows)."""
    try:
        import winsound
        winsound.PlaySound(r"C:\Windows\Media\tada.wav", winsound.SND_FILENAME)
        return
    except Exception:
        pass
    try:
        import winsound
        for freq in [523, 659, 784, 1047]:  # ascending C-E-G-C, fanfare-ish
            winsound.Beep(freq, 150)
    except Exception:
        pass  # not on Windows / winsound unavailable - skip silently


def format_duration(seconds):
    minutes, secs = divmod(int(seconds), 60)
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def run():
    run_start = time.time()
    start_clock = datetime.now().strftime("%I:%M:%S %p")

    check_config()
    print("Job Search Agent starting (read-only mode)")
    print(f"Started at {start_clock}")
    print(f"Searching within {RADIUS_MILES} miles of {LOCATION}, {STATE}\n")

    search_start = time.time()
    jobs, center_lat, center_lon = collect_all_jobs()
    search_duration = time.time() - search_start
    print(f"\nCollected {len(jobs)} unique listings from Adzuna in {format_duration(search_duration)}.")
    print(f"Ranking with {RANKING_BACKEND}...")

    ranking_start = time.time()
    if RANKING_BACKEND == "ollama":
        ranked = rank_jobs_with_ollama(jobs)
    else:
        ranked = rank_jobs_with_gemini(jobs)
    ranking_duration = time.time() - ranking_start

    good_matches = [j for j in ranked if j.get("match_score", 0) >= 6]
    final_jobs = good_matches if good_matches else ranked

    write_start = time.time()
    write_results(final_jobs)
    run_label = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = write_html_report(final_jobs, center_lat, center_lon, run_label)
    write_duration = time.time() - write_start

    print(f"\nTop 5 matches:")
    for job in final_jobs[:5]:
        print(f"  {job.get('title')} at {job.get('company')} - {job.get('match_score')}/10")
        print(f"    {job.get('url')}")

    print(f"\nOpen your visual report: {report_path}")
    import webbrowser
    webbrowser.open(f"file:///{os.path.abspath(report_path).replace(os.sep, '/')}")

    end_clock = datetime.now().strftime("%I:%M:%S %p")
    total_duration = time.time() - run_start
    print(f"\n{'='*40}")
    print(f"TIMING")
    print(f"{'='*40}")
    print(f"  Started:   {start_clock}")
    print(f"  Finished:  {end_clock}")
    print(f"  Adzuna search:  {format_duration(search_duration)}")
    print(f"  Ranking ({RANKING_BACKEND}):  {format_duration(ranking_duration)}")
    print(f"  Writing report: {format_duration(write_duration)}")
    print(f"  TOTAL:     {format_duration(total_duration)}")
    print(f"{'='*40}")

    play_completion_sound()

    prompt_open_tabs(final_jobs)


if __name__ == "__main__":
    run()

# ----------------------------------------------------------------------
# ENV VAR SETUP (PowerShell) - run these once, then restart your terminal
# ----------------------------------------------------------------------
#
# [System.Environment]::SetEnvironmentVariable('ADZUNA_APP_ID', 'your-app-id', 'User')
# [System.Environment]::SetEnvironmentVariable('ADZUNA_APP_KEY', 'your-app-key', 'User')
# [System.Environment]::SetEnvironmentVariable('GEMINI_API_KEY', 'your-gemini-key', 'User')
# [System.Environment]::SetEnvironmentVariable('JOB_SEARCH_CENTER_ADDRESS', 'your-address-here', 'User')
# [System.Environment]::SetEnvironmentVariable('JOB_SEARCH_OUTPUT', 'C:\Users\yourname\Documents\job_search_results.txt', 'User')
#
# Also create candidate_profile.txt in the same folder as this script
# with your real background - see load_candidate_profile() for the format.
# Add both JOB_SEARCH_CENTER_ADDRESS's value and candidate_profile.txt
# to a .gitignore before pushing this repo publicly.
