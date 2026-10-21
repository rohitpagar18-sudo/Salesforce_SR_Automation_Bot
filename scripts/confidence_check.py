import html
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import webbrowser

from sr_automation.parser import parse_ticket


HOST = "127.0.0.1"
PORT = 8765
BASE_URL = f"http://{HOST}:{PORT}/"


MATCH_SOURCE_MAP = {
    "SUBJECT_ONLY_CONFIRMATION_REQUIRED": "SUBJECT_ONLY",
    "DESCRIPTION_ACTION_ONLY": "DESCRIPTION_ONLY",
    "SUBJECT_DESCRIPTION_MATCH": "SUBJECT_DESCRIPTION_MATCH",
    "SUBJECT_DESCRIPTION_CONFLICT": "SUBJECT_DESCRIPTION_CONFLICT",
    "NO_ACTION_FOUND": "NO_MATCH",
    "NO_SR_FOUND": "NO_MATCH",
    "MULTIPLE_ACTIONS": "NO_MATCH",
    "NEGATED_OR_MIXED_ACTION": "NO_MATCH",
}

REASON_TEXT = {
    "SUBJECT_ONLY_CONFIRMATION_REQUIRED": "Subject contains a clear action, but the description does not confirm it.",
    "DESCRIPTION_ACTION_ONLY": "Description contains the action, while the subject alone does not confirm it.",
    "SUBJECT_DESCRIPTION_MATCH": "Subject and description agree on the same action.",
    "SUBJECT_DESCRIPTION_CONFLICT": "Subject and description conflict on the action to take.",
    "NO_ACTION_FOUND": "No action phrase was found in the subject or description.",
    "NO_SR_FOUND": "No valid Case number was found in the text.",
    "MULTIPLE_ACTIONS": "Multiple action cues were detected in the same input.",
    "NEGATED_OR_MIXED_ACTION": "Negated or mixed action wording was detected; manual review is required.",
}


def classify_outcome(parsed) -> str:
    if parsed.action in {"UNKNOWN", "CONFLICT"}:
        return "MANUAL_REVIEW"
    if parsed.confidence == "LOW":
        return "MANUAL_REVIEW"
    return "AUTO_ELIGIBLE"


def format_match_source(reason_code: str) -> str:
    return MATCH_SOURCE_MAP.get(reason_code, "NO_MATCH")


def format_reason(reason_code: str) -> str:
    return REASON_TEXT.get(reason_code, "Text understanding did not produce a clear action decision.")


def build_result(subject: str, description: str):
    parsed = parse_ticket("confidence-check", subject, description)
    case_numbers = ", ".join(parsed.case_numbers) if parsed.case_numbers else "None"

    return {
        "action": "MIXED" if parsed.action == "CONFLICT" else parsed.action,
        "confidence": parsed.confidence,
        "match_source": format_match_source(parsed.reason_code),
        "case_numbers": case_numbers,
        "outcome": classify_outcome(parsed),
        "reason": format_reason(parsed.reason_code),
        "reason_code": parsed.reason_code,
    }


def result_panel_class(result, error=None) -> str:
    if error:
        return "result-panel danger"
    if result is None:
        return "result-panel neutral"
    if result["action"] in {"CONFLICT", "UNKNOWN"}:
        return "result-panel danger"
    if result["outcome"] == "AUTO_ELIGIBLE":
        return "result-panel success"
    if result["outcome"] == "MANUAL_REVIEW":
        return "result-panel warning"
    return "result-panel danger"


