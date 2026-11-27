#!/usr/bin/env python
"""Submit the platform's WhatsApp message templates to Meta for approval.

The templates come from apps/messaging/templates.py: the same definitions the code uses to send them,
so the approved wording and the parameters we send cannot drift apart.

    python scripts/submit_whatsapp_templates.py               # dry run (default): prints payloads, no network
    python scripts/submit_whatsapp_templates.py --submit      # creates all templates, one after another
    python scripts/submit_whatsapp_templates.py --status      # shows the current approval status
    python scripts/submit_whatsapp_templates.py --submit --only report_delivery

Environment (never pass the token on the command line, where it lands in shell history). Inside the web
container the app's own .env names are used automatically (in brackets):
    WABA_ID              WhatsApp Business Account ID, not the phone number ID [WHATSAPP_BUSINESS_ACCOUNT_ID]
    META_ACCESS_TOKEN    System User token with whatsapp_business_management [WHATSAPP_ACCESS_TOKEN]
    GRAPH_API_VERSION    e.g. v21.0 (default) [WHATSAPP_GRAPH_VERSION]
    META_APP_ID          Meta app ID. Needed only to upload the sample PDF that Meta requires for the
                         document header of report_delivery_v1 (Resumable Upload API). [WHATSAPP_APP_ID]
    SAMPLE_PDF_PATH      optional: your own sample PDF for that header. Without it a one-page specimen with
                         no personal data is generated. Never use a real customer's report.

Endpoints used, per Meta's WhatsApp Business Management API docs (check them against the version you set):
    POST /{version}/{app_id}/uploads?file_name&file_length&file_type   -> {"id": "upload:..."}
    POST /{version}/{upload_id}  (header file_offset: 0, raw bytes)    -> {"h": "<header handle>"}
    POST /{version}/{waba_id}/message_templates                        -> {"id", "status", "category"}
    GET  /{version}/{waba_id}/message_templates?name=...&fields=...
The token is sent only in the Authorization header, never in a URL, and is never printed.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/, for apps.messaging.templates

from apps.messaging.templates import TEMPLATES, TemplateDef  # noqa: E402

GRAPH = "https://graph.facebook.com"
TIMEOUT = 30
HANDLE_PLACEHOLDER = "<header handle from the sample PDF upload>"


# --- payloads --------------------------------------------------------------------------------------------


def build_payload(t: TemplateDef, header_handle: str | None = None) -> dict:
    components: list[dict] = []
    if t.has_document_header:
        components.append(
            {
                "type": "HEADER",
                "format": "DOCUMENT",
                "example": {"header_handle": [header_handle or HANDLE_PLACEHOLDER]},
            }
        )
    components.append({"type": "BODY", "text": t.body, "example": {"body_text": [list(t.body_examples)]}})
    if t.footer:
        components.append({"type": "FOOTER", "text": t.footer})
    return {"name": t.default_name, "language": t.language, "category": t.category, "components": components}


def specimen_pdf() -> bytes:
    """A valid one-page PDF that says SAMPLE. Contains no personal data."""
    text = b"BT /F1 24 Tf 72 720 Td (SAMPLE CREDIT REPORT - SPECIMEN ONLY) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text), text),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def sample_pdf() -> tuple[bytes, str]:
    path = os.environ.get("SAMPLE_PDF_PATH")
    if path:
        data = Path(path).read_bytes()
        if not data.startswith(b"%PDF"):
            sys.exit(f"SAMPLE_PDF_PATH is not a PDF: {path}")
        return data, Path(path).name
    return specimen_pdf(), "sample-credit-report.pdf"


# --- Graph API -------------------------------------------------------------------------------------------


class GraphError(Exception):
    pass


class Graph:
    def __init__(self, version: str, token: str):
        import requests  # only needed when talking to Meta

        self.base = f"{GRAPH}/{version}"
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _json(self, resp) -> dict:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or "error" in body:
            err = body.get("error", {})
            raise GraphError(
                f"HTTP {resp.status_code}: {err.get('message', resp.reason)}"
                f" (code {err.get('code')}, subcode {err.get('error_subcode')},"
                f" fbtrace_id {err.get('fbtrace_id')})"
                + (f" - {err['error_user_msg']}" if err.get("error_user_msg") else "")
            )
        return body

    def upload_sample(self, app_id: str, data: bytes, filename: str) -> str:
        """Resumable Upload API: returns the handle Meta wants as the header example."""
        session = self._json(
            self.session.post(
                f"{self.base}/{app_id}/uploads",
                params={"file_name": filename, "file_length": len(data), "file_type": "application/pdf"},
                timeout=TIMEOUT,
            )
        )
        upload_id = session.get("id")
        if not upload_id:
            raise GraphError("upload session returned no id")
        done = self._json(
            self.session.post(
                f"{self.base}/{upload_id}",
                headers={"file_offset": "0", "Content-Type": "application/pdf"},
                data=data,
                timeout=TIMEOUT,
            )
        )
        if not done.get("h"):
            raise GraphError("upload returned no header handle")
        return done["h"]

    def create_template(self, waba_id: str, payload: dict) -> dict:
        return self._json(
            self.session.post(f"{self.base}/{waba_id}/message_templates", json=payload, timeout=TIMEOUT)
        )

    def template_status(self, waba_id: str, name: str) -> list[dict]:
        body = self._json(
            self.session.get(
                f"{self.base}/{waba_id}/message_templates",
                params={"name": name, "fields": "id,name,language,status,category,rejected_reason"},
                timeout=TIMEOUT,
            )
        )
        return [row for row in body.get("data", []) if row.get("name") == name]


# --- commands --------------------------------------------------------------------------------------------


# Each setting can come from its own name or from the name the app already uses in .env, so the script runs
# inside the web container with no extra configuration.
ALIASES = {
    "WABA_ID": ("WHATSAPP_BUSINESS_ACCOUNT_ID",),
    "META_ACCESS_TOKEN": ("WHATSAPP_ACCESS_TOKEN",),
    "GRAPH_API_VERSION": ("WHATSAPP_GRAPH_VERSION",),
    "META_APP_ID": ("WHATSAPP_APP_ID",),
}
DEFAULTS = {"GRAPH_API_VERSION": "v21.0"}  # same default as settings.WHATSAPP_GRAPH_VERSION


def env_value(name: str) -> str:
    for candidate in (name, *ALIASES.get(name, ())):
        value = os.environ.get(candidate, "").strip()
        if value:
            return value
    return DEFAULTS.get(name, "")


def require_env(*names: str) -> dict[str, str]:
    values = {n: env_value(n) for n in names}
    missing = [" or ".join((n, *ALIASES.get(n, ()))) for n, v in values.items() if not v]
    if missing:
        sys.exit("Missing environment variable(s): " + ", ".join(missing))
    return values


def selected(only: list[str] | None) -> list[TemplateDef]:
    unknown = set(only or []) - set(TEMPLATES)
    if unknown:
        sys.exit(f"Unknown template key(s): {', '.join(sorted(unknown))}. Known: {', '.join(TEMPLATES)}")
    return [t for k, t in TEMPLATES.items() if not only or k in only]


def dry_run(templates: list[TemplateDef]) -> int:
    print("DRY RUN - nothing is sent to Meta. Add --submit to create the templates.\n")
    for t in templates:
        print(f"== {t.default_name}  (key {t.key}, {t.param_count} body parameter(s), env {t.env_var})")
        print("   sent by: " + "; ".join(t.used_by))
        print(json.dumps(build_payload(t), ensure_ascii=False, indent=2))
        print()
    if any(t.has_document_header for t in templates):
        data, name = sample_pdf()
        print(f"Document header sample: {name}, {len(data)} bytes, uploaded with META_APP_ID at submit time.")
    present = [
        n for n in ("WABA_ID", "META_ACCESS_TOKEN", "GRAPH_API_VERSION", "META_APP_ID") if env_value(n)
    ]
    print("Environment variables present: " + (", ".join(present) or "none") + " (values not shown)")
    return 0


# Meta's error subcode for "There is already <language> content for this template".
ALREADY_EXISTS_SUBCODE = "subcode 2388024"


def _current_status(graph: "Graph", waba_id: str, t: TemplateDef) -> str:
    try:
        rows = graph.template_status(waba_id, t.default_name)
    except Exception:
        return "status lookup failed; check with --status"
    row = next((r for r in rows if r.get("language") == t.language), rows[0] if rows else None)
    if row is None:
        return "not visible to this token"
    reason = row.get("rejected_reason")
    extra = f" rejected_reason={reason}" if reason and reason != "NONE" else ""
    return f"id={row.get('id')} status={row.get('status')} category={row.get('category')}{extra}"


def submit(templates: list[TemplateDef]) -> int:
    names = ["WABA_ID", "META_ACCESS_TOKEN", "GRAPH_API_VERSION"]
    if any(t.has_document_header for t in templates):
        names.append("META_APP_ID")
    env = require_env(*names)
    graph = Graph(env["GRAPH_API_VERSION"], env["META_ACCESS_TOKEN"])
    failures = existing = 0
    handle = None
    for t in templates:  # sequential on purpose: one failure is reported and the rest still go
        print(f"-> {t.default_name}: ", end="", flush=True)
        try:
            if t.has_document_header and handle is None:
                data, filename = sample_pdf()
                handle = graph.upload_sample(env["META_APP_ID"], data, filename)
            result = graph.create_template(env["WABA_ID"], build_payload(t, handle))
        except GraphError as exc:
            if ALREADY_EXISTS_SUBCODE in str(exc):  # same name + language submitted before
                existing += 1
                print(f"already submitted ({_current_status(graph, env['WABA_ID'], t)})")
                continue
            failures += 1
            print(f"FAILED {exc}")
            continue
        except Exception as exc:  # network error etc.: report and continue with the next template
            failures += 1
            print(f"FAILED {type(exc).__name__}")
            continue
        print(f"id={result.get('id')} status={result.get('status')} category={result.get('category')}")
    submitted = len(templates) - failures - existing
    print(f"\n{submitted} submitted, {existing} already submitted, {failures} failed.")
    if existing:
        print(
            "An existing template can't be submitted again. To change its wording, use a new name (e.g. _v2)."
        )
    if submitted and not failures:
        print("Approval usually takes minutes to a day. Check with --status.")
    return 1 if failures else 0


def status(templates: list[TemplateDef]) -> int:
    env = require_env("WABA_ID", "META_ACCESS_TOKEN", "GRAPH_API_VERSION")
    graph = Graph(env["GRAPH_API_VERSION"], env["META_ACCESS_TOKEN"])
    for t in templates:
        try:
            rows = graph.template_status(env["WABA_ID"], t.default_name)
        except Exception as exc:
            detail = exc if isinstance(exc, GraphError) else type(exc).__name__
            print(f"{t.default_name}: lookup failed: {detail}")
            continue
        if not rows:
            print(f"{t.default_name}: not found")
        for row in rows:
            reason = row.get("rejected_reason")
            print(
                f"{t.default_name} [{row.get('language')}]: id={row.get('id')} status={row.get('status')}"
                f" category={row.get('category')}"
                + (f" rejected_reason={reason}" if reason and reason != "NONE" else "")
            )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--submit", action="store_true", help="create the templates at Meta")
    mode.add_argument("--status", action="store_true", help="show approval status")
    parser.add_argument("--only", nargs="+", metavar="KEY", help=f"subset of: {', '.join(TEMPLATES)}")
    args = parser.parse_args(argv)
    templates = selected(args.only)
    if args.submit:
        return submit(templates)
    if args.status:
        return status(templates)
    return dry_run(templates)


if __name__ == "__main__":
    sys.exit(main())
