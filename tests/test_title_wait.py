#!/usr/bin/env python3
"""Offline checks for the title -> tags handoff (no servers, no model).

    python3 tests/test_title_wait.py

Covers the part that decides WHEN the sidecar acts, after paperless' own AI
workflow has written the title.
"""

import dataclasses
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

    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "  :: " + str(detail)))
    print("")
    print(str(len(checks) - len(failed)) + "/" + str(len(checks)) + " checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