def render_page(subject: str = "", description: str = "", result=None, error: str | None = None) -> str:
    safe_subject = html.escape(subject)
    safe_description = html.escape(description)
    panel_class = result_panel_class(result, error)

    result_section = ""
    if result or error:
        if error:
            result_section = f"""
                <div id="result-panel" class="{panel_class}">
                    <h2>Result</h2>
                    <div class="result-message error-message">{html.escape(error)}</div>
                </div>
            """
        else:
            rows = [
                ("Detected action", result["action"]),
                ("Confidence", result["confidence"]),
                ("Match source", result["match_source"]),
                ("Case numbers", result["case_numbers"]),
                ("Outcome", result["outcome"]),
                ("Reason", result["reason"]),
                ("Raw reason code", result["reason_code"]),
            ]
            rendered_rows = "".join(
                f"<div class=\"result-row\"><span>{label}</span><strong>{html.escape(str(value))}</strong></div>"
                for label, value in rows
            )
            result_section = f"""
                <div id="result-panel" class="{panel_class}">
                    <h2>Result</h2>
                    <div class="result-grid">{rendered_rows}</div>
                </div>
            """

    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Salesforce SR Automation Confidence Check</title>
    <style>
        :root {{
            --bg: #f3f6fb;
            --panel: #ffffff;
            --border: #dfe7f2;
            --text: #1e293b;
            --muted: #58657a;
            --primary: #1d4ed8;
            --primary-dark: #1635a6;
            --success: #1f8f59;
            --warning: #b7791f;
            --danger: #b42318;
            --neutral: #475467;
            --button-gray: #dfe3e8;
            --button-gray-hover: #cdd5dd;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font-family: Arial, Helvetica, sans-serif;
            background: var(--bg);
            color: var(--text);
        }}
        .page {{
            max-width: 980px;
            margin: 32px auto;
            padding: 0 16px 32px;
        }}
        h1 {{
            margin-bottom: 20px;
            font-size: 2rem;
            color: var(--text);
        }}
        .card {{
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 10px;
            box-shadow: 0 4px 12px rgba(15, 23, 42, 0.04);
            padding: 22px;
        }}
        .field {{
            margin-bottom: 20px;
        }}
        label {{
            display: block;
            font-weight: 700;
            margin-bottom: 8px;
            color: var(--text);
        }}
        input[type=text], textarea {{
            width: 100%;
            border: 1px solid var(--border);
            border-radius: 6px;
            background: #fff;
            color: var(--text);
            padding: 10px 12px;
            font-size: 1rem;
            line-height: 1.5;
        }}
        input[type=text] {{
            height: 44px;
        }}
        textarea {{
            min-height: 180px;
            resize: vertical;
            font-family: Consolas, "Courier New", monospace;
        }}
        .actions {{
            display: flex;
            gap: 12px;
            margin-top: 8px;
            flex-wrap: wrap;
        }}
        button {{
            border: 0;
            border-radius: 6px;
            cursor: pointer;
            font-size: 1rem;
            font-weight: 700;
            padding: 10px 18px;
            transition: background-color 0.15s ease-in-out;
        }}
        .primary-btn {{
            background: var(--primary);
            color: #fff;
        }}
        .primary-btn:hover {{
            background: var(--primary-dark);
        }}
        .secondary-btn {{
            background: var(--button-gray);
            color: var(--text);
        }}
        .secondary-btn:hover {{
            background: var(--button-gray-hover);
        }}
        .inline-error {{
            margin-top: 10px;
            color: var(--danger);
            font-weight: 700;
            min-height: 1.2em;
        }}
        .result-panel {{
            margin-top: 26px;
            border-radius: 8px;
            border: 1px solid var(--border);
            padding: 18px 18px 10px;
        }}
        .result-panel.success {{
            border-color: rgba(31, 143, 89, 0.3);
            background: rgba(31, 143, 89, 0.08);
        }}
        .result-panel.warning {{
            border-color: rgba(183, 121, 31, 0.3);
            background: rgba(183, 121, 31, 0.08);
        }}
        .result-panel.danger {{
            border-color: rgba(180, 35, 24, 0.3);
            background: rgba(180, 35, 24, 0.08);
        }}
        .result-panel.neutral {{
            border-color: rgba(71, 84, 103, 0.25);
            background: rgba(71, 84, 103, 0.06);
        }}
        .result-panel h2 {{
            margin: 0 0 16px;
            font-size: 1.4rem;
        }}
        .result-grid {{
            display: grid;
            grid-template-columns: minmax(150px, 190px) minmax(0, 1fr);
            gap: 10px 18px;
        }}
        .result-row {{
            display: contents;
        }}
        .result-row span {{
            font-weight: 700;
            color: var(--muted);
        }}
        .result-row strong {{
            word-break: break-word;
            white-space: pre-wrap;
        }}
        .error-message {{
            color: var(--danger);
            font-weight: 700;
            white-space: pre-wrap;
        }}
        .disclaimer {{
            margin-top: 28px;
            font-size: 0.97rem;
            line-height: 1.6;
            color: var(--muted);
            background: #f8fafc;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 14px 16px;
        }}
        @media (max-width: 640px) {{
            .result-grid {{
                grid-template-columns: 1fr;
            }}
            .result-row span {{
                margin-top: 8px;
            }}
        }}
    </style>
