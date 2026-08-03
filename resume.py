"""
Resume Parser using pdfplumber + Google Gemini AI
---------------------------------------------------
Extracts raw text from a resume PDF with pdfplumber, then sends that text
to Gemini with a prompt instructing it to return structured resume data
as JSON.

Install dependencies:
    pip install pdfplumber google-genai

Set your API key (get one from https://aistudio.google.com/apikey):
    export GEMINI_API_KEY="your_api_key_here"

Run:
    python resume_parser.py path/to/resume.pdf
"""

import os
import sys
import json
import pdfplumber
from google import genai


# ---------------------------------------------------------------------------
# 1. Extract raw text from the PDF
# ---------------------------------------------------------------------------
def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract all text from a PDF file using pdfplumber."""
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"No such file: {pdf_path}")

    text_chunks = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_chunks.append(page_text)

    full_text = "\n".join(text_chunks).strip()
    if not full_text:
        raise ValueError(
            "No extractable text found in PDF. "
            "It may be a scanned/image-based resume that needs OCR."
        )
    return full_text


# ---------------------------------------------------------------------------
# 2. Build the extraction prompt
# ---------------------------------------------------------------------------
EXTRACTION_PROMPT = """You are a resume-parsing engine. Read the resume text below and
extract the information into the exact JSON schema provided. Follow these rules:

- Return ONLY valid JSON. No markdown fences, no commentary, no extra text.
- If a field is not present in the resume, use null (or an empty list/array where appropriate).
- Dates should be kept as written in the resume (e.g. "Jan 2022 - Present").
- Do not invent or hallucinate information that isn't in the text.
- For "suggested_job_titles": based on the candidate's skills, tech stack, and work
  experience, suggest 3 to 6 job titles this person is well-suited for
  (e.g. "Software Developer", "React JS Developer", "Node JS Developer",
  "MERN Stack Developer", "Frontend Developer"). Base these only on evidence in
  the resume (skills/tools/frameworks/roles actually mentioned), ordered from
  most to least relevant. Do not include titles unrelated to their skill set.

JSON schema to fill:
{
  "name": string or null,
  "email": string or null,
  "phone": string or null,
  "location": string or null,
  "linkedin": string or null,
  "github": string or null,
  "portfolio": string or null,
  "summary": string or null,
  "skills": [string],
  "education": [
    {
      "degree": string or null,
      "institution": string or null,
      "location": string or null,
      "start_date": string or null,
      "end_date": string or null,
      "gpa": string or null
    }
  ],
  "experience": [
    {
      "job_title": string or null,
      "company": string or null,
      "location": string or null,
      "start_date": string or null,
      "end_date": string or null,
      "responsibilities": [string]
    }
  ],
  "projects": [
    {
      "name": string or null,
      "description": string or null,
      "technologies": [string]
    }
  ],
  "certifications": [string],
  "languages": [string],
  "achievements": [string],
  "suggested_job_titles": [string]
}

Resume text:
\"\"\"
{resume_text}
\"\"\"
"""


# ---------------------------------------------------------------------------
# 3. Call Gemini and parse the JSON response
# ---------------------------------------------------------------------------
def parse_resume_with_gemini(resume_text: str, api_key: str = None, model: str = "gemini-2.5-flash") -> dict:
    """Send resume text to Gemini and return the parsed JSON as a dict."""
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GEMINI_API_KEY not set. Export it or pass api_key explicitly."
        )

    client = genai.Client(api_key=api_key)

    prompt = EXTRACTION_PROMPT.replace("{resume_text}", resume_text)

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={
            "response_mime_type": "application/json",
        },
    )

    raw_output = response.text.strip()

    try:
        return json.loads(raw_output)
    except json.JSONDecodeError:
        # Fallback: strip accidental markdown fences and retry
        cleaned = raw_output.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        return json.loads(cleaned)


# ---------------------------------------------------------------------------
# 4. Main entry point
# ---------------------------------------------------------------------------
def parse_resume(pdf_path: str, api_key: str = None) -> dict:
    """Full pipeline: PDF -> text -> Gemini -> structured JSON."""
    resume_text = extract_text_from_pdf(pdf_path)
    result = parse_resume_with_gemini(resume_text, api_key=api_key)
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python resume_parser.py <path_to_resume.pdf>")
        sys.exit(1)

    pdf_file = sys.argv[1]
    parsed_data = parse_resume(pdf_file)

    print(json.dumps(parsed_data, indent=2, ensure_ascii=False))

    # Optionally save to a JSON file next to the PDF
    output_path = os.path.splitext(pdf_file)[0] + "_parsed.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(parsed_data, f, indent=2, ensure_ascii=False)
    print(f"\nSaved parsed output to: {output_path}")