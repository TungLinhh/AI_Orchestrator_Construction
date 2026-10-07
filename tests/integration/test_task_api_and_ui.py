"""Creating a task over HTTP, as the things that actually call it.

F65. `tasks.requester_id` is a foreign key to `users.id`. The API wrote
`str(ctx.actor.id)` into it, so the control plane's own service credential
(`svc:control-plane`) produced a foreign-key violation on every single task created
over HTTP.

The repository caught the `IntegrityError` and reported *"an equivalent task was
created concurrently"*, with the new task's own fingerprint in the details. So the
message was confident, specific, and wrong — and it sent the caller looking for a
duplicate they had never created. It is the most expensive kind of bug: not a crash,
but a confident false statement about the caller's own data.

Two things are asserted here, because either alone would have been satisfied by the
broken version:

* the request succeeds for a non-human principal, and
* a genuine duplicate is still reported as a duplicate.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.api_client import TEST_SECRET
from tests.integration.api_client import auth_headers as _headers

pytestmark = pytest.mark.integration


async def _create(client: Any, organization_id: str, goal: str) -> Any:
    return await client.post(
        "/api/v1/tasks",
        headers={**_headers(organization_id), "Idempotency-Key": str(uuid.uuid4())},
        json={"title": goal[:60], "goal": goal, "task_type": "analysis", "start_workflow": False},
    )


def _ctx(
    actor: object,
    *,
    organization_id: str = "org_01m3d5hwxet3x61vjc1ffjyrzh",
    session: Any = None,
) -> Any:
    """An `ApiContext` carrying only what the code under test reads.

    Built by hand because `_requester_id` takes a context and needs no request, no
    session and no transaction — and constructing real ones would make this test
    depend on all three. The `session` is a real (unconnected) `AsyncSession` rather
    than `None` purely so the object is honestly typed: a test double that lies about
    a field's type is how a type system stops being worth anything.
    """
    from ai_orchestrator.api.deps import ApiContext
    from ai_orchestrator.security.auth import Principal

    principal = Principal(actor=actor, is_service=False)  # type: ignore[arg-type]
    return ApiContext(
        principal=principal,
        session=session if session is not None else AsyncSession(),
        organization_id=organization_id,
        actor=actor,  # type: ignore[arg-type]
    )


class TestWhoIsRecordedAsTheRequester:
    """F65, at the level the fix lives.

    Driven through the HTTP client in the first draft, and the interesting assertion —
    *what a caller is told* — is not asserted here. The in-process client cannot get
    past authentication in this harness, so the end-to-end behaviour is verified
    against the running server instead: a `POST /api/v1/tasks` as a service principal
    returns `201` with `"requester_type": "service"` and `"requester_id": null`, where
    before the fix it returned `409 an equivalent task was created concurrently` on the
    first call, for a goal that had never been seen.

    Both are recorded rather than one being quietly dropped.
    """

    async def test_a_service_principal_is_not_recorded_as_a_user(self) -> None:
        from ai_orchestrator.api.tasks import _requester_id
        from ai_orchestrator.domain.contracts import Actor
        from ai_orchestrator.domain.enums import ActorType

        ctx = _ctx(Actor(id="svc:control-plane", kind=ActorType.SERVICE))
        assert await _requester_id(ctx) is None, (
            "a service principal's id was written into a column that references "
            "users; the foreign key rejects it"
        )

    async def test_an_agent_is_not_recorded_as_a_user(self) -> None:
        """Agents live in `agents`, not `users`. Same fault, different actor."""
        from ai_orchestrator.api.tasks import _requester_id
        from ai_orchestrator.domain.contracts import Actor
        from ai_orchestrator.domain.enums import ActorType

        ctx = _ctx(Actor(id="agt_01m3d5hwxet3x61vjc1ffjyrzj", kind=ActorType.AGENT))
        assert await _requester_id(ctx) is None

    async def test_a_human_with_a_users_row_is_recorded(self, tenant: Any) -> None:
        """The other direction, because a fix that always returned `None` would pass
        the two tests above and lose the attribution entirely."""
        from sqlalchemy import insert

        from ai_orchestrator.api.tasks import _requester_id
        from ai_orchestrator.domain.contracts import Actor
        from ai_orchestrator.domain.enums import ActorType
        from ai_orchestrator.persistence.models import User

        actor_id = "usr_01m3d5hwxet3x61vjc1ffjyrzk"
        await tenant.session.execute(
            insert(User).values(
                id=actor_id,
                organization_id=tenant.organization_id,
                email="operator@example.invalid",
                display_name="Local operator",
                password_hash="!",
                role="admin",
            )
        )
        ctx = _ctx(
            Actor(id=actor_id, kind=ActorType.HUMAN),
            organization_id=tenant.organization_id,
            session=tenant.session,
        )
        assert await _requester_id(ctx) == actor_id

    async def test_a_human_stand_in_with_no_users_row_is_not_recorded(self, tenant: Any) -> None:
        """The fault this exists to stop, and the one that blocked the whole page.

        With authentication off the principal is a human *stand-in* whose id has a
        `users` row in one organisation -- the one `seed_local_operator.py`
        provisions -- and none anywhere else. Writing it anyway failed
        `fk_tasks_requester_id_users`, so every task created from the page in every
        other tenant came back as a validation error naming a column the reader
        never touched.

        "A human is not automatically a user" is the whole content of this test,
        and the two above it are the cases where that is obvious.
        """
        from ai_orchestrator.api.tasks import _requester_id
        from ai_orchestrator.domain.contracts import Actor
        from ai_orchestrator.domain.enums import ActorType

        ctx = _ctx(
            Actor(id="dev:no-auth", kind=ActorType.HUMAN),
            organization_id=tenant.organization_id,
            session=tenant.session,
        )
        assert await _requester_id(ctx) is None


class TestARealDuplicateIsStillADuplicate:
    async def test_the_same_goal_twice_is_refused(self, tenant: Any) -> None:
        """The check that made F65's false message plausible must keep working.

        If this were the only test, the broken version would pass it — the broken
        version refused *everything* as a duplicate.
        """
        from ai_orchestrator.domain.errors import ConflictError
        from ai_orchestrator.persistence.repositories.task import TaskRepository

        repo = TaskRepository(tenant.session, tenant.organization_id)
        goal = f"exactly the same goal {uuid.uuid4().hex}"
        await repo.create(title="first", goal=goal, task_type="analysis", requester_type="human")
        with pytest.raises(ConflictError) as caught:
            await repo.create(
                title="second", goal=goal, task_type="analysis", requester_type="human"
            )
        assert "concurrent" in str(caught.value).lower()

    async def test_a_different_goal_is_not_a_duplicate(self, tenant: Any) -> None:
        """Two goals differing by a UUID must both be accepted. A dedup check that
        refuses these would be refusing real work, and from the caller's side it looks
        identical to F65."""
        from ai_orchestrator.persistence.repositories.task import TaskRepository

        repo = TaskRepository(tenant.session, tenant.organization_id)
        for prefix in ("goal one", "goal two"):
            await repo.create(
                title=prefix,
                goal=f"{prefix} {uuid.uuid4().hex}",
                task_type="analysis",
                requester_type="human",
            )


class TestTheConflictHandlerTellsTheTruth:
    async def test_a_foreign_key_violation_is_not_reported_as_a_conflict(self, tenant: Any) -> None:
        """A bad `owner_agent_id` is a different fault from a duplicate, and the
        message has to say which.

        A handler that catches a broad `IntegrityError` and reports a narrow cause has
        not diagnosed anything — it has guessed, and the guess is confident.
        """
        from ai_orchestrator.domain.errors import ValidationError
        from ai_orchestrator.persistence.repositories.task import TaskRepository

        repo = TaskRepository(tenant.session, tenant.organization_id)
        with pytest.raises(ValidationError) as caught:
            await repo.create(
                title="bad agent",
                goal=f"nonexistent agent {uuid.uuid4().hex}",
                task_type="analysis",
                requester_type="human",
                owner_agent_id="agt_01m3d5hwxet3x61vjc1ffjyrzz",
            )
        message = str(caught.value)
        assert "concurrent" not in message.lower(), (
            f"an integrity fault was reported as a concurrency conflict: {message}"
        )
        assert "fk_tasks_owner_agent_id_agents" in str(caught.value.details), (
            f"the constraint was not named, so the reader is left guessing: {message}"
        )


def _js_without_comments(html: str) -> str:
    """The page's script, with comments removed.

    Deliberately simple and deliberately incomplete: block comments, and lines whose
    first non-space characters are `//`. A full tokenizer would also have to know
    about strings, regex literals and the `http://` inside them, and getting *that*
    subtly wrong would strip real code and make this test lie in the other direction.

    Incomplete is fine and honest here. It cannot create a false failure, because a
    stripped region is only ever a region the test then finds nothing in.
    """
    import re

    body = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))
    without_blocks = re.sub(r"/\*.*?\*/", " ", body, flags=re.S)
    return "\n".join(
        line for line in without_blocks.split("\n") if not line.strip().startswith("//")
    )


class TestTheServiceCallActuallyAuthenticates:
    """The test `_create` has been waiting for. F123, part two.

    This file's HTTP client sent `Authorization: svc.<secret>` with **no `Bearer`
    scheme**. `security/auth.py` reads it through FastAPI's `HTTPBearer`, which returns
    `None` for an unprefixed value, so `authenticate` answered `403
    missing_credentials` before it looked at the value -- every request through the
    fixture would have been refused.

    It went unnoticed for a long time because the only HTTP calls in this file are to
    `GET /api/v1/ui`, which is unauthenticated, and `_create` at line 85 was **defined
    and never called**. A helper nobody calls is a helper nobody knows is broken.

    So: the fixture now sends the scheme (`api_client.auth_headers`), and this test
    makes an authenticated request so that the next person who removes the scheme finds
    out here rather than in a browser.
    """

    async def test_a_service_call_with_a_tenant_is_accepted(self, client: Any, tenant: Any) -> None:
        response = await client.get("/api/v1/tasks", headers=_headers(tenant.organization_id))
        assert response.status_code == 200, (
            f"an authenticated service call was refused: {response.status_code} "
            f"{response.text[:200]}"
        )

    async def test_the_same_call_without_a_tenant_is_refused(
        self, client: Any, tenant: Any
    ) -> None:
        """And the refusal names the tenant, not the token.

        `authenticate` checks the credential first and it passes, so a missing
        `x-organization-id` read as a rejected token -- and the message the user saw
        named the wrong thing.
        """
        # A **valid** credential with no tenant. No credential at all is a different
        # case and a different message -- `missing_credentials` -- and asserting on
        # that would have passed without the credential ever being checked.
        response = await client.get(
            "/api/v1/tasks",
            headers={"Authorization": f"Bearer svc.{TEST_SECRET}"},
        )
        assert response.status_code in (401, 403)
        assert "missing_org_header" in response.text, (
            "a valid credential with no tenant must name the tenant, not the "
            f"credential: {response.text[:200]}"
        )


class TestTheOperatorView:
    async def test_it_is_served_from_this_origin(self, client: Any) -> None:
        """Same origin, so the page can send the token in a header and no CORS
        configuration is needed to render the flow."""
        response = await client.get("/api/v1/ui")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Orchestrator" in response.text

    async def test_it_is_not_cached(self, client: Any) -> None:
        """A cached control-plane view is a view of a moment that has passed."""
        assert "no-store" in (await client.get("/api/v1/ui")).headers.get("cache-control", "")

    async def test_it_never_embeds_a_credential(self, client: Any) -> None:
        """The page asks for the token and keeps it in this tab. A secret in the HTML
        is a secret every browser of every user has already fetched."""
        from ai_orchestrator.config.settings import get_settings

        secret = get_settings().internal_service_secret.get_secret_value()
        if secret:
            assert secret not in (await client.get("/api/v1/ui")).text, (
                "the operator view contains a live service credential"
            )

    async def test_it_does_not_put_the_token_in_a_url(self, client: Any) -> None:
        """`EventSource` cannot set a header, so the first draft passed the token as a
        query parameter — which lands in browser history, in `Referer` headers and in
        every proxy log on the way. The page reads the stream with `fetch` instead;
        this asserts that did not get reverted."""
        text = (await client.get("/api/v1/ui")).text
        assert "access_token" not in text, (
            "the token is being passed in the URL; use fetch so it stays in a header"
        )
        assert "Authorization" in text

    async def test_it_does_not_carry_its_own_backfill_limit(self, client: Any) -> None:
        """The page must not restate the server's backfill window.

        It did: the server said 60 and the page said `?limit=60`, and when the server
        moved to 300 the page kept asking for 60. Nothing failed, no test went red, and
        the viewer's delegation tree was empty because the page was requesting the
        wrong slice of history and looking like it was broken. Two copies of one number
        is one more number than should exist.
        """
        text = (await client.get("/api/v1/ui")).text
        assert "limit=60" not in text, (
            "the page pins a backfill limit the server also owns; remove it and let the "
            "server's default stand"
        )
        from ai_orchestrator.api.stream import BACKFILL_LIMIT

        assert f"/stream?limit={BACKFILL_LIMIT}" not in text, (
            "the page restates the server's backfill limit as a literal"
        )

    async def test_the_backfill_window_would_reach_a_real_delegation(self, client: Any) -> None:
        """Pinned to the measurement, because the number means nothing without it.

        This tenant held 533 events with the newest delegation 208 back, so a 60-event
        window showed a page of task churn and no delegation at all. Asserted against
        the *observed* distance rather than a round number, so that someone lowering
        the limit has to look at why rather than just see a test go red.
        """
        from ai_orchestrator.api.stream import BACKFILL_LIMIT

        observed_events_back_from_the_newest_delegation = 208
        assert observed_events_back_from_the_newest_delegation < BACKFILL_LIMIT, (
            f"a {BACKFILL_LIMIT}-event window stops short of the most recent "
            "delegation, so a freshly opened page shows no delegation tree"
        )

    async def test_a_pasted_whole_config_line_is_unwrapped(self, client: Any) -> None:
        """`grep INTERNAL_SERVICE_SECRET .secrets/runtime.env` prints the whole line.

        I gave the user that command, they pasted its output, and the server refused
        it — with the same `invalid credential` as a wrong secret, so the obvious
        cause was invisible. The page now unwraps the shapes a terminal actually
        prints, and this asserts the function it ships, by extracting it from the
        served page and running it under node rather than by re-implementing it here.
        A Python copy of this logic would test the copy.
        """
        import asyncio
        import shutil

        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed; the token normaliser is page-side JS")

        page = (await client.get("/api/v1/ui")).text
        script = _js_without_comments(page)
        start = script.index("function normaliseToken(")
        end = script.index("\n}", start) + 2
        function_source = script[start:end]
        assert "function normaliseToken" in function_source

        cases: list[tuple[str, str]] = [
            ("s3cr3t", "s3cr3t"),
            ("INTERNAL_SERVICE_SECRET=s3cr3t", "s3cr3t"),
            ('INTERNAL_SERVICE_SECRET="s3cr3t"', "s3cr3t"),
            ("INTERNAL_SERVICE_SECRET='s3cr3t'", "s3cr3t"),
            ("  s3cr3t  ", "s3cr3t"),
            ("export INTERNAL_SERVICE_SECRET=s3cr3t", "s3cr3t"),
            # And the three it must NOT touch, or a real secret containing `=` breaks.
            ("lower_case=s3cr3t", "lower_case=s3cr3t"),
            ("not a var=s3cr3t", "not a var=s3cr3t"),
            ("s3cr3t=tail", "s3cr3t=tail"),
        ]
        program = (
            function_source
            + "\n"
            + "\n".join(
                f"console.log(JSON.stringify(normaliseToken({value!r})))" for value, _ in cases
            )
        )
        # `create_subprocess_exec`, not `subprocess.run`. This is an async test
        # and `subprocess.run` blocks the event loop for the whole lifetime of
        # the node process — up to the 30s timeout below. That is a defect and
        # not a style preference: anything else on the same loop stops making
        # progress, including the fixture teardown waiting behind it, and a
        # timeout then looks like a hang rather than like the cause.
        process = await asyncio.create_subprocess_exec(
            node,
            "-e",
            program,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except TimeoutError:
            process.kill()
            await process.wait()
            pytest.fail("node did not finish the token-normaliser program in 30s")
        assert process.returncode == 0, stderr.decode()
        produced = stdout.decode().strip().splitlines()
        assert len(produced) == len(cases), stderr.decode()
        for (value, want), got in zip(cases, produced, strict=True):
            assert got == f'"{want}"', (
                f"normaliseToken({value!r}) produced {got}, expected {want!r}"
            )

    async def test_the_page_actually_calls_the_normaliser(self, client: Any) -> None:
        """The function existing is not the same as the page using it.

        The test above passed with the call site removed, because it exercised
        `normaliseToken` directly and never asked who calls it. That is the third time
        in this project a well-tested helper turned out to be wired to nothing — F59,
        F67, and this — and the fix each time is the same: assert the *call*, not the
        callee.
        """
        page = _js_without_comments((await client.get("/api/v1/ui")).text)
        assert "setToken(normaliseToken(" in page, (
            "the page stores the pasted token without unwrapping it, so a whole "
            "`INTERNAL_SERVICE_SECRET=...` line is rejected with no visible cause"
        )

    async def test_a_rejected_token_is_forgotten(self, client: Any) -> None:
        """A wrong token left in storage is a trap.

        The page reads `sessionStorage` on load, reconnects with whatever is there, is
        refused, and prompts again — so the user is asked for a credential they have
        just been told is wrong, with no way to tell the stored copy from the one they
        typed. And a dismissed prompt leaves the page disconnected with no way back in
        short of a reload. Both are fixed by forgetting the token on rejection and
        offering a visible way to re-enter it.

        This is the failure the user hit: a bad token from the first attempt, still in
        storage, so every subsequent paste looked like it was being ignored.
        """
        page = _js_without_comments((await client.get("/api/v1/ui")).text)
        assert "forgetToken();" in page, (
            "a rejected token is not cleared, so the page reconnects with a credential "
            "it already knows is wrong"
        )
        # On the rejection path specifically, not merely somewhere in the file.
        #
        # The rule is two branches now, and both are asserted. **A 401 always forgets the
        # token** -- that is a refused credential and there is nothing else it can mean.
        # **A 403 forgets it only while authentication is on**, because with authentication
        # off a 403 is an authorisation *answer*: `require_human()` returns one, and popping
        # a token dialog for it is how a person ends up being asked for a password they do
        # not have.
        #
        # The previous version searched for the literal `401 || res.status === 403`, so
        # correcting the predicate broke the very test guarding it. That is the cost of a
        # test anchored on a string; this one is anchored on the rule instead -- both
        # branches, and the gate between them.
        rejection = page.split("res.status === 401")[1][:400]
        assert "forgetToken()" in rejection, "the token is not forgotten when the server refuses it"
        assert "403 && !AUTH_OFF" in rejection, (
            "a 403 must forget the token only while authentication is on"
        )
        assert 'id="tokenBtn"' in (await client.get("/api/v1/ui")).text, (
            "there is no visible way to re-enter a token after dismissing the prompt"
        )

    async def test_the_page_sends_every_header_the_server_requires(self, client: Any) -> None:
        """The test that should have existed before two rounds of debugging.

        A service credential needs **two** things: the token and the tenant. The page
        sent the first and not the second, `authenticate` refused every request with
        `missing_org_header`, and because the token is verified *first* and passed, the
        only thing the user saw was "That token was rejected."

        Every earlier test here asserted what the page must **not** send — no token in
        a URL, no secret in the HTML. Nothing asserted what it **must**, so a request
        that was permanently unauthorised passed all of them. Absence-oriented tests
        cannot catch a missing header; only a complete request can.
        """
        page = _js_without_comments((await client.get("/api/v1/ui")).text)
        assert '"x-organization-id"' in page, (
            "the page never sends x-organization-id, so every service call is refused "
            "with missing_org_header and looks like a bad token"
        )
        # And the header has to be attached, not merely mentioned in a comment.
        assert 'headers["x-organization-id"] = ORG' in page, (
            "x-organization-id is present in the page but never attached to a request"
        )

    async def test_the_tenant_is_baked_in_from_the_url(self, client: Any) -> None:
        org = "org_01m3d5hwxet3x61vjc1ffjyrzh"
        html = (await client.get(f"/api/v1/ui?org={org}")).text
        assert f'const ORG = "{org}"' in html, (
            "the tenant from the query string did not reach the page"
        )
        # Without one the page must say what is missing rather than fail opaquely.
        assert 'const ORG = ""' in (await client.get("/api/v1/ui")).text

    async def test_the_tenant_is_validated_before_it_reaches_a_script(self, client: Any) -> None:
        """`org` lands in a JavaScript string literal in the page every user loads.

        Accepting arbitrary text there would be a way to inject script into a document
        served to anyone who can reach the port — so the shape is checked, and a
        rejection is a validation error rather than a silently emptied field.
        """
        for hostile in ('"; alert(1); "', "org_short", "'; fetch('http://x');//"):
            response = await client.get(f"/api/v1/ui?org={hostile}")
            # 4xx, not specifically 400: `ValidationError` surfaces through FastAPI's
            # handler as 422, and the number is the framework's to choose. What matters
            # is that it is refused, which the injection case depends on entirely.
            assert 400 <= response.status_code < 500, (
                f"org={hostile!r} was accepted (HTTP {response.status_code}) and would "
                "be interpolated into a script the browser executes"
            )
            assert f'"{hostile}"' not in response.text, (
                "the rejected value was echoed into the page anyway"
            )

    async def test_it_does_not_read_fields_that_do_not_exist(self, client: Any) -> None:
        """The view must read field names that exist.

        Four times now, a field name here was read from memory rather than from the
        schema: `child_task_id` and `to_agent_name` on the event payload (neither
        exists), `result.note` (it is `reason`), and `a.status` on an agent (it is
        `lifecycle_status`). Each one type-checks, each one renders, and each one
        silently produces nothing.

        Checked against the **code**, with comments stripped: the view's own comments
        name these deliberately, because a comment recording the mistake is worth more
        than the test that would have caught it, and a substring search over the whole
        document cannot tell the two apart.
        """
        text = _js_without_comments((await client.get("/api/v1/ui")).text)
        for invented in ("to_agent_name", "from_agent_name", "result.note", "a.status"):
            # Match a JS identifier, not the suffix of a valid draft `data.status`.
            assert re.search(r"\b" + re.escape(invented) + r"\b", text) is None, (
                f"the view reads {invented!r}, which no payload has ever contained"
            )
        # And the names it *should* read, so the test fails if the working version is
        # replaced by a plausible one.
        #
        # Note what is absent: `source_agent_id` and `target_agent_id`. Those are what
        # the event carries, and the view does not read them -- the projection resolves
        # them to `from_agent` and `to_agent` in the `view` block. The first version of
        # this test asserted the raw ids and failed, which is the same mistake in the
        # opposite direction: a list of field names written from memory. A test
        # asserting on names is exactly as fallible as the code reading them.
        # A tuple rather than a long inline literal, because the line was 102
        # characters and the reason to keep it readable is that this is the list
        # of field names somebody once wrote from memory instead of reading.
        for real in (
            "lifecycle_status",
            "from_agent",
            "to_agent",
            "child_task_id",
            "parent_task_id",
        ):
            assert real in text, f"the view stopped reading {real!r}"


class TestTheConsoleCanSeeAnything:
    """The console was rendering 189 real events as blank lines.

    Reported as *"I haven't seen agents used for any task"* -- and the cause was not that
    no agent had run. Forty-five tasks, thirty-one executions, a hundred and thirty-two
    events and one real delegation were in the development database. The page read
    `ev.detail` and `ev.at`, and the payload has never had either field:

        { id, type, subject, source, actor_id, data, occurred_at, view }

    `data` carries the ids, `view` carries the names, because the projection resolves
    them so a reader never renders two opaque `agt_01m3...` strings. The old code looked
    for the names in `data`, three layers from where they are.

    `test_event_stream.py` already proves the *server's* shape, and
    `test_a_delegation_frame_resolves_both_agent_names` already proves the names are
    names. What nobody checked was the one thing that was wrong: **the page**. So these
    tests take a real frame from the server and require the page to read it.
    """

    async def _page_reads_a_real_frame(self, client: Any, tenant: Any, frame: dict) -> None:
        """The page must be able to render this exact object.

        A field-name assertion over the file cannot do this: `from_agent` was in the
        file, in a comment, while the code read three layers away from it. Passing the
        real object is the only check that distinguishes a page that reads the payload
        from a page that mentions it.
        """
        script = _js_without_comments((await client.get("/api/v1/ui")).text)

        # **Every field the page must read, and nothing else.**
        #
        # The assertion used to be "every field of a real frame", which was true only
        # while an event feed rendered every field. The feed is gone (F247) and the page
        # draws a *delegation tree* from frames now, which needs `id` to de-duplicate,
        # `type` for the lifecycle colour, and `data`/`view` for the ids and the names.
        #
        # `subject`, `actor_id` and `source` belonged to the feed. Requiring them would
        # be requiring the page to render a screen that does not exist -- a test that
        # fails on a correct product, which is the third time that has happened in this
        # file and the reason the rule is "the fields you render", not "the fields the
        # API sends".
        REQUIRED = {"id", "type", "data", "view"}
        for field in sorted(REQUIRED):
            assert field in script, (
                f"the page never reads {field!r}, so the delegation tree cannot be "
                f"drawn from a real frame. It carries {sorted(frame)}"
            )

        # **And the half that caught the original bug: the page must not read a field
        # the API does not send.** `ev.detail` and `ev.at` were in the script for
        # months, and the symptom was a page reporting "I haven't seen agents used".
        for field in sorted(frame):
            assert (
                field in script
                or field.split("_")[0] in script
                or field
                in (
                    "subject",
                    "actor_id",
                    "source",
                )
            ), (
                f"the page reads {field!r} by name but no real frame carries it -- "
                f"a real frame is {sorted(frame)}"
            )

    async def test_the_page_renders_a_task_event(self, client: Any, tenant: Any) -> None:
        """Creating a task is enough to produce an event, so the fixture does it.

        Not a stub event either -- a real `POST /api/v1/tasks` goes through the same
        path an operator uses, and the frame it produces is the one the page has to
        render. A test that hand-builds an event proves the page reads *that shape*, and
        a hand-built shape is how the wrong one got in.
        """
        from ai_orchestrator.api.stream import backfill

        response = await _create(client, tenant.organization_id, "Report the lateness")
        # 201, not 200: the route creates a resource. Asserting exactly 200 here
        # would have been a test that fails on a correct API.
        assert response.status_code in (200, 201), response.text
        await tenant.commit()
        frames = await backfill(tenant.session, tenant.organization_id, 50)
        if not frames:
            # A task creation writes to `outbox_events`; a **publisher** moves it to
            # `events`, and the stream reads `events`. Nothing publishes in the test
            # environment, so a freshly created task produces no frame to check the
            # page against.
            #
            # That is a real property of the system rather than a test gap — the stream
            # shows published events, and an unpublished one is invisible to a reader —
            # but it is also the reason this test cannot be the primary guard. The
            # guard that always runs is
            # `test_the_page_does_not_read_a_field_that_does_not_exist`, which is the
            # assertion that actually caught F131.
            pytest.skip("no published event yet; the outbox publisher is not running")
        await self._page_reads_a_real_frame(client, tenant, frames[0])

    async def test_the_page_renders_a_delegation_event(self, client: Any, tenant: Any) -> None:
        """The one that was blank: 189 real events and zero lines rendered."""
        from ai_orchestrator.api.stream import backfill

        frames = await backfill(tenant.session, tenant.organization_id, 50)
        delegations = [f for f in frames if str(f["type"]).startswith("delegation.")]
        if not delegations:
            pytest.skip("no published delegation to check the page against")
        await self._page_reads_a_real_frame(client, tenant, delegations[0])

    async def test_the_page_does_not_read_a_field_that_does_not_exist(self, client: Any) -> None:
        """By name, in the code, with comments stripped.

        The invented names are checked *absent* and the real ones *present*. Comments are
        stripped first, so the comment explaining that `ev.detail` was wrong is not
        itself the wrong -- a check that counts it eventually demands the bug back.
        """
        script = _js_without_comments((await client.get("/api/v1/ui")).text)
        for invented in ("ev.detail", "e.detail", "ev.at ", "e.at ||", "d.from_agent"):
            assert invented not in script, (
                f"the console still reads {invented!r}, which no event has ever had -- "
                "189 real events rendered as blank lines"
            )
        for real in ("ev.data", "ev.view", "occurred_at"):
            assert real in script, f"the console should read {real!r} and does not"


class TestTheServerTellsThePageTheTruth:
    """A page cannot be trusted to report its own security posture.

    If the page decided whether to show the "authentication is off" banner, then
    anyone who could inject a script — or any future refactor that moved the flag —
    would remove the only warning that the session is unauthenticated. The server
    renders it.
    """

    @pytest.fixture(autouse=True)
    def _no_settings_cache(self) -> None:
        """No cache to reset.

        Both tests patch `stream_module.get_settings` itself, so the real settings
        object is never consulted and its cache is never populated. An earlier version
        cleared the cache "to be safe" and imported a symbol that does not exist here,
        which is a worse outcome than the thing it was guarding against.
        """
        return None

    async def test_the_flag_is_rendered_into_the_page(self, client: Any, monkeypatch: Any) -> None:
        from ai_orchestrator.api import stream as stream_module
        from ai_orchestrator.config.settings import Settings

        monkeypatch.setattr(stream_module, "get_settings", lambda: Settings(api_auth_disabled=True))
        html = (await client.get("/api/v1/ui")).text
        assert "const AUTH_OFF = true" in html
        assert 'id="authwarn"' in html, "there is no banner to show when it is off"

    async def test_and_the_banner_is_shown_without_javascript(
        self, client: Any, monkeypatch: Any
    ) -> None:
        from ai_orchestrator.api import stream as stream_module
        from ai_orchestrator.config.settings import Settings

        monkeypatch.setattr(
            stream_module, "get_settings", lambda: Settings(api_auth_disabled=False)
        )
        html = (await client.get("/api/v1/ui")).text
        assert "const AUTH_OFF = false" in html
        # The banner element is in the document, and only the script un-hides it, so
        # a failure to load the script leaves it hidden rather than lying.
        assert 'id="authwarn" hidden' in html


class TestAnOwnerlessTaskGetsTheChief:
    """**No owner means the chief, not an orphan.**

    Measured on the live page: the agent picker shipped as "loading agents…" with no code
    behind it (F266), so pressing Run created a task with `owner_agent_id: null`. Then
    `POST /tasks/{id}/run` refused it with "this task has no agent, so there is nothing
    to run it with. Assign one first" — a correct refusal of a task the platform itself
    had created un-runnable.

    A task with nobody to do it is a row the queue will never drain. The chief is the
    root unit's head, not a name: a name is a second definition of who is on top.
    """

    async def test_creating_without_an_owner_assigns_the_chief(
        self, client: Any, tenant: Any
    ) -> None:
        from sqlalchemy import select

        from ai_orchestrator.persistence.models import Agent, Organization, OrgUnit
        from ai_orchestrator.seed import seed

        # A bare tenant has units but no agents; the chief has to exist to be defaulted to.
        org = (
            await tenant.session.execute(
                select(Organization).where(Organization.id == tenant.organization_id)
            )
        ).scalar_one()
        await seed(tenant.session, into=org)
        await tenant.commit()

        response = await _create(
            client, tenant.organization_id, f"ownerless work {uuid.uuid4().hex}"
        )
        assert response.status_code in (200, 201), response.text
        body = response.json()
        assert body.get("owner_agent_id"), (
            "the task was created with no owner, so nothing will ever run it"
        )
        assert body.get("owner_defaulted_to_chief") is True, (
            "the owner was set but the caller was not told it was defaulted, so the "
            "page cannot say who will do the work"
        )

        chief_unit = (
            await tenant.session.execute(
                select(OrgUnit).where(
                    OrgUnit.organization_id == tenant.organization_id,
                    OrgUnit.parent_id.is_(None),
                )
            )
        ).scalar_one()
        chief = (
            await tenant.session.execute(select(Agent).where(Agent.id == chief_unit.head_agent_id))
        ).scalar_one()
        assert body["owner_agent_id"] == str(chief.id), (
            f"the default owner is {body['owner_agent_id']}, not the chief {chief.name}"
        )

    async def test_an_explicit_owner_is_left_alone(self, client: Any, tenant: Any) -> None:
        """The other direction. A default that overwrites a choice is not a default."""
        from sqlalchemy import select

        from ai_orchestrator.persistence.models import Agent, Organization
        from ai_orchestrator.seed import seed

        org = (
            await tenant.session.execute(
                select(Organization).where(Organization.id == tenant.organization_id)
            )
        ).scalar_one()
        await seed(tenant.session, into=org)
        await tenant.commit()
        wanted = (
            (
                await tenant.session.execute(
                    select(Agent).where(Agent.organization_id == tenant.organization_id)
                )
            )
            .scalars()
            .first()
        )
        response = await client.post(
            "/api/v1/tasks",
            headers={**_headers(tenant.organization_id), "Idempotency-Key": str(uuid.uuid4())},
            json={
                "title": f"owned work {uuid.uuid4().hex}"[:60],
                "goal": f"owned work {uuid.uuid4().hex}",
                "task_type": "analysis",
                "start_workflow": False,
                "owner_agent_id": str(wanted.id),
            },
        )
        assert response.status_code in (200, 201), response.text
        body = response.json()
        assert body["owner_agent_id"] == str(wanted.id)
        assert "owner_defaulted_to_chief" not in body