</head>
<body>
    <div class=\"page\">
        <h1>Salesforce SR Automation Confidence Check</h1>
        <div class=\"card\">
            <form id=\"check-form\" method=\"post\" action=\"/\">
                <div class=\"field\">
                    <label for=\"subject\">Subject</label>
                    <input id=\"subject\" name=\"subject\" type=\"text\" value=\"{safe_subject}\" maxlength=\"500\" />
                </div>
                <div class=\"field\">
                    <label for=\"description\">Description</label>
                    <textarea id=\"description\" name=\"description\" rows=\"12\">{safe_description}</textarea>
                </div>
                <div class=\"actions\">
                    <button class=\"primary-btn\" type=\"submit\">Check Confidence</button>
                    <button id=\"clear-button\" class=\"secondary-btn\" type=\"button\">Clear</button>
                </div>
                <div id=\"form-error\" class=\"inline-error\" aria-live=\"polite\"></div>
            </form>
            {result_section}
        </div>
        <div class=\"disclaimer\">Text confidence check only. No Salesforce or ManageEngine records are read or changed. Final eligibility also depends on Salesforce status, activities, record type and configured rules.</div>
    </div>
    <script>
        document.addEventListener('DOMContentLoaded', function () {{
            const form = document.getElementById('check-form');
            const subjectField = document.getElementById('subject');
            const descriptionField = document.getElementById('description');
            const errorBox = document.getElementById('form-error');
            const clearButton = document.getElementById('clear-button');

            form.addEventListener('submit', function (event) {{
                const subject = subjectField.value.trim();
                const description = descriptionField.value.trim();
                if (!subject && !description) {{
                    event.preventDefault();
                    errorBox.textContent = 'Enter a subject or description before checking confidence.';
                    return;
                }}
                errorBox.textContent = '';
            }});

            clearButton.addEventListener('click', function () {{
                subjectField.value = '';
                descriptionField.value = '';
                const resultPanel = document.getElementById('result-panel');
                if (resultPanel) {{
                    resultPanel.remove();
                }}
                errorBox.textContent = '';
            }});
        }});
    </script>
</body>
</html>
"""


class ConfidenceHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_html(render_page())

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        form = parse_qs(raw_body, keep_blank_values=True)

        subject = form.get("subject", [""])[0].strip()
        description = form.get("description", [""])[0].strip()

        if not subject and not description:
            self.send_html(render_page(subject, description, error="Enter a subject or description before checking confidence."))
            return

        try:
            result = build_result(subject, description)
            self.send_html(render_page(subject, description, result=result))
        except Exception as exc:  # pragma: no cover - defensive UI path
            self.send_html(render_page(subject, description, error=f"Confidence check failed: {exc}"))

    def send_html(self, page: str) -> None:
        payload = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


def start_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, PORT), ConfidenceHandler)
    threading.Timer(0.5, lambda: webbrowser.open_new_tab(BASE_URL)).start()
    print(f"Confidence check UI is running at {BASE_URL}")
    return server


if __name__ == "__main__":
    server = start_server()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping confidence check UI.")
    finally:
        server.server_close()
        sys.exit(0)
