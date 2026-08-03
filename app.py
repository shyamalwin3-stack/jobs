from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, jsonify, send_file, abort
from scrapers import (scrape_linkedin, scrape_glassdoor, scrape_indeed,
                      scrape_hirist, scrape_naukri, scrape_foundit,
                      scrape_apna, scrape_shine)
from scrapers.glassdoor import GLASSDOOR_CITIES
from scrapers.indeed import INDEED_CITIES

try:
    from scrapers.hcafe import scrape_hcafe, HCAFE_CITIES
    HCAFE_ENABLED = True
except ModuleNotFoundError:
    scrape_hcafe = None
    HCAFE_CITIES = {}
    HCAFE_ENABLED = False
from scrapers.hirist import HIRIST_CATEGORIES, HIRIST_CITIES, HIRIST_EXPERIENCE
from scrapers.naukri import NAUKRI_CITIES
from scrapers.foundit import FOUNDIT_CITIES
from scrapers.apna import APNA_CITIES
from scrapers.shine import SHINE_CITIES, SHINE_EXPERIENCE
from resume import parse_resume
from google import genai
import pandas as pd
import os
import json as _json
import tempfile
from dateutil import parser as date_parser
from datetime import datetime
import re

app = Flask(__name__)


def _parse_resume_date(text):
    """Best-effort parse of free-text resume dates like 'Aug 2024', 'Present'."""
    if not text:
        return None
    text = str(text).strip()
    if text.lower() in ("present", "current", "ongoing", "now", "till date", "to date"):
        return datetime.today()
    try:
        # default to first day of year when only year provided
        return date_parser.parse(text, default=datetime(datetime.today().year, 1, 1))
    except Exception:
        return None


def estimate_total_experience_months(resume):
    """Sum durations across all resume 'experience' entries. Returns float months or None."""
    total_days = 0
    parsed_any = False
    for exp in (resume.get("experience") or []):
        try:
            start = _parse_resume_date(exp.get("start_date"))
            end = _parse_resume_date(exp.get("end_date"))
        except Exception:
            start = None
            end = None
        if start and end and end > start:
            total_days += (end - start).days
            parsed_any = True
    if not parsed_any:
        return None
    # average month length
    return round(total_days / 30.44, 1)


