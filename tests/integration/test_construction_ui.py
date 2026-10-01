"""The construction UI: a page that can be read, and a page that tells the truth.

The substrate console (`test_task_api_and_ui.py`) has been served since the first
tranche and was never visually verified — no browser was ever connected to the
session that built it. The notes say so. This file is what can be asserted about a
page without a browser, and it is worth being precise about the difference:

* **Structural** — the construction vocabulary is present, the endpoints it calls are
  the ones that exist, the unmeasured state has its own shape.
* **Behavioural** — the served document actually contains working JavaScript and the
  ids its script reaches for.

Neither is a screenshot, and this file does not claim to be one. What it does claim is
that a page which renders nothing, or renders a page of `undefined`, or silently
confuses "unmeasured" with "on time", fails here.

## The unmeasured rule, asserted against the markup

The stylesheet gives an unmeasured work package a **dashed border and no bar**, where a
measured on-plan one gets a solid green bar. That difference is the test. A page that
rendered both as a green bar would pass every other assertion in this file and would
tell a PM that a project nobody reported on is a project that is on time — which is
the specific failure the `actual_updated` column and the `unmeasured` list exist to
prevent, and it would be reintroduced at the last mile, in CSS.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

pytestmark = pytest.mark.integration


def _page(text: str) -> str:
    """The script body, comments stripped.

    The same treatment `test_task_api_and_ui.py` applies, and for the same reason: a
    substring search over the whole document cannot tell a comment recording a mistake
    from code making one.
    """
    body = re.search(r"<script[^>]*>(.*)</script>", text, re.S)
    assert body is not None, "the page has no script"
    return re.sub(r"/\*.*?\*/", "", body.group(1), flags=re.S)


class TestThePageIsServed:
    async def test_it_serves_html(self, client: Any) -> None:
        response = await client.get("/api/v1/ui")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    async def test_it_declares_a_viewport(self, client: Any) -> None:
        """The HITL queue is designed at 375px, and a page without this says so."""
        page = (await client.get("/api/v1/ui")).text
        assert 'name="viewport"' in page
        assert "width=device-width" in page

    async def test_it_carries_no_cache(self, client: Any) -> None:
        """A stale page hides a fix. The build stamp exists for the same reason."""
        response = await client.get("/api/v1/ui")
        assert "no-store" in response.headers.get("cache-control", "")


class TestThePageHasConstructionVocabulary:
    """Before this, the page contained no construction words at all.

    `PRODUCT_GAP.md` recorded it as "three tabs, zero construction vocabulary". Each
    section below is named for what a person in that role is looking at.
    """

    async def test_the_surfaces_are_present(self, client: Any) -> None:
        """Every section the product is supposed to have, by name.

        The list is the one the rebuild settled on: a dashboard, a project list with a
        breakdown and a progress table, an approval queue, an agent roster, a decision
        log, and the substrate console. An earlier version asserted "three tabs", which
        was a miscount of a page that had no tabs at all -- so the assertion is against
        names that mean something rather than against a number.
        """
        page = (await client.get("/api/v1/ui")).text
        for heading in (
            "Portfolio",
            "Work breakdown",
            "Progress",
            "Approvals",
            "Roster",
            "Decision log",
            "Behind schedule",
            "Needs a person",
        ):
            assert heading in page, f"the page has no {heading!r} section"

    async def test_the_role_switcher_names_the_three_roles(self, client: Any) -> None:
        """Tập 1 §Tầng 1: a dashboard *per role*.

        And the switcher **reorders** rather than hides, which is the whole design
        decision. The first version toggled `hidden` on panels, so a control made
        things disappear and a person could not tell whether a section was empty or
        merely switched off. Reordering keeps every panel reachable and keeps the
        sidebar identical, so navigation never changes underfoot.
        """
        page = (await client.get("/api/v1/ui")).text
        for role in ("CEO", "PM", "Approver"):
            assert f">{role}<" in page, f"no {role} button"
        js = _page(page)
        assert "applyRole" in js and "ROLES" in js
        assert "style.order" in js, (
            "the role switcher must reorder panels, not hide them: `order` reorders "
            "without moving a node, `hidden` is what made the page feel like it was "
            "hiding things"
        )
        assert ".style.display" not in js.split("function applyRole")[1][:600], (
            "applyRole must not set display or hidden -- that is the version that made "
            "navigation feel like it was hiding things from you"
        )

    async def test_the_dom_ids_the_script_reaches_for_all_exist(self, client: Any) -> None:
        """The check that catches a page of `undefined`.

        Every `$("id")` in the construction script must resolve to an element in the
        served document. The first version of this file only checked that ids existed,
        which passes even if the script asks for one that does not — so it is written
        the other way round: take the ids the script asks for and demand each one.
        """
        page = (await client.get("/api/v1/ui")).text
        js = _page(page)
        asked = set(re.findall(r"""\$\(["']([A-Za-z0-9_-]+)["']\)""", js))
        assert asked, "no ids found in the script, so this test is vacuous"
        missing = sorted(i for i in asked if f'id="{i}"' not in page)
        assert not missing, f"the script reads ids the page does not define: {missing}"

    async def test_the_script_is_valid_javascript(self, client: Any) -> None:
        """Parse it, rather than trust it.

        A page whose script does not parse serves a 200, renders a frame, and shows
        nothing — which is the failure mode that a substring test cannot see and that
        nobody notices until they open the URL.
        """
        import asyncio
        import functools
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed; the served-page tests still apply")

        js = _page((await client.get("/api/v1/ui")).text)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(js)
            path = handle.name

        # In a thread, because `subprocess.run` blocks and this is an async test sharing
        # a loop with everything else running. A 60ms block is not a problem; a block
        # in a loop with 2000 other tests is.
        run = functools.partial(
            subprocess.run,
            [node, "--check", path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        result = await asyncio.to_thread(run)
        assert result.returncode == 0, result.stderr[:600]


class TestYouCanAlwaysGetOut:
    """The complaint that started the rebuild, as tests.

    *"Khi click vào bất cứ dự án hay vị trí thì không thể thoát ra ngoài dễ dàng."* — click
    into any project or place and you cannot get back out.

    The first version of this page kept the selected project in a JavaScript variable.
    That is a dead end, and it is a dead end in three separate ways: there was no Back
    control, the browser's own Back button went to wherever the person had been before
    the *site* rather than before the project, and a refresh threw the selection away.
    So the three properties that fix it are asserted here rather than reviewed.
    """

    async def test_the_url_carries_the_selection(self, client: Any) -> None:
        """The URL is the state. Without this, Back cannot work."""
        js = _page((await client.get("/api/v1/ui")).text)
        assert "hashchange" in js, "the page does not listen for navigation"
        assert "location.hash = hash" in js, "navigation does not go through the URL"

    async def test_a_project_has_its_own_route(self, client: Any) -> None:
        """A shareable, linkable address, not a hidden variable."""
        js = _page((await client.get("/api/v1/ui")).text)
        assert "#/projects/" in js, "a project has no address of its own"

    async def test_escape_goes_back(self, client: Any) -> None:
        """One predictable way out, bound on the document so it works anywhere."""
        js = _page((await client.get("/api/v1/ui")).text)
        assert 'e.key !== "Escape"' in js
        assert "history.back()" in js, "Escape does not use history, so Back breaks"

    async def test_there_is_a_visible_back_control(self, client: Any) -> None:
        """And it is on the page, not something you have to know to press."""
        page = (await client.get("/api/v1/ui")).text
        assert 'id="backBtn"' in page
        assert "crumbs" in page and "deep" in page, (
            "the breadcrumb bar must be able to turn the Back control on, and it must "
            "be able to stay on: there is no state in which somebody is somewhere "
            "with no visible route out"
        )

    async def test_the_list_is_not_replaced_by_its_detail(self, client: Any) -> None:
        """The other half. Selecting a project must not remove the list."""
        page = (await client.get("/api/v1/ui")).text
        assert "split" in page, "there is no side-by-side layout for list and detail"
        js = _page(page)
        # The list is rendered by the same function that renders the detail, so a
        # detail can never appear without it.
        assert "renderProjects" in js
        assert re.search(r"async function renderProjects\([^)]*\)\s*\{", js)

    async def test_every_navigation_target_is_a_route(self, client: Any) -> None:
        """A nav link to a hash that no route handles is a dead page.

        Checked by parsing the routes out of the script and the links out of the
        markup, so adding a link without a route fails here rather than in a browser.
        """
        page = (await client.get("/api/v1/ui")).text
        js = _page(page)
        routes = set(re.findall(r"^  (\w+): \{ label:", js, re.M))
        assert routes, "no routes were parsed, so this test is vacuous"
        links = set(re.findall(r'href="#/([a-z]+)', page))
        assert links, "no navigation links were found"
        assert links <= routes, f"links with no route behind them: {sorted(links - routes)}"

    async def test_the_same_route_renders_from_one_function(self, client: Any) -> None:
        """`projects` and `projects/:id` are one view, so the list is always there.

        If they were two functions, one of them would eventually be edited without the
        other and the list would appear only on one of them.
        """
        js = _page((await client.get("/api/v1/ui")).text)
        switch = re.search(r"switch \(r\.name\) \{(.*?)\n  \}", js, re.S)
        assert switch is not None, "no route dispatch found"
        body = switch.group(1)
        assert re.search(r'case "projects":\s*case "project":', body) or (
            'case "projects":' in body and 'case "project":' in body
        ), "the project list and the project detail are dispatched separately"


class TestYouCanActuallyDoSomething:
    """The second complaint, also as tests: work could be *seen* but not *handled*."""

    async def test_the_approval_queue_has_buttons(self, client: Any) -> None:
        """An inbox with no approve button is a read-only list of somebody's work."""
        js = _page((await client.get("/api/v1/ui")).text)
        # The buttons are rendered with `data-do`, and the URL is assembled from that
        # value -- so `data-do` is the thing to assert, and the endpoint paths follow
        # from it. Asserting the literal strings `/approve` would have passed on a page
        # that rendered a button and wired it to nothing.
        for action in ("approve", "reject", "ask"):
            assert f'data-do="{action}"' in js, f"no {action} button is rendered"
        assert "request-information" in js, "the 'ask' action has no endpoint"

    async def test_the_kill_switch_is_a_control(self, client: Any) -> None:
        """Not a column somebody can read. A control somebody can pull."""
        js = _page((await client.get("/api/v1/ui")).text)
        assert "/kill" in js and "/revive" in js
        assert "data-kill=" in js, "no kill control is rendered"
        assert "reason" in js, "a kill with no reason is refused, so it must be asked for"

    async def test_an_action_can_fail_visibly(self, client: Any) -> None:
        """An action that fails silently is worse than one that is missing."""
        js = _page((await client.get("/api/v1/ui")).text)
        assert "toast(" in js, "nothing reports the outcome of an action"
        assert "Could not" in js, "a failure is never surfaced"

    async def test_a_reason_too_short_is_refused_in_the_page(self, client: Any) -> None:
        """Before the request, with an explanation of why."""
        js = _page((await client.get("/api/v1/ui")).text)
        assert "trim().length < 3" in js, (
            "the page sends the request and lets the 422 come back; the reason is "
            "required and saying so here is the difference between a rule and a "
            "surprise"
        )


class TestThePageCallsRealEndpoints:
    """Every path the page fetches must be one the server actually serves.

    Asserted against the served OpenAPI schema rather than a hand-kept list, so a
    renamed route breaks this test instead of producing a page that 404s in a browser.
    """

    async def test_every_fetched_path_is_in_the_schema(self, client: Any) -> None:
        page = (await client.get("/api/v1/ui")).text
        js = _page(page)
        paths = (await client.get("/openapi.json")).json()["paths"]

        # `apiGet("/projects/${...}/workspace")` — normalise the interpolations away.
        called = re.findall(r'apiGet\(\s*[`"\'](/[^`"\']*)', js)
        assert called, "the construction script fetches nothing"

        # The page calls `apiGet("/portfolio")` and `apiGet` prepends `API = "/api/v1"`,
        # so the schema path carries the prefix. Asserting against the raw call site
        # rejects all 7 of the 7 endpoints it means to check — a test that fails on
        # everything it is about is a test about itself, and it looks like a finding.
        prefix = re.search(r'const API = "([^"]+)"', js)
        assert prefix is not None, "the page has no API base"
        base = prefix.group(1)

        for path in called:
            # Normalise **both** sides. The page writes `${encodeURIComponent(id)}` and
            # the schema writes `{project_id}`, so comparing them literally rejects
            # every parameterised path while accepting every literal one — a test that
            # quietly checks only the half it can match.
            resolved = re.sub(r"\$\{[^}]*\}", "{x}", base + path)
            candidates = {re.sub(r"\{[^}]+\}", "{x}", p) for p in paths}
            assert resolved in candidates, (
                f"the page fetches {base + path!r}, which is not in the served schema"
            )

    async def test_it_fetches_the_three_role_surfaces(self, client: Any) -> None:
        js = _page((await client.get("/api/v1/ui")).text)
        for path in (
            "/portfolio",
            "/projects",
            "/workspace",
            "/wbs",
            "/activity",
            "/approvals/inbox",
        ):
            assert path in js, f"the page never fetches {path}"

    async def test_it_does_not_restate_a_number_the_server_owns(self, client: Any) -> None:
        """The backfill-limit defect, generalised.

        `test_task_api_and_ui.py` proves the page does not pin `?limit=60` for the
        stream. The same class of bug is a hard-coded page size for a construction
        list, so the construction fetches must pass limits the server defaults.
        """
        js = _page((await client.get("/api/v1/ui")).text)
        for call in re.findall(r"apiGet\((?:[^()]|\([^()]*\))*\)", js):
            if "?limit=" in call and "searchParams" not in call:
                assert "{ limit:" in call, f"a limit is pinned in the URL: {call}"

    async def test_the_token_is_never_in_a_url(self, client: Any) -> None:
        """A token in a query string lands in history, in `Referer`, and in every
        proxy log between the browser and here."""
        page = (await client.get("/api/v1/ui")).text
        assert "access_token" not in page
        assert "Authorization" in page

    async def test_the_tenant_header_is_always_sent(self, client: Any) -> None:
        """`authenticate` refuses a service call with no `x-organization-id` as
        `missing_org_header`, and the token check passes first, so the user is told
        their token was rejected when their tenant was the problem."""
        js = _page((await client.get("/api/v1/ui")).text)
        assert 'headers["x-organization-id"] = ORG' in js


class TestUnmeasuredIsNotOnTime:
    """The rule, asserted in the markup rather than in a comment."""

    async def test_the_unmeasured_state_has_its_own_shape(self, client: Any) -> None:
        page = (await client.get("/api/v1/ui")).text
        # A dashed outline and no fill, against a solid bar for a measurement.
        assert ".bar.unmeasured .track" in page
        assert ".bar.unmeasured .fillbar { display:none; }" in page, (
            "an unmeasured work package must render no bar at all: a bar is a width "
            "and a width is a measurement"
        )
        # The dashed border is asserted by *intent*, not by the exact bytes.
        #
        # The previous version asserted `"border:1px dashed"`, which is a **format**:
        # it fails on `border: 1px dashed` and passes on a rule that never applies,
        # because a substring does not know which selector it belongs to. So the rule
        # is read, and the declaration is looked for inside it. The stronger check is
        # `make verify-page`, which renders the bars against the real corpus and counts
        # them -- a shape assertion cannot tell a rule that is present from one that
        # wins.
        rule = re.search(r"\.bar\.unmeasured \.track\s*\{([^}]*)\}", page)
        assert rule is not None, "there is no .bar.unmeasured .track rule"
        assert re.search(r"border[^;]*dashed", rule.group(1)), (
            f"the unmeasured track has no dashed border: {rule.group(1)[:120]}"
        )

    async def test_the_three_states_are_distinguished(self, client: Any) -> None:
        page = (await client.get("/api/v1/ui")).text
        for selector in (".bar.late .fillbar", ".bar.unmeasured .track"):
            assert selector in page, f"{selector} is missing: the states are not distinct"

    async def test_the_page_reads_actual_updated(self, client: Any) -> None:
        """The flag is what knows a copied planned date from a recorded one, and
        100% of the real corpus's actual columns are copies."""
        js = _page((await client.get("/api/v1/ui")).text)
        assert "actual_updated" in js, (
            "the page renders actual dates without reading the flag that says whether "
            "they were recorded"
        )

    async def test_the_page_reads_is_measured_and_not_just_the_date(self, client: Any) -> None:
        js = _page((await client.get("/api/v1/ui")).text)
        assert "is_measured" in js

    async def test_a_zone_is_never_summarised_to_a_zero(self, client: Any) -> None:
        """The legend states the distinction where the bars are, not in a comment.

        A reader looking at a wall of hatched bars needs told what hatching means
        without clicking anything.
        """
        page = (await client.get("/api/v1/ui")).text
        assert "not measured" in page
        assert "An empty actual column is not a zero variance." in page

    async def test_the_above_ceiling_control_is_on_the_page(self, client: Any) -> None:
        """`above_l1` is a control, not a description, and it is expected to be 0.

        It gets an alert treatment when it is not, so a widened delegation is visible
        without anybody reading a number.
        """
        js = _page((await client.get("/api/v1/ui")).text)
        assert "above_l1" in js
        assert "Above ceiling" in js


class TestThePageEscapesWhatItRenders:
    async def test_no_untrusted_string_reaches_inner_html_raw(self, client: Any) -> None:
        """Project names and work descriptions are corpus strings and all of them
        contain characters that mean something in HTML.

        Every corpus value in the construction script goes through `esc`. The check
        is for the pattern, not a proof of every path, and it is paired with
        `test_the_script_is_valid_javascript` so a parse error cannot quietly turn
        this into a test that passes because nothing ran.
        """
        js = _page((await client.get("/api/v1/ui")).text)
        construction = js.split("The construction product")[-1]

        # The fields that come out of the corpus, named explicitly. A blanket "every
        # interpolation is escaped" rule is wrong in both directions: it flags a
        # computed bar width, and it would be satisfied by one `esc(` anywhere in the
        # block. So the list is the field names, and the assertion is that each appears
        # in the document only inside an `esc(`.
        #
        # `pct` is deliberately absent: it is `Math.round` of two integers divided by
        # another, and it is the one interpolation here that cannot carry a string.
        for field in (
            "p.name",
            "p.code",
            "p.address",
            "p.status",
            "z.name",
            "z.code",
            "r.work_description",
            "r.section_label",
            "a.action_type",
            "a.requested_by",
            "a.assigned_approver_id",
        ):
            for use in re.findall(rf"\$\{{([^}}]*?\b{re.escape(field)}\b[^}}]*?)\}}", construction):
                assert "esc(" in use, f"{field} reaches innerHTML without esc(): ${{{use}}}"

    async def test_the_escape_helper_covers_the_five_characters_that_matter(
        self, client: Any
    ) -> None:
        js = _page((await client.get("/api/v1/ui")).text)
        body = re.search(r"function esc\(s\)\s*\{(.*?)\n\}", js, re.S)
        assert body is not None, "no esc() helper"
        for ch, entity in (
            ("&", "&amp;"),
            ("<", "&lt;"),
            (">", "&gt;"),
            ('"', "&quot;"),
            ("'", "&#39;"),
        ):
            assert ch in body.group(1) and entity in body.group(1), f"esc misses {ch!r}"


class TestTheServerStillOwnsItsPosture:
    async def test_the_auth_off_banner_comes_from_the_server(self, client: Any) -> None:
        """The page must not decide whether authentication is on.

        If it did, a future refactor that moved the flag would be invisible here, and
        a page claiming a session is authenticated is the one thing this platform
        exists to prevent.
        """
        js = _page((await client.get("/api/v1/ui")).text)
        assert "AUTH_OFF" in js
        assert "if (AUTH_OFF)" in js

    async def test_the_build_stamp_is_present(self, client: Any) -> None:
        """If a fix does not appear, the page has not changed — and the number says
        so. The alternative is debugging the browser's cache."""
        page = (await client.get("/api/v1/ui")).text
        assert 'id="build"' in page
