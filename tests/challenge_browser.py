"""Run the judge flow against an explicitly selected Challenge 1 deployment."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def main():
    base = os.environ["CHALLENGE_ORIGIN"]
    parsed = urlsplit(base)
    assert parsed.scheme == "https" and parsed.netloc and not parsed.path and not parsed.query
    local = parsed.hostname == "localhost"
    code = os.environ["CHALLENGE_ACCESS_CODE"]

    def open_session(browser):
        ctx = browser.new_context(base_url=base, ignore_https_errors=local, accept_downloads=True)
        page = ctx.new_page()
        page.goto("/")
        page.get_by_label("Judge access code").fill(code)
        page.get_by_role("button", name="Sign in").click()
        expect(page.get_by_role("button", name="Create and run broken sample")).to_be_visible()
        return ctx, page

    def propose(page):
        page.get_by_role("button", name="Create and run broken sample").click()
        expect(page.get_by_role("heading", name="Sample failed: 0 rows returned")).to_be_visible(timeout=30000)
        page.get_by_role("button", name="Request Vultr AI proposal").click()
        expect(page.get_by_role("heading", name="Proposal ready for review")).to_be_visible(timeout=20000)
        run = page.request.get("/api/repair").json()["run"]
        assert run["status"] == "awaiting-approval"
        return run

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        ctx, page = open_session(browser)
        anonymous = browser.new_context(base_url=base, ignore_https_errors=local).request
        assert anonymous.get("/healthz").json()["mode"] == "docker-sqlite-fixture"
        assert anonymous.get("/api/audit").status == 401
        assert anonymous.post("/action", data={}).status == 403
        run = propose(page)
        second, second_page = open_session(browser)
        assert second_page.request.get("/api/repair").json() == {"run": None}
        csrf = second_page.request.get("/session").json()["csrf"]
        assert second_page.request.post("/api/repair/decide",
            headers={"Origin": base, "X-CSRF-Token": csrf},
            data={"run_id": run["run_id"], "decision": "approve"}).status == 404
        second.close()
        page.get_by_role("button", name="Approve and run isolated repair").click()
        expect(page.get_by_role("heading", name="Repair committed and verified")).to_be_visible(timeout=30000)
        with page.expect_download() as event:
            page.get_by_role("button", name="Download audit JSON").click()
        exported = json.loads(Path(event.value.path()).read_text())
        assert exported == page.request.get("/api/audit").json()
        assert [row["status"] for row in exported["audit"]] == [
            "failure-observed", "awaiting-approval", "approved", "verified"]
        observed, proposed, _, verified = [item["evidence"] for item in exported["audit"]]
        assert observed["result"]["rows_returned"] == 0 and verified["after"]["rows_returned"] == 1
        assert verified["rows_updated"] == 1 and verified["committed"] is True
        assert verified["fixture_sha256"] == observed["fixture_sha256"] != verified["repaired_sha256"]
        if not local:
            assert proposed["inference"]["provider"] == "Vultr Serverless Inference"
        assert proposed["inference"]["completion_id"] and proposed["inference"]["model"]
        ids = []
        for evidence in (observed, verified):
            events = {event["event"]: event for event in evidence["sandbox"]}
            assert {"created", "started", "limits-checked", "completed", "runtime-checked",
                    "cleanup-confirmed"} <= events.keys()
            assert events["completed"]["exit_code"] == 0
            assert events["runtime-checked"]["network_interfaces"] == ["lo"]
            ids.append(events["created"]["container_id"])
        assert len(set(ids)) == 2
        output = os.environ.get("CHALLENGE_AUDIT_FILE")
        if output:
            Path(output).write_text(json.dumps(exported, indent=2) + "\n")
        propose(page)
        page.get_by_role("button", name="Reject proposal").click()
        expect(page.get_by_role("heading", name="Proposal rejected; no repair worker started")).to_be_visible()
        rejected = page.request.get("/api/audit").json()["audit"]
        assert rejected[-1]["evidence"] == {"rows_updated": 0}
        assert not any(event["task"] == "apply" for row in rejected
                       for event in row["evidence"].get("sandbox", []))
        ctx.close()
        print(json.dumps({"health": "pass", "anonymous_access": "denied", "approval": "pass",
                          "session_isolation": "pass", "rejection": "pass", "audit_export": "pass",
                          "inference": proposed["inference"], "container_ids": ids}))


if __name__ == "__main__":
    main()
