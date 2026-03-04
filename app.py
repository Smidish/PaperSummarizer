import html
import io
import json
import os
import re
import uuid

import requests
from flask import Flask, jsonify, render_template, request, send_file
from PyPDF2 import PdfReader
from cerebras.cloud.sdk import Cerebras


# Cerebras API Key (free tier)
CEREBRAS_API_KEY = ""

MAX_SUMMARY_TEXT_LENGTH = 10000
MAX_TAGS_TEXT_LENGTH = 8000

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB upload limit

ZOTERO_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "zotero_settings.json")


def extract_text_from_pdf(file_storage):
    reader = PdfReader(file_storage)
    text = []
    for page in reader.pages:
        text.append(page.extract_text() or "")
    return "\n".join(text)



# Cerebras chat completion API
def call_cerebras(prompt):
    client = Cerebras(api_key=CEREBRAS_API_KEY)
    response = client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model="llama3.1-8b",
        max_completion_tokens=1024,
        temperature=0.2,
        top_p=1,
        stream=False
    )
    try:
        return response.choices[0].message.content
    except Exception:
        return "Error: Could not parse Cerebras response."


def extract_metadata_from_text(paper_text):
    """Use the LLM to extract bibliographic metadata from paper text."""
    meta_prompt = (
        "From the following paper text, extract the bibliographic metadata. "
        "Return ONLY a JSON object with these keys: title, authors (semicolon-separated), "
        "year, doi, url. If a field cannot be determined, use an empty string.\n\n"
        + paper_text[:MAX_TAGS_TEXT_LENGTH]
    )
    raw = call_cerebras(meta_prompt)
    # Strip markdown code fences if present
    raw = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw.strip(), flags=re.IGNORECASE | re.MULTILINE)
    try:
        return json.loads(raw.strip())
    except Exception:
        return {}


@app.route("/", methods=["GET", "POST"])
def index():
    summary = None
    tags = None
    error = None
    meta = {}

    if request.method == "POST":
        pdf_file = request.files.get("pdf")
        custom_section = request.form.get("custom_section", "").strip()
        sections = request.form.getlist("sections")

        if not pdf_file or pdf_file.filename == "":
            error = "Please upload a PDF file."
        else:
            try:
                paper_text = extract_text_from_pdf(pdf_file)

                if not sections:
                    sections = [
                        "Snapshot",
                        "Key Findings",
                        "Objective",
                        "Methods",
                        "Results",
                        "Discussion",
                        "Conclusion",
                    ]

                prompt_parts = [
                    "You are summarizing an academic research paper for Zotero notes.",
                    "Paper text:\n" + paper_text[:MAX_SUMMARY_TEXT_LENGTH],
                    "Create a structured summary in Markdown using only these sections:",
                ]
                for s in sections:
                    prompt_parts.append(f"- {s}")
                if custom_section:
                    prompt_parts.append(
                        f"Also add a section titled '{custom_section}'."
                    )

                prompt = "\n\n".join(prompt_parts)
                summary = call_cerebras(prompt)

                tags_prompt = (
                    "From the following paper text, extract 5-12 concise tags/key concepts, "
                    "comma-separated only:\n\n" + paper_text[:MAX_TAGS_TEXT_LENGTH]
                )
                tags = call_cerebras(tags_prompt)

                meta = extract_metadata_from_text(paper_text)
            except Exception as exc:
                error = f"Error processing request: {exc}"

    return render_template("index.html", summary=summary, tags=tags, error=error, meta=meta)


