#!/usr/bin/env python3
"""Offline end-to-end test: fake paperless + fake Ollama, no network, no model.

    python3 tests/selftest.py

Starts throwaway servers on 127.0.0.1, points the real decider code at them and
asserts shortlisting, thresholds, review tagging, dry-run, idempotence and the
HTTP surface. Non-zero exit on failure - paste the whole output when asking for
help. Nothing outside 127.0.0.1 is contacted.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app"))

PAPERLESS_PORT = 18099
OLLAMA_PORT = 18100
SIDECAR_PORT = 18101

PATCHES = []

TAGS = [
    {"id": 1, "name": "Insurance", "document_count": 40},
    {"id": 2, "name": "Car", "document_count": 30},
    {"id": 3, "name": "Health", "document_count": 25},
    {"id": 4, "name": "Urgent", "document_count": 3},
    {"id": 90, "name": "ai-review", "document_count": 1},
]
CORRESPONDENTS = [
    {"id": 10, "name": "Allianz", "document_count": 20},
    {"id": 11, "name": "Deutsche Bahn", "document_count": 12},
    {"id": 12, "name": "AOK", "document_count": 9},
]
DOCUMENT_TYPES = [
    {"id": 20, "name": "Invoice", "document_count": 50},
    {"id": 21, "name": "Contract", "document_count": 7},
    {"id": 22, "name": "Statement", "document_count": 4},
]
STORAGE_PATHS = [
    {"id": 30, "name": "Finance", "document_count": 5},
    {"id": 31, "name": "Medical", "document_count": 2},
]

# correspondent unset  -> decide it (fake answers confidently)
# document type unset  -> fake answers below threshold -> review, no write
# storage path set     -> must be left alone (overwrite off)
# tag 2 (Car) present  -> must survive the tags PATCH
DOCUMENT = {
    "id": 77,
    "title": "Rechnung Allianz Kfz",
    "original_file_name": "scan-2026-10-13-allianz.pdf",
    "content": (
        "Allianz Versicherungs-AG, Kfz-Haftpflicht Versicherung. "
        "Rechnung Nr. 99-1234, Versicherungsnehmer: Max Mustermann. "
        "Betrag 104,99 EUR faellig bis 30.11.2026. "
        "Insurance invoice for the car policy renewal. Payment due month end. "
    ),
    "tags": [{"id": 2}],
    "correspondent": None,
    "document_type": None,
    "storage_path": 30,
}


class FakePaperless(BaseHTTPRequestHandler):
    port = PAPERLESS_PORT

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8", "replace") or "{}")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/documents/77/":
            return self._send(200, DOCUMENT)
        if path == "/api/documents/78/":
            return self._send(200, {"id": 78, "title": "blank scan", "content": "", "tags": []})
        if path.startswith("/api/documents/"):
            return self._send(404, {"error": "not found"})
        for prefix, items in (
            ("/api/tags", TAGS),
            ("/api/correspondents", CORRESPONDENTS),
            ("/api/document_types", DOCUMENT_TYPES),
            ("/api/storage_paths", STORAGE_PATHS),
        ):
            if path.startswith(prefix):
                return self._send(200, {"results": items, "next": None})
        return self._send(404, {"detail": "nope"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        if path == "/api/tags/":
            new = {"id": 91, "name": body.get("name", ""), "document_count": 0}
            TAGS.append(new)
            return self._send(201, new)
        return self._send(404, {"detail": "nope"})

    def do_PATCH(self):
        body = self._body()
        PATCHES.append({"path": urllib.parse.urlparse(self.path).path, "body": body})
        merged = dict(DOCUMENT)
        merged.update(body)
        return self._send(200, merged)

    def log_message(self, *args):
        pass


class FakeOllama(BaseHTTPRequestHandler):
    """Deliberately mixed confidence.

    correspondent Allianz  p=0.97 -> apply
    document type Invoice  p=0.55 -> below threshold -> review, no write
    noul about Insurance   p=0.93 -> add tag
    noul anything else     p=0.08 -> skip
    """

    port = OLLAMA_PORT

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8", "replace") or "{}")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        if path == "/api/show":
            return self._send(200, {"capabilities": ["completion", "decision"]})
        if path != "/v1/systemone":
            return self._send(404, {"error": "no such endpoint"})
        questions = body.get("questions") or {}
        if len(questions) != 1:
            return self._send(400, {"error": "this fake answers one question at a time"})
        question = questions["q"]
        instructions = question.get("instructions", "")
        criteria = question.get("criteria") or {}
        if question.get("type") == "noul":
            prob = 0.93 if "Insurance" in instructions else 0.08
            return self._send(200, {"answers": {"q": {"type": "noul", "noul": prob}}})

        lowered = instructions.lower()
        if "correspondent" in lowered:
            winner, prob = "Allianz", 0.97
        elif "document type" in lowered:
            winner, prob = "Invoice", 0.55
        else:
            winner, prob = None, 0.40
        if winner is None or winner not in criteria.values():
            key = "cnone"
        else:
            key = next(k for k, v in criteria.items() if v == winner)
        probabilities = dict.fromkeys(criteria, 0.01)
        probabilities[key] = prob
        return self._send(
            200,
            {
                "answers": {
                    "q": {
                        "type": "choice",
                        "choice": key,
                        "probabilities": probabilities,
                        "confidence": prob,
                    },
                },
                "usage": {"input_tokens": 220, "output_tokens": 0},
            },
        )

    def log_message(self, *args):
        pass


def serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", handler.port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"_status": exc.code, "_body": exc.read().decode("utf-8", "replace")[:200]}


def post_json(url, body, headers):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers)
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return dict({"_status": exc.code}, **json.loads(raw))
        except json.JSONDecodeError:
            return {"_status": exc.code, "_body": raw[:200]}


def main():
    os.environ.update(
        {
            "PAPERLESS_URL": "http://127.0.0.1:" + str(PAPERLESS_PORT),
            "PAPERLESS_API_TOKEN": "testtoken",
            "OLLAMA_URL": "http://127.0.0.1:" + str(OLLAMA_PORT),
            "DECIDER_MODEL": "fake-decision",
            "DECIDER_SHARED_SECRET": "s3cret",
            "DECIDER_FIELDS": "correspondent,document_type,storage_path,tag",
            "DECIDER_OVERWRITE": "false",
            "DECIDER_DRY_RUN": "false",
            "DECIDER_REVIEW_TAG": "ai-review",
            "DECIDER_DONE_TAG": "",
            "DECIDER_THRESH_SINGLE": "0.85",
            "DECIDER_THRESH_TAG": "0.80",
            "DECIDER_MAX_TAG_OPTIONS": "3",
            "DECIDER_MAX_SINGLE_OPTIONS": "10",
            "DECIDER_MIN_CONTENT_CHARS": "80",
            "DECIDER_HOST": "127.0.0.1",
            "DECIDER_PORT": str(SIDECAR_PORT),
            "DECIDER_LOG_LEVEL": "ERROR",
        }
    )
    fakes = [serve(FakePaperless), serve(FakeOllama)]

    import decider

    cfg = decider.Config.from_env()
    pl = decider.Paperless(cfg)
    ollama = decider.Ollama(cfg)

    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    check("capability probe sees 'decision'", "decision" in ollama.model_capabilities())

    ranked = [r["name"] for r in decider.shortlist(TAGS, DOCUMENT["content"], 2)]
    check(
        "shortlist keeps the names present in the text, drops the rest",
        set(ranked) == set(["Insurance", "Car"]),
        str(ranked),
    )

    report = decider.process_document(77, cfg, pl, ollama)
    patch = PATCHES[-1]["body"] if PATCHES else {}

    check(
        "exactly one PATCH, to the right document",
        len(PATCHES) == 1 and PATCHES[0]["path"] == "/api/documents/77/",
        str(PATCHES),
    )
    check(
        "confident correspondent (p=0.97) applied",
        patch.get("correspondent") == 10,
        str(report["decisions"].get("correspondent")),
    )
    check("unsure document type (p=0.55) not written", "document_type" not in patch, str(patch))
    check(
        "unsure document type listed as unresolved",
        report["unresolved_fields"] == ["document_type"],
        str(report.get("unresolved_fields")),
    )
    check("needs_review true", report["needs_review"] is True)
    check(
        "review tag + confident tag + pre-existing tag all kept",
        sorted(patch.get("tags") or []) == [1, 2, 90],
        "tags=" + str(patch.get("tags")) + " (1 Insurance, 2 pre-existing Car, 90 ai-review)",
    )
    check(
        "storage_path untouched (already set, overwrite off)",
        "storage_path" not in patch
        and report["decisions"]["storage_path"]["note"].startswith("already set"),
        str(report["decisions"]["storage_path"]),
    )

    blank = decider.process_document(78, cfg, pl, ollama)
    check("blank scan skipped, no extra PATCH", "skipped" in blank and len(PATCHES) == 1, str(blank))

    missing = decider.process_document(999, cfg, pl, ollama)
    check(
        "404 document handled gracefully",
        missing.get("skipped") == "document no longer exists",
        str(missing),
    )

    before = len(PATCHES)
    dry_cfg = dataclasses_replace(decider, cfg, dry_run=True)
    dry = decider.process_document(77, dry_cfg, pl, ollama)
    check(
        "dry run decides but writes nothing",
        len(PATCHES) == before
        and dry["result"] == "dry run"
        and dry["would_patch"].get("correspondent") == 10,
        str(dry.get("result")),
    )

    single = dataclasses_replace(decider, cfg, fields=("storage_path",))
    again = decider.process_document(77, single, pl, ollama)
    check(
        "no PATCH when nothing would change",
        again["result"] == "nothing to apply" and len(PATCHES) == before,
        str(again.get("result")),
    )

    # ---- HTTP surface ----
    worker = decider.Worker(cfg, pl, ollama)
    threading.Thread(target=worker.run, daemon=True).start()

    class Sidecar(decider.Handler):
        pass

    Sidecar.cfg = cfg
    Sidecar.worker = worker
    Sidecar.pl = pl
    Sidecar.ollama = ollama
    Sidecar.port = SIDECAR_PORT
    sidecar = serve(Sidecar)
    time.sleep(0.2)

    started = time.monotonic()
    accepted = post_json(
        "http://127.0.0.1:" + str(SIDECAR_PORT) + "/decide",
        {"id": "999"},
        {"X-Decider-Token": "s3cret"},
    )
    elapsed = time.monotonic() - started
    check(
        "POST /decide answers far inside paperless' 5s webhook timeout",
        accepted.get("status") == "queued" and elapsed < 0.5,
        str(round(elapsed, 3)) + "s " + str(accepted),
    )
    check(
        "rejects a wrong shared secret with 401",
        post_json(
            "http://127.0.0.1:" + str(SIDECAR_PORT) + "/decide", {"id": "999"}, {}
        ).get("_status")
        == 401,
    )
    check(
        "422 when the workflow sent no id",
        post_json(
            "http://127.0.0.1:" + str(SIDECAR_PORT) + "/decide",
            {"title": "x"},
            {"X-Decider-Token": "s3cret"},
        ).get("_status")
        == 422,
    )
    time.sleep(0.4)

    patches_now = len(PATCHES)
    preview = get_json("http://127.0.0.1:" + str(SIDECAR_PORT) + "/preview/77")
    check(
        "GET /preview/<id> returns decisions and writes nothing",
        "decisions" in preview and len(PATCHES) == patches_now and preview.get("dry_run") is True,
        str(preview)[:200],
    )
    status = get_json("http://127.0.0.1:" + str(SIDECAR_PORT) + "/status")
    check("GET /status exposes recent results", bool(status.get("last_results")), str(status)[:160])

    for httpd in fakes + [sidecar]:
        httpd.shutdown()

    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "  :: " + str(detail)))
    print("")
    print(str(len(checks) - len(failed)) + "/" + str(len(checks)) + " checks passed")
    return 1 if failed else 0


def dataclasses_replace(decider, cfg, **changes):
    import dataclasses

    return dataclasses.replace(cfg, **changes)


if __name__ == "__main__":
    sys.exit(main())
