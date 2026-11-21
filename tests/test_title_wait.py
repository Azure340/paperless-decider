#!/usr/bin/env python3
"""Offline checks for the title -> tags handoff (no servers, no model).

    python3 tests/test_title_wait.py

Covers the part that decides WHEN the sidecar acts, after paperless' own AI
workflow has written the title.
"""

import dataclasses
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app"))

os.environ.update(
    {
        "PAPERLESS_URL": "http://127.0.0.1:1",
        "PAPERLESS_API_TOKEN": "t",
        "DECIDER_SHARED_SECRET": "s",
        "DECIDER_FIELDS": "tag",
    }
)

import decider  # noqa: E402

BASE = decider.Config.from_env()


class StubPaperless:
    """Hands back one entry per document() call, then repeats the last one."""

    def __init__(self, versions):
        self.versions = versions
        self.calls = 0

    def document(self, doc_id):
        self.calls += 1
        return self.versions[min(self.calls - 1, len(self.versions) - 1)]


def _problem():
    return decider.HTTPProblem(404, "http://127.0.0.1:1/api/documents/5/", "Not Found")


class FlakyPaperless:
    """404 for the first `missing` reads, then `doc`: the commit race in one stub.

    paperless can deliver the workflow webhook from inside the consumer's still
    open transaction, so a brand new document legitimately answers 404 for a few
    hundred milliseconds. This is the shape that used to be skipped in silence.
    """

    def __init__(self, doc, missing):
        self.doc = doc
        self.missing = missing
        self.calls = 0

    def document(self, doc_id):
        self.calls += 1
        if self.calls <= self.missing:
            raise _problem()
        return self.doc


class VanishingPaperless:
    """Readable for `good` reads, then 404 forever: a deletion mid-flight."""

    def __init__(self, doc, good):
        self.doc = doc
        self.good = good
        self.calls = 0

    def document(self, doc_id):
        self.calls += 1
        if self.calls > self.good:
            raise _problem()
        return self.doc


class RecordingHandler(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.levelname + " " + record.getMessage())


def main():
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    placeholder = {
        "id": 5,
        "title": "scan-2026-10-13-allianz",
        "original_file_name": "scan-2026-10-13-allianz.pdf",
        "content": "invoice text " * 30,
        "tags": [],
    }
    titled = dict(placeholder, title="Allianz car insurance invoice 2026")

    check("untouched filename title reads as a placeholder", decider.title_is_placeholder(placeholder))
    check("AI-written title is not a placeholder", not decider.title_is_placeholder(titled))
    check(
        "a title whose words all come from the filename still waits",
        decider.title_is_placeholder(dict(placeholder, title="allianz scan")),
    )
    check(
        "accents and case do not matter",
        decider.title_is_placeholder(
            {"title": "Rechnung_Müller", "original_file_name": "rechnung-müller.PDF"}
        ),
    )

    no_wait = dataclasses.replace(BASE, wait_for_title=False)
    stub = StubPaperless([placeholder, placeholder, titled])
    decider.reload_for_title(5, no_wait, stub, {})
    check("wait_for_title=false reads the document exactly once", stub.calls == 1, "calls=" + str(stub.calls))

    wait = dataclasses.replace(BASE, wait_for_title=True, title_poll_seconds=0.05, title_poll_tries=5)
    stub = StubPaperless([placeholder, placeholder, titled])
    report = {}
    started = time.monotonic()
    got = decider.reload_for_title(5, wait, stub, report)
    elapsed = time.monotonic() - started
    check(
        "it re-reads until the title arrives",
        got is not None and got["title"] == titled["title"],
        str(got)[:80],
    )
    check(
        "and it actually waited (2 polls)",
        0.08 < elapsed < 1.0 and report.get("title_wait_seconds") in (0.1, 0.15),
        str(round(elapsed, 2)) + " " + str(report),
    )
    check("a successful wait records no give-up note", "title_wait" not in report, str(report))

    stub = StubPaperless([placeholder] * 30)
    report = {}
    got = decider.reload_for_title(5, wait, stub, report)
    check(
        "budget exhausted -> proceeds and says so instead of blocking forever",
        got is not None and "wait budget" in report.get("title_wait", ""),
        str(report),
    )
    check(
        "fetches == 1 + title_poll_tries",
        stub.calls == wait.title_poll_tries + 1,
        "calls=" + str(stub.calls),
    )

    # ---- the commit race: a webhook can arrive before the INSERT is visible ----
    flaky = FlakyPaperless(titled, missing=1)
    report = {}
    got = decider.reload_for_title(5, wait, flaky, report)
    check(
        "a 404 on the first read is retried instead of believed",
        got is not None and got["title"] == titled["title"] and flaky.calls == 2,
        "calls=" + str(flaky.calls) + " got=" + str(got is not None),
    )
    check(
        "and the report says it waited for the commit",
        report.get("uncommitted_retries") == 1 and report.get("commit_wait_seconds") == 0.05,
        # (a 0.05 s poll is what makes this readable in a test; production uses 5 s)
        str(report),
    )

    never = FlakyPaperless(titled, missing=99)
    report = {}
    got = decider.reload_for_title(5, wait, never, report)
    check(
        "404 for the whole budget -> gone, after exactly 1 + title_poll_tries reads",
        got is None and report.get("gone") == "never readable"
        and never.calls == wait.title_poll_tries + 1,
        "calls=" + str(never.calls) + " " + str(report),
    )

    quick = FlakyPaperless(titled, missing=99)
    report = {}
    got = decider.reload_for_title(5, no_wait, quick, report)
    check(
        "wait_for_title=false still gives up after one 404 (a probe must not stall)",
        got is None and quick.calls == 1,
        "calls=" + str(quick.calls),
    )

    vanished = VanishingPaperless(placeholder, good=1)
    report = {}
    got = decider.reload_for_title(5, wait, vanished, report)
    check(
        "a document lost during the title wait reads as deleted, not unreadable",
        got is None and report.get("gone") == "deleted while waiting for its title"
        and vanished.calls == 2,
        "calls=" + str(vanished.calls) + " " + str(report),
    )

    logger = logging.getLogger("decider")
    handler = RecordingHandler()
    level, propagate = logger.level, logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    decider.process_document(5, wait, FlakyPaperless(titled, missing=99), None)
    logger.removeHandler(handler)
    logger.setLevel(level)
    logger.propagate = propagate
    check(
        "a skip is logged, never silent",
        any(line.startswith("WARNING") and "skipped" in line for line in handler.lines),
        str(handler.lines),
    )

    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "  :: " + str(detail)))
    print("")
    print(str(len(checks) - len(failed)) + "/" + str(len(checks)) + " checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