@app.route("/export/csljson", methods=["POST"])
def export_csljson():
    title = request.form.get("title", "")
    authors = request.form.get("authors", "")
    year = request.form.get("year", "")
    doi = request.form.get("doi", "")
    paper_url = request.form.get("url", "")
    summary = request.form.get("summary", "")
    tags_str = request.form.get("tags", "")

    tags = [t.strip() for t in tags_str.split(",") if t.strip()]

    author_list = []
    for author in authors.split(";"):
        author = author.strip()
        if not author:
            continue
        parts = author.rsplit(" ", 1)
        if len(parts) == 2:
            first, last = parts
        else:
            first, last = "", parts[0]
        author_list.append({"family": last, "given": first})

    csl_item = {
        "id": str(uuid.uuid4()),
        "type": "article-journal",
        "title": title,
        "author": author_list,
        "issued": {"date-parts": [[int(year)]]} if year and year.strip().lstrip("-").isdigit() else None,
        "abstract": summary,
        "categories": tags,
    }
    if doi:
        csl_item["DOI"] = doi
    if paper_url:
        csl_item["URL"] = paper_url

    data = json.dumps([csl_item], indent=2)
    return send_file(
        io.BytesIO(data.encode("utf-8")),
        mimetype="application/json",
        as_attachment=True,
        download_name="zotero_item.json",
    )


def build_zotero_item(form):
    title = form.get("title", "")
    authors = form.get("authors", "")
    year = form.get("year", "")
    doi = form.get("doi", "")
    paper_url = form.get("url", "")
    tags_str = form.get("tags", "")

    creators = []
    for author in authors.split(";"):
        author = author.strip()
        if not author:
            continue
        parts = author.rsplit(" ", 1)
        if len(parts) == 2:
            first, last = parts
        else:
            first, last = "", parts[0]
        creators.append(
            {
                "creatorType": "author",
                "firstName": first,
                "lastName": last,
            }
        )

    tags = [{"tag": t.strip()} for t in tags_str.split(",") if t.strip()]

    return {
        "itemType": "journalArticle",
        "title": title,
        "creators": creators,
        "date": year,
        "DOI": doi,
        "url": paper_url,
        "abstractNote": "",
        "tags": tags,
    }


@app.route("/export/zotero-api", methods=["POST"])
def export_zotero_api():
    api_key = request.form.get("zotero_api_key", "").strip()
    user_or_group = request.form.get("lib_type", "user")
    lib_id = request.form.get("lib_id", "").strip()
    collection = request.form.get("collection", "").strip()

    if not api_key or not lib_id:
        return "Missing Zotero API key or library ID", 400

    if user_or_group not in ("user", "group"):
        return "Invalid library type; must be 'user' or 'group'", 400

    item = build_zotero_item(request.form)
    if collection:
        item["collections"] = [collection]

    base_url = f"https://api.zotero.org/{user_or_group}s/{lib_id}/items"
    headers = {
        "Zotero-API-Key": api_key,
        "Content-Type": "application/json",
    }

    # Step 1: POST the bibliographic item
    resp = requests.post(base_url, headers=headers, json=[item], timeout=30)
    if not resp.ok:
        return f"Zotero API error: {resp.status_code} {resp.text}", 500

    # Step 2: Extract the new item key and POST the note as a separate item
    try:
        item_key = resp.json()["success"]["0"]
    except (KeyError, ValueError):
        item_key = None

    if item_key:
        summary_md = request.form.get("summary", "")
        note_html = f"<pre>{html.escape(summary_md)}</pre>"
        note_item = {
            "itemType": "note",
            "parentItem": item_key,
            "note": note_html,
            "tags": [],
        }
        requests.post(base_url, headers=headers, json=[note_item], timeout=30)

    return render_template(
        "index.html",
        summary=request.form.get("summary"),
        tags=request.form.get("tags"),
        error=None,
        zotero_success=True,
        meta={},
    )


@app.route("/settings/zotero", methods=["GET"])
def get_zotero_settings():
    if os.path.exists(ZOTERO_SETTINGS_FILE):
        with open(ZOTERO_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    return jsonify({})


@app.route("/settings/zotero", methods=["POST"])
def save_zotero_settings():
    data = request.get_json(force=True) or {}
    settings = {
        "api_key": data.get("api_key", ""),
        "lib_type": data.get("lib_type", "user"),
        "lib_id": data.get("lib_id", ""),
        "collection": data.get("collection", ""),
    }
    with open(ZOTERO_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug_mode)
