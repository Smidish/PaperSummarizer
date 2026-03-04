import html
import io
import json
import os

import requests
from flask import Flask, render_template, request, send_file
from PyPDF2 import PdfReader
from cerebras.cloud.sdk import Cerebras


# Cerebras API Key (free tier)
CEREBRAS_API_KEY = ""

MAX_SUMMARY_TEXT_LENGTH = 10000
MAX_TAGS_TEXT_LENGTH = 8000

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB upload limit


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


@app.route("/", methods=["GET", "POST"])
def index():
    summary = None
    tags = None
    error = None


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
            except Exception as exc:
                error = f"Error processing request: {exc}"

    return render_template("index.html", summary=summary, tags=tags, error=error)


@app.route("/export/csljson", methods=["POST"])
def export_csljson():
    title = request.form.get("title", "")
    authors = request.form.get("authors", "")
    year = request.form.get("year", "")
    summary = request.form.get("summary", "")
    tags_str = request.form.get("tags", "")

    tags = [t.strip() for t in tags_str.split(",") if t.strip()]

    csl_item = {
        "type": "article-journal",
        "title": title,
        "author": [
            {"family": author.strip(), "given": ""}
            for author in authors.split(";")
            if author.strip()
        ],
        "issued": {"date-parts": [[year]]} if year else None,
        "note": summary,
        "keyword": ", ".join(tags),
    }

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
    summary_md = form.get("summary", "")

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

    note_html = f"<pre>{html.escape(summary_md)}</pre>"

    return {
        "itemType": "journalArticle",
        "title": title,
        "creators": creators,
        "date": year,
        "DOI": doi,
        "url": paper_url,
        "abstractNote": "",
        "tags": tags,
        "notes": [{"note": note_html}],
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

    url = f"https://api.zotero.org/{user_or_group}s/{lib_id}/items"
    headers = {
        "Zotero-API-Key": api_key,
        "Content-Type": "application/json",
    }
    payload = [item]

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if not resp.ok:
        return f"Zotero API error: {resp.status_code} {resp.text}", 500

    return render_template(
        "index.html",
        summary=request.form.get("summary"),
        tags=request.form.get("tags"),
        error=None,
        zotero_success=True,
    )


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug_mode)
