#!/usr/bin/env python3
"""Offline end-to-end test: fake paperless + fake Ollama, no network, no model.

    python3 tests/selftest.py

Starts throwaway servers on 127.0.0.1, points the real decider code at them and
asserts shortlisting, thresholds, review tagging, dry-run, idempotence and the
HTTP surface (including that every endpoint except /healthz needs the shared
secret). Non-zero exit on failure - paste the whole output when asking for help.
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
OLLAMA_CRASH_PORT = 18102
SECRET = "s3cret"

PATCHES = []
BATCH_SIZES = []
READS = {}
LATE_TAGS = []
LATE_FIELD = []

TAGS = [
    {"id": 1, "name": "Insurance", "document_count": 40},
    {"id": 2, "name": "Car", "document_count": 30},
    {"id": 3, "name": "Health", "document_count": 25},
    {"id": 4, "name": "Urgent", "document_count": 3},
    {"id": 5, "name": "Pets", "document_count": 2},
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
    # paperless returns tag PRIMARY KEYS, not objects. The fake used to return
    # [{"id": 2}], which agreed with a broken reader and hid a data-loss bug.
    "tags": [2],
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
            READS["77"] = READS.get("77", 0) + 1
            if READS["77"] == 1:
                return self._send(200, DOCUMENT)
            late = dict(DOCUMENT)
            late["tags"] = list(DOCUMENT["tags"]) + list(LATE_TAGS)
            if LATE_FIELD:
                late[LATE_FIELD[0]] = LATE_FIELD[1]
            return self._send(200, late)
        if path == "/api/documents/78/":
            return self._send(200, {"id": 78, "title": "blank scan", "content": "", "tags": []})
        if path == "/api/documents/80/":
            # a payload without a tags field at all: writing would be a blind replace
            return self._send(200, {"id": 80, "title": "no tags key", "content": DOCUMENT["content"]})
        if path == "/api/documents/79/":
            # a document whose tags are objects, the shape some payloads use
            return self._send(200, dict(DOCUMENT, id=79, tags=[{"id": 2}]))
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
        if not questions:
            return self._send(400, {"error": "no questions"})
        BATCH_SIZES.append(len(questions))
        batched = len(questions) > 1
        answers = {key: self._answer(question, batched) for key, question in questions.items()}
        return self._send(200, {"answers": answers, "usage": {"input_tokens": 220, "output_tokens": 0}})

    def _answer(self, question, batched):
        instructions = question.get("instructions", "")
        criteria = question.get("criteria") or {}
        if question.get("type") == "noul":
            if "Pets" in instructions:
                # Deliberately straddles DECIDER_THRESH_TAG=0.80: read 0.78 when asked
                # in a batch, 0.86 when asked alone. Only the grey-zone re-ask turns
                # that into an apply, so these two numbers make the behaviour visible.
                return {"type": "noul", "noul": 0.78 if batched else 0.86}
            prob = 0.93 if "Insurance" in instructions else 0.08
            return {"type": "noul", "noul": prob}

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
        return {
            "type": "choice",
            "choice": key,
            "probabilities": probabilities,
            "confidence": prob,
        }

    def log_message(self, *args):
        pass


class FakeCrashOllama(BaseHTTPRequestHandler):
    """An upstream that accepts the connection and hangs up with no reply.

    Exactly what Ollama does when its llama-server child is SIGKILLed (the 4.6 GB
    tev1 Q8_0 build on a memory-capped container): the client gets
    RemoteDisconnected rather than an HTTP status, which used to escape
    request_json uncaught and abort the decision with a bare traceback.
    """

    port = OLLAMA_CRASH_PORT

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.close_connection = True
        self.connection.close()

    def log_message(self, *args):
        pass


def serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", handler.port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def read_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
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
            "DECIDER_SHARED_SECRET": SECRET,
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
            "DECIDER_SKIP_TAGS": "",
            "DECIDER_HOST": "127.0.0.1",
            "DECIDER_PORT": str(SIDECAR_PORT),
            "DECIDER_LOG_LEVEL": "CRITICAL",
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
        "tags=" + str(patch.get("tags")),
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

    import dataclasses

    before = len(PATCHES)
    dry = decider.process_document(77, dataclasses.replace(cfg, dry_run=True), pl, ollama)
    check(
        "dry run decides but writes nothing",
        len(PATCHES) == before
        and dry["result"] == "dry run"
        and dry["would_patch"].get("correspondent") == 10,
        str(dry.get("result")),
    )

    single = dataclasses.replace(cfg, fields=("storage_path",))
    again = decider.process_document(77, single, pl, ollama)
    check(
        "no PATCH when nothing would change",
        again["result"] == "nothing to apply" and len(PATCHES) == before,
        str(again.get("result")),
    )

    # ---- the service's own tags must never be candidates ----
    wide = dataclasses.replace(cfg, max_tag_options=25)
    offered = [d.label for d in decider.decide_tags(wide, pl, ollama, DOCUMENT["content"], set())]
    check(
        "bookkeeping tags exist in the fake taxonomy (test is meaningful)",
        "ai-review" in [t["name"] for t in TAGS],
        str([t["name"] for t in TAGS]),
    )
    check(
        "the service's own bookkeeping tags are never offered to the model",
        "ai-review" not in offered,
        str(offered),
    )
    check(
        "real taxonomy tags are still offered (nothing over-blocked)",
        set(["Insurance", "Car", "Health", "Urgent", "Pets"]) == set(offered),
        str(offered),
    )
    check(
        "marker_tag_ids resolves both configured markers",
        decider.marker_tag_ids(cfg, pl) == set([90]),
        str(decider.marker_tag_ids(cfg, pl)),
    )
    check(
        "no markers configured -> nothing blocked beyond current tags",
        decider.marker_tag_ids(dataclasses.replace(cfg, review_tag="", done_tag=""), pl) == set(),
    )

    # ---- DECIDER_SKIP_TAGS ----
    skip_cfg = dataclasses.replace(cfg, skip_tags=("Health", "no-such-tag-name"))
    skipped = [d.label for d in decider.decide_tags(skip_cfg, pl, ollama, DOCUMENT["content"], set())]
    check(
        "DECIDER_SKIP_TAGS keeps a named tag out of the candidates",
        "Health" not in skipped and "Insurance" in skipped,
        str(skipped),
    )
    check(
        "skipped tags and bookkeeping tags are excluded together",
        # skip_cfg keeps the env shortlist of 3, so `Pets` is legitimately absent here:
        # this check is about what got blocked, not about ask-all coverage.
        set(skipped) == set(["Car", "Insurance", "Urgent"]),
        str(skipped),
    )
    os.environ["DECIDER_SKIP_TAGS"] = "Inbox, Back up,Misc"
    parsed = decider.Config.from_env().skip_tags
    os.environ["DECIDER_SKIP_TAGS"] = ""
    check(
        "DECIDER_SKIP_TAGS parses as a comma separated list",
        parsed == ("Inbox", "Back up", "Misc"),
        str(parsed),
    )
    check(
        "an unknown skip name is warned about, not ignored silently",
        "no-such-tag-name" in decider.WARNED_MISSING_SKIP,
        str(sorted(decider.WARNED_MISSING_SKIP)),
    )

    # ---- batching, ask-all and the grey zone ----
    all_cfg = dataclasses.replace(cfg, max_tag_options=0)
    asked_all = [d.label for d in decider.decide_tags(all_cfg, pl, ollama, DOCUMENT["content"], set())]
    check(
        "max_tag_options=0 asks about every visible tag, not a shortlist",
        set(asked_all) == set(["Insurance", "Car", "Health", "Urgent", "Pets"]),
        str(asked_all),
    )
    check(
        "a tag only reachable outside the shortlist still gets applied",
        "Pets" in asked_all
        and any(d.label == "Pets" and d.probability >= 0.80 for d in
                 decider.decide_tags(all_cfg, pl, ollama, DOCUMENT["content"], set())),
        str(asked_all),
    )
    check(
        "questions really go out batched, several per request",
        bool(BATCH_SIZES) and max(BATCH_SIZES) > 1,
        str(BATCH_SIZES[-8:]),
    )
    pets_list = [d for d in decider.decide_tags(all_cfg, pl, ollama, DOCUMENT["content"], set())
                 if d.label == "Pets"]
    pets = pets_list[0] if pets_list else None
    check(
        "a batched answer near the threshold is re-asked alone and the solo number wins",
        pets is not None and pets.probability == 0.86 and "re-asked alone" in pets.note,
        "Pets missing" if pets is None else str(pets.as_dict()),
    )
    strict = dataclasses.replace(all_cfg, grey_zone=0.0)
    pets_strict_list = [d for d in decider.decide_tags(strict, pl, ollama, DOCUMENT["content"], set())
                        if d.label == "Pets"]
    pets_strict = pets_strict_list[0] if pets_strict_list else None
    check(
        "grey_zone=0 disables the re-ask (batched 0.78 stays refused at 0.80)",
        pets_strict is not None and pets_strict.probability == 0.78
        and "below threshold" in pets_strict.note,
        "Pets missing" if pets_strict is None else str(pets_strict.as_dict()),
    )
    solo = dataclasses.replace(all_cfg, questions_per_request=1)
    solo_before = len(BATCH_SIZES)
    solo_pets_list = [d for d in decider.decide_tags(solo, pl, ollama, DOCUMENT["content"], set())
                      if d.label == "Pets"]
    solo_pets = solo_pets_list[0] if solo_pets_list else None
    check(
        "questions_per_request=1 restores the old one-question-per-request mode",
        max(BATCH_SIZES[solo_before:]) == 1
        and solo_pets is not None and solo_pets.probability == 0.86,
        str(BATCH_SIZES[solo_before:]),
    )

    # ---- shortlist recall: stems and tie-breaks ----
    check(
        "stem() folds plurals, -ing and -ment without eating short words",
        [decider.stem(w) for w in ("taxes", "tax", "licenses", "licensing", "amendments",
                                    "visa", "data", "the")] ==
        ["tax", "tax", "licens", "licens", "amend", "visa", "data", "the"],
        str([decider.stem(w) for w in ("taxes", "licenses", "amendments")]),
    )
    recall = [{"id": 201, "name": "US Taxes", "document_count": 115},
              {"id": 202, "name": "Passports", "document_count": 120},
              {"id": 203, "name": "Work", "document_count": 121},
              {"id": 204, "name": "Pool", "document_count": 119}]
    tax_text = "INTERNAL REVENUE SERVICE Amended return for tax year 2025. Tax paid."
    picked = [c["name"] for c in decider.shortlist(recall, tax_text, 3)]
    check(
        "a tag whose singular form appears in the text reaches the shortlist",
        "US Taxes" in picked,
        str(picked),
    )
    check(
        "unrelated high-usage tags do not outrank the one that matched",
        bool(picked) and picked[0] == "US Taxes",
        str(picked),
    )
    tied = [{"id": 1, "name": "Alpha Tag", "document_count": 100},
            {"id": 2, "name": "Zeta Tag", "document_count": 115}]
    first_tied = decider.shortlist(tied, "nothing here names any of these", 1)
    check(
        "with no lexical signal the busier tag wins the tie, not the alphabetically first",
        [c["name"] for c in first_tied] == ["Zeta Tag"],
        str(decider.shortlist(tied, "nothing here names any of these", 2)),
    )

    # ---- an inference server that dies mid-request ----
    crash_server = serve(FakeCrashOllama)
    crash_cfg = dataclasses.replace(
        cfg,
        ollama_url="http://127.0.0.1:" + str(OLLAMA_CRASH_PORT),
        retries=1,
        decision_timeout=10.0,
    )
    crash_ollama = decider.Ollama(crash_cfg)
    raised = None
    try:
        crash_ollama.noul(
            "TITLE: Rechnung\n\nAllianz Kfz Versicherung",
            "Is this document about Insurance?",
            "The document is about Insurance.",
            "The document is not about Insurance.",
        )
    except Exception as exc:
        raised = exc
    check(
        "a hung-up inference connection is retried and reported, not raw",
        isinstance(raised, decider.DecisionError)
        and "RemoteDisconnected" in str(raised)
        and "2 attempts" in str(raised),
        str(raised)[:220],
    )

    crash_worker = decider.Worker(crash_cfg, pl, crash_ollama)
    threading.Thread(target=crash_worker.run, daemon=True).start()
    crash_worker.submit(77)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and (
        not decider.RESULTS or "error" not in decider.RESULTS[0]
    ):
        time.sleep(0.2)
    first = decider.RESULTS[0] if decider.RESULTS else {}
    check(
        "/status stores why a decision failed, not just 'see log'",
        crash_worker.failed == 1
        and "RemoteDisconnected" in str(first.get("error", ""))
        and first.get("document_id") == 77,
        str(first)[:240],
    )
    crash_server.shutdown()

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
    time.sleep(0.3)

    base = "http://127.0.0.1:" + str(SIDECAR_PORT)
    auth = {"X-Decider-Token": SECRET}

    started = time.monotonic()
    accepted = post_json(base + "/decide", {"id": "999"}, auth)
    elapsed = time.monotonic() - started
    check(
        "POST /decide answers far inside paperless' 5s webhook timeout",
        accepted.get("status") == "queued" and elapsed < 0.5,
        str(round(elapsed, 3)) + "s " + str(accepted),
    )
    check(
        "rejects a wrong shared secret with 401",
        post_json(base + "/decide", {"id": "999"}, {}).get("_status") == 401,
    )
    check(
        "422 when the workflow sent no id",
        post_json(base + "/decide", {"title": "x"}, auth).get("_status") == 422,
    )
    check("/healthz stays open for the container healthcheck", read_json(base + "/healthz").get("ok") is True)
    check(
        "GET /status needs the shared secret too",
        read_json(base + "/status").get("_status") == 401,
    )
    check(
        "GET /preview/<id> needs the shared secret too",
        read_json(base + "/preview/77").get("_status") == 401,
    )
    time.sleep(0.5)

    patches_now = len(PATCHES)
    preview = read_json(base + "/preview/77", auth)
    check(
        "GET /preview/<id> returns decisions and writes nothing",
        "decisions" in preview and len(PATCHES) == patches_now and preview.get("dry_run") is True,
        str(preview)[:200],
    )
    status = read_json(base + "/status", auth)
    check("GET /status exposes recent results", bool(status.get("last_results")), str(status)[:160])
    check(
        "GET /status reports the skip list so it can be verified live",
        status.get("skip_tags") == [],
        str(status.get("skip_tags")),
    )

    # ---- a write must only ever ADD ----
    check(
        "tag_ids() reads paperless' real shape (a list of primary keys)",
        decider.tag_ids({"tags": [3, 24]}) == set([3, 24])
        and decider.tag_ids({"tags": [{"id": 3}]}) == set([3])
        and decider.tag_ids({}) is None,
        str(decider.tag_ids({"tags": [3, 24]})),
    )

    patch_before = len(PATCHES)
    kept = decider.process_document(79, cfg, pl, ollama)
    kept_patch = PATCHES[-1]["body"] if len(PATCHES) > patch_before else {}
    dry_first = decider.process_document(77, dataclasses.replace(cfg, dry_run=True), pl, ollama)
    check(
        "the first read itself understands paperless' integer tag list",
        2 in (dry_first.get("would_patch", {}).get("tags") or []),
        str(dry_first.get("would_patch")),
    )
    check(
        "object-shaped tags are preserved too, not just primary keys",
        2 in (kept_patch.get("tags") or []) and 1 in (kept_patch.get("tags") or []),
        str(kept_patch) + " " + str(kept.get("result")),
    )

    # paperless adds the inbox tag while the sidecar waits for the AI title; the
    # write must not delete it again (this is exactly what happened on document 1334)
    LATE_TAGS[:] = [{"id": 24}]
    READS.clear()
    patch_before = len(PATCHES)
    raced = decider.process_document(77, cfg, pl, ollama)
    raced_patch = PATCHES[-1]["body"] if len(PATCHES) > patch_before else {}
    check(
        "the write re-reads the document first",
        READS.get("77", 0) == 2,
        str(READS.get("77")),
    )
    check(
        "a tag paperless added during the wait survives the write",
        24 in (raced_patch.get("tags") or []) and 1 in (raced_patch.get("tags") or []),
        str(raced_patch),
    )
    check(
        "and the report says which tags came from somewhere else",
        raced.get("tags_added_by_others") == [24],
        str(raced.get("tags_added_by_others")),
    )
    del LATE_TAGS[:]

    LATE_FIELD[:] = ["correspondent", 11]
    READS.clear()
    patch_before = len(PATCHES)
    human = decider.process_document(77, cfg, pl, ollama)
    human_patch = PATCHES[-1]["body"] if len(PATCHES) > patch_before else {}
    check(
        "a correspondent a human set during the wait is not overwritten",
        "correspondent" not in human_patch
        and "correspondent" in (human.get("skipped_stale_fields") or []),
        str(human_patch) + " " + str(human.get("skipped_stale_fields")),
    )
    del LATE_FIELD[:]

    refused = decider.process_document(80, cfg, pl, ollama)
    check(
        "a document whose tags cannot be read is never written",
        "refusing to write" in str(refused.get("skipped", ""))
        and "would_patch" not in refused,
        str(refused)[:220],
    )

    for httpd in fakes + [sidecar]:
        httpd.shutdown()

    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "  :: " + str(detail)))
    print("")
    print(str(len(checks) - len(failed)) + "/" + str(len(checks)) + " checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