def _infer_job_required_years(job):
    """Try to infer a job's required years from title/description heuristically."""
    text = " ".join([str(job.get(k, "")) for k in ("Job Title", "Description")])
    text = text.lower()
    # explicit patterns like '5+ years', '8-10 years', 'minimum 3 years'
    m = re.search(r"(\d+)\s*\+\s*years", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*-\s*(\d+)\s*years", text)
    if m:
        return int(m.group(1))
    m = re.search(r"minimum\s*(\d+)\s*years", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*years", text)
    if m:
        return int(m.group(1))
    # title-based heuristics
    title = (job.get("Job Title") or "").lower()
    if any(t in title for t in ("principal", "staff", "architect", "director")):
        return 8
    if any(t in title for t in ("senior", "lead", "manager")):
        return 5
    if any(t in title for t in ("junior", "associate", "trainee", "intern")):
        return 1
    return None


def heuristic_score_jobs(resume, jobs):
    """Produce heuristic scores when the remote model is unavailable.

    Returns a list of dicts: {index, score, reason} matching job indices.
    """
    candidate_months = estimate_total_experience_months(resume)
    candidate_years = round(candidate_months / 12, 1) if candidate_months is not None else None
    resume_skills = set([s.lower() for s in (resume.get("skills") or []) if isinstance(s, str)])
    results = []
    for i, job in enumerate(jobs):
        job_skills = set([s.strip().lower() for s in (job.get("Skills") or "").split(",") if s.strip()])
        if job_skills:
            overlap = len(resume_skills & job_skills) / len(job_skills)
        else:
            overlap = 0.5 if resume_skills else 0
        base_score = int(round(overlap * 100))

        required = _infer_job_required_years(job)
        reason = []
        score = base_score
        if required is not None and candidate_years is not None:
            # substantial gap
            if required >= candidate_years + 3:
                score = min(score, 35)
                reason.append(f"Role appears to require {required}+ years; candidate has ~{candidate_years} years")
            elif required > candidate_years + 1:
                score = min(score, 65)
                reason.append(f"Role appears to require {required} years; candidate has ~{candidate_years} years")
        else:
            reason.append("Scored by heuristic skill-overlap and experience estimate")

        score = max(0, min(95, score))
        results.append({"index": i, "score": score, "reason": "; ".join(reason)})
    return results

# Per-portal cache of the most recent search results
latest = {"linkedin": [], "glassdoor": [], "indeed": [],
          "hirist": [], "naukri": [], "foundit": [],
          "apna": [], "shine": [], "hcafe": []}


@app.route("/")
def home():
    return render_template("landing.html")


@app.route("/favicon.ico")
def favicon():
    return send_file(
        os.path.join(app.static_folder, "favicon.png"),
        mimetype="image/png",
    )


@app.route("/upload-resume", methods=["POST"])
def upload_resume():
    file = request.files.get("resume")
    if not file or file.filename == "":
        return jsonify({"error": "No resume file provided"}), 400
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF resumes are supported"}), 400

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        parsed = parse_resume(tmp_path)
    except Exception as e:
        return jsonify({"error": f"Failed to parse resume: {e}"}), 500
    finally:
        os.remove(tmp_path)

    # Attach computed experience so front-end pages can autofill filters
    try:
        months = estimate_total_experience_months(parsed)
    except Exception:
        months = None
    parsed["computed_experience_months"] = months
    parsed["computed_experience_years"] = (
        round(months / 12, 1) if months is not None else None
    )

    return jsonify(parsed)


@app.route("/linkedin")
def linkedin_page():
    return render_template("linkedin.html")


@app.route("/glassdoor")
def glassdoor_page():
    return render_template("glassdoor.html", cities=list(GLASSDOOR_CITIES.keys()))


@app.route("/indeed")
def indeed_page():
    return render_template("indeed.html", cities=list(INDEED_CITIES.keys()))


@app.route("/hirist")
def hirist_page():
    return render_template(
        "hirist.html",
        categories=HIRIST_CATEGORIES,
        cities=list(HIRIST_CITIES.keys()),
        experiences=list(HIRIST_EXPERIENCE.keys()),
    )


@app.route("/naukri")
def naukri_page():
    return render_template("naukri.html", cities=list(NAUKRI_CITIES.keys()))


@app.route("/foundit")
def foundit_page():
    return render_template("foundit.html", cities=list(FOUNDIT_CITIES.keys()))


@app.route("/apna")
def apna_page():
    return render_template("apna.html", cities=list(APNA_CITIES.keys()))


@app.route("/hcafe")
def hcafe_page():
    return render_template("hcafe.html", cities=list(HCAFE_CITIES.keys()))


@app.route("/shine")
def shine_page():
    return render_template(
        "shine.html",
        cities=list(SHINE_CITIES.keys()),
        experiences=SHINE_EXPERIENCE,
    )


@app.route("/search/hcafe", methods=["POST"])
def search_hcafe():
    try:
        role = request.form.get("role", "").strip()
        city = request.form.get("city", "").strip()
        posting = 1

        if not HCAFE_ENABLED:
            return jsonify({"error": "HiringCafe support requires the patchright dependency. Install patchright and restart the server."}), 500
        if not role:
            return jsonify({"error": "Please enter a job role"}), 400
        if not city:
            return jsonify({"error": "Please select a location"}), 400
        if city not in HCAFE_CITIES:
            return jsonify({"error": "Unknown city"}), 400

        jobs = scrape_hcafe(
            role=role,
            city=city,
            posting_days=posting,
            limit=500,
        )
        latest["hcafe"] = jobs
        return jsonify({"jobs": jobs, "count": len(jobs)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/search/linkedin", methods=["POST"])
def search_linkedin():
    try:
        role        = request.form.get("role", "").strip()
        time_filter = 86400
        apply_mode  = request.form.get("apply_mode", "include_easy").strip().lower()
        locations   = request.form.getlist("locations")

        if not role:
            return jsonify({"error": "Please enter a job role"}), 400
        if not locations:
            return jsonify({"error": "Select at least one location"}), 400
        if apply_mode not in {"include_easy", "only_easy", "only_external"}:
            return jsonify({"error": "Invalid apply filter selected"}), 400

        jobs = scrape_linkedin(
            role=role,
            time_filter=time_filter,
            limit=500,
            locations=locations,
            apply_mode=apply_mode,
        )
        latest["linkedin"] = jobs
        return jsonify({"jobs": jobs, "count": len(jobs)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/search/glassdoor", methods=["POST"])
def search_glassdoor():
    try:
        role        = request.form.get("role", "").strip()
        from_age    = 1
        apply_mode  = request.form.get("apply_mode", "include_easy").strip().lower()
        locations   = request.form.getlist("locations")

        if not role:
            return jsonify({"error": "Please enter a job role"}), 400
        if not locations:
            return jsonify({"error": "Select at least one location"}), 400
        if apply_mode not in {"include_easy", "only_easy", "only_external"}:
            return jsonify({"error": "Invalid apply filter selected"}), 400

        jobs = scrape_glassdoor(
            role=role,
            from_age_days=from_age,
            limit=500,
            locations=locations,
            apply_mode=apply_mode,
        )
        latest["glassdoor"] = jobs
        return jsonify({"jobs": jobs, "count": len(jobs)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/search/indeed", methods=["POST"])
def search_indeed():
    try:
        role        = request.form.get("role", "").strip()
        fromage     = 1
        apply_mode  = request.form.get("apply_mode", "include_easy").strip().lower()
        locations   = request.form.getlist("locations")

        if not role:
            return jsonify({"error": "Please enter a job role"}), 400
        if not locations:
            return jsonify({"error": "Select at least one location"}), 400
        if apply_mode not in {"include_easy", "only_easy", "only_external"}:
            return jsonify({"error": "Invalid apply filter selected"}), 400

        jobs = scrape_indeed(
            role=role,
            fromage_days=fromage,
            limit=500,
            locations=locations,
            apply_mode=apply_mode,
        )
        latest["indeed"] = jobs
        return jsonify({"jobs": jobs, "count": len(jobs)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/search/hirist", methods=["POST"])
def search_hirist():
    try:
        role        = request.form.get("role", "").strip()  # role here = category slug
        city        = request.form.get("city", "").strip()
        exp_key     = request.form.get("experience", "any").strip()
        posting     = 1

        if not role:
            return jsonify({"error": "Please select a job category"}), 400
        if not city:
            return jsonify({"error": "Please select a location"}), 400
        if role not in HIRIST_CATEGORIES:
            return jsonify({"error": "Unknown category"}), 400
        if city not in HIRIST_CITIES:
            return jsonify({"error": "Unknown city"}), 400
        if exp_key not in HIRIST_EXPERIENCE:
            return jsonify({"error": "Unknown experience range"}), 400

        jobs = scrape_hirist(
            category=role,
            city=city,
            exp_key=exp_key,
            posting_days=posting,
            limit=500,
        )
        latest["hirist"] = jobs
        return jsonify({"jobs": jobs, "count": len(jobs)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/search/naukri", methods=["POST"])
def search_naukri():
    try:
        role        = request.form.get("role", "").strip()
        city        = request.form.get("city", "").strip()
        job_age     = 1
        exp_raw     = (request.form.get("experience") or "").strip()
        experience  = int(exp_raw) if exp_raw else None
        # NOTE: Posted age and result limit are intentionally hardcoded to last 24 hours and a high limit
        job_age = 1

        if not role:
            return jsonify({"error": "Please enter a job role"}), 400
        if not city:
            return jsonify({"error": "Please select a location"}), 400
        if city not in NAUKRI_CITIES:
            return jsonify({"error": "Unknown city"}), 400

        jobs = scrape_naukri(
            role=role, city=city, job_age_days=job_age,
            limit=500, experience=experience,
        )
        latest["naukri"] = jobs
        return jsonify({"jobs": jobs, "count": len(jobs)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/search/foundit", methods=["POST"])
def search_foundit():
    try:
        role        = request.form.get("role", "").strip()
        city        = request.form.get("city", "").strip()
        freshness   = 1
        exp_raw     = (request.form.get("experience") or "").strip()
        experience  = int(exp_raw) if exp_raw else None

        if not role:
            return jsonify({"error": "Please enter a job role"}), 400
        if not city:
            return jsonify({"error": "Please select a location"}), 400
        if city not in FOUNDIT_CITIES:
            return jsonify({"error": "Unknown city"}), 400

        jobs = scrape_foundit(
            role=role, city=city, job_freshness_days=freshness,
            limit=500, experience=experience,
        )
        latest["foundit"] = jobs
        return jsonify({"jobs": jobs, "count": len(jobs)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/search/apna", methods=["POST"])
def search_apna():
    try:
        role        = request.form.get("role", "").strip()
        city        = request.form.get("city", "").strip()
        posted_in   = 1
        min_raw     = (request.form.get("min_experience") or "").strip()
        max_raw     = (request.form.get("max_experience") or "").strip()
        min_exp     = int(min_raw) if min_raw else None
        max_exp     = int(max_raw) if max_raw else None

        if not role:
            return jsonify({"error": "Please enter a job role"}), 400
        if not city:
            return jsonify({"error": "Please select a location"}), 400
        if city not in APNA_CITIES:
            return jsonify({"error": "Unknown city"}), 400

        jobs = scrape_apna(
            role=role, city=city, posted_in_days=posted_in,
            limit=500, min_experience=min_exp, max_experience=max_exp,
        )
        latest["apna"] = jobs
        return jsonify({"jobs": jobs, "count": len(jobs)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/search/shine", methods=["POST"])
def search_shine():
    try:
        role         = request.form.get("role", "").strip()
        city         = request.form.get("city", "").strip()
        posting_days = 1
        fexp         = request.form.getlist("fexp")

        if not role:
            return jsonify({"error": "Please enter a job role"}), 400
        if not city:
            return jsonify({"error": "Please select a location"}), 400
        if city not in SHINE_CITIES:
            return jsonify({"error": "Unknown city"}), 400
        for v in fexp:
            if v not in SHINE_EXPERIENCE:
                return jsonify({"error": f"Unknown experience band: {v}"}), 400

        jobs = scrape_shine(
            role=role, city=city, fexp=fexp,
            posting_days=posting_days, limit=500,
        )
        latest["shine"] = jobs
        return jsonify({"jobs": jobs, "count": len(jobs)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/score-jobs", methods=["POST"])
def score_jobs():
    payload = request.get_json(force=True)
    resume = payload.get("resume")
    jobs = payload.get("jobs", [])

    if not resume or not jobs:
        return jsonify({"error": "resume and jobs are required"}), 400

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not set on server"}), 500

    client = genai.Client(api_key=api_key)

    slim_jobs = [
        {
            "index": i,
            "title": j.get("Job Title"),
            "company": j.get("Company"),
            "skills": j.get("Skills"),
            # pass full description (no truncation) so experience signals aren't lost
            "description": j.get("Description") or "",
        }
        for i, j in enumerate(jobs)
    ]

    candidate_months = estimate_total_experience_months(resume)
    if candidate_months is not None:
        years = round(candidate_months / 12, 1)
        experience_line = (
            f"Candidate's total professional experience (computed precisely from their resume "
            f"dates, do NOT recalculate this yourself): approximately {candidate_months} months "
            f"(~{years} years). Treat this number as ground truth."
        )
    else:
        experience_line = (
            "Candidate's total professional experience could not be computed automatically — "
            "estimate conservatively from their listed roles/projects, and if experience level "
            "is unclear, do not assume a high experience level."
        )

    prompt = f"""You are a strict job-matching engine. Score how well each job matches the
candidate from 0 to 100 (100 = ideal match). Weigh BOTH of these — do not let one dominate:
  (a) skill / tech-stack overlap between the candidate and the job, and
  (b) whether the candidate's experience level actually fits what the job requires.

{experience_line}

Experience-level rules (apply these strictly, even when skill overlap is high):
- For each job, infer its required experience level from its title and description. Look for
  explicit signals first ("5+ years", "8-10 years", "Minimum 3 years", "Entry level", "Fresher",
  "0-1 years"). If there's no explicit number, infer a reasonable level from the title alone:
  titles containing "Senior", "Lead", "Staff", "Principal", "Architect", "Manager", "Director"
  typically require 5+ years (Staff/Principal/Architect/Director often 8+); plain titles like
  "Software Engineer", "Developer", "Analyst" typically need 0-3 years; "Junior"/"Associate"/
  "Trainee"/"Intern" need 0-2 years.
- If the job's required experience is clearly and substantially higher than the candidate's
  actual experience (e.g. job wants 5+ years and candidate has under 2 years), CAP the score at
  35 regardless of how well the skills overlap, and say so explicitly in the reason (e.g.
  "Strong tech match but role requires 5+ years; candidate has ~10 months").
- If the job's required experience is moderately higher (e.g. wants 2-3 years, candidate has
  ~1 year), reduce the score moderately (cap around 55-65) rather than rejecting outright.
- If the candidate is significantly more experienced than an entry-level role requires, that is
  NOT a hard penalty — reduce moderately at most (cap around 70), noting overqualification.
- If experience levels are reasonably aligned (or the job has no seniority signal at all and
  reads as generalist/entry-friendly), score primarily on skill/tech overlap as before.

Return ONLY a JSON array, no markdown, no commentary, in this exact shape, one entry per job,
same order as input, matched by "index":

[{{"index": 0, "score": 82, "reason": "short one-sentence reason mentioning experience fit if relevant"}}, ...]

Candidate resume JSON:
{_json.dumps(resume)}

Jobs:
{_json.dumps(slim_jobs)}
"""

    score_map = {}
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
        scores = _json.loads(response.text.strip())
        score_map = {s["index"]: s for s in scores if "index" in s}
    except Exception as e:
        # Log and fallback to heuristic scoring so UI still shows results when Gemini is unavailable
        print(f"[score_jobs] Gemini scoring failed: {e}")
        fallback = heuristic_score_jobs(resume, jobs)
        score_map = {s["index"]: s for s in fallback}

    for i, job in enumerate(jobs):
        match = score_map.get(i, {})
        job["MatchScore"] = match.get("score")
        job["MatchReason"] = match.get("reason")

    return jsonify({"jobs": jobs})


@app.route("/download-combined", methods=["POST"])
def download_combined():
    payload = request.get_json(force=True)
    jobs = payload.get("jobs", [])
    if not jobs:
        return "No data.", 400

    df = pd.DataFrame(jobs)
    df.rename(columns={"Company": "Company Name"}, inplace=True)
    column_order = [
        "Suggested Role", "Link", "Company Name", "Job Title", "Location",
        "Posted", "Experience", "Workplace", "Seniority", "Rating",
        "Salary", "Skills", "Industry", "Description", "MatchScore",
        "MatchReason"
    ]
    df = df[[c for c in column_order if c in df.columns]]

    path = os.path.join(os.path.dirname(__file__), "jobs_combined.xlsx")
    df.to_excel(path, index=False)
    return send_file(path, as_attachment=True)


@app.route("/download/<source>")
def download(source):
    source = source.lower()
    if source not in latest:
        abort(404)
    data = latest[source]
    if not data:
        return "No data. Run a search first.", 400

    df = pd.DataFrame(data)
    df["Source"] = {"linkedin": "LinkedIn", "glassdoor": "Glassdoor",
                    "indeed": "Indeed", "hirist": "Hirist",
                    "naukri": "Naukri", "foundit": "Foundit",
                    "apna": "Apna", "shine": "Shine"}.get(source, source.capitalize())

    df.rename(columns={"Company": "Company Name"}, inplace=True)
    # Columns vary by portal; include any that exist.
    column_order = ["Link", "Company Name", "Job Title", "Location", "Source",
                    "Posted", "Experience", "Workplace", "Seniority", "Rating",
                    "Salary", "Skills", "Industry", "Description", "Source ATS",
                    "Easy Apply", "Apply Type"]
    df = df[[c for c in column_order if c in df.columns]]

    path = os.path.join(os.path.dirname(__file__), f"jobs_{source}.xlsx")
    df.to_excel(path, index=False)
    return send_file(path, as_attachment=True)


import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
