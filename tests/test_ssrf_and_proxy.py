"""SSRF, body-size, proxy-trust and scoping guards.

Every test here corresponds to a way the previous version could be made to do
something it was supposed to refuse. They are written as attacks rather than as
API documentation: the assertion is that a hostile *response* is refused, not
that a function returns a particular value.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent


def make_crawler(handler, **policy_overrides):
    """A crawler over a MockTransport, with no delay and no retries.

    `follow_redirects=False` on the client matters: these tests are about what
    the crawler does with a redirect, and httpx following it itself would hide
    the behaviour under test.
    """
    from app.research.crawler import CrawlPolicy, ResponsibleCrawler

    def transport(request: httpx.Request) -> httpx.Response:
        return handler(str(request.url), request)

    client = httpx.Client(
        transport=httpx.MockTransport(transport), follow_redirects=False
    )
    policy = CrawlPolicy(delay_seconds=0.0, max_retries=0, **policy_overrides)
    return ResponsibleCrawler(policy=policy, client=client)


def public_dns(monkeypatch):
    """Make every hostname resolve to a public address."""
    from app.research import crawler as crawler_mod

    monkeypatch.setattr(
        crawler_mod, "_resolve_public_addresses", lambda host: ["93.184.216.34"]
    )


def resolved_to(monkeypatch, mapping):
    """Resolve hostnames via a dict, so each host can differ.

    Matching is exact-then-suffix, which also covers a bare IP literal used as
    a redirect target: `urlparse("http://127.0.0.1/admin").hostname` is the
    string `"127.0.0.1"`, and that is what the guard looks up.
    """
    from app.research import crawler as crawler_mod

    def resolve(hostname):
        if hostname in mapping:
            return [mapping[hostname]]
        for suffix, address in mapping.items():
            if suffix and hostname.endswith("." + suffix):
                return [address]
        return ["93.184.216.34"]

    monkeypatch.setattr(crawler_mod, "_resolve_public_addresses", resolve)


class TestRedirectsCannotBypassTheAddressCheck:
    """The original guard validated the first URL and let httpx follow the rest.

    A public competitor URL answering `302 Location: 169.254.169.254` is the
    whole attack, and it is one header away.
    """

    def test_the_client_does_not_follow_redirects_itself(self):
        """If httpx follows them, no amount of checking the first URL helps."""
        from app.research.crawler import ResponsibleCrawler

        crawler = ResponsibleCrawler()
        try:
            assert crawler.client.follow_redirects is False
        finally:
            crawler.close()

    def test_a_redirect_to_loopback_is_blocked(self, monkeypatch):
        resolved_to(monkeypatch, {"evil.test": "93.184.216.34", "127.0.0.1": "127.0.0.1"})

        def handler(url, request):
            if url.startswith("https://evil.test"):
                return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})
            return httpx.Response(200, text="<html></html>", request=request)

        result = make_crawler(handler).fetch("https://evil.test/start")
        assert result.error, "a redirect to loopback was followed"
        assert "127.0.0.1" in result.error, result.error

    def test_a_redirect_to_cloud_metadata_is_blocked(self, monkeypatch):
        """169.254.169.254 is the credential-theft target on every cloud."""
        resolved_to(
            monkeypatch,
            {"evil.test": "93.184.216.34", "169.254.169.254": "169.254.169.254"},
        )

        def handler(url, request):
            if url.startswith("https://evil.test"):
                return httpx.Response(
                    301, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
                )
            return httpx.Response(200, request=request)

        result = make_crawler(handler).fetch("https://evil.test/start")
        assert result.error, "a redirect to cloud metadata was followed"
        assert "169.254.169.254" in result.error, result.error

    def test_a_redirect_to_a_private_range_is_blocked(self, monkeypatch):
        resolved_to(monkeypatch, {"evil.test": "93.184.216.34", "10.0.0.5": "10.0.0.5"})

        def handler(url, request):
            if url.startswith("https://evil.test"):
                return httpx.Response(307, headers={"Location": "http://10.0.0.5/"})
            return httpx.Response(200, request=request)

        assert make_crawler(handler).fetch("https://evil.test/s").error

    def test_a_dns_rebinding_peer_is_rejected(self):
        """The name validated public; the socket we actually got was private.

        This is the window URL checking cannot close, so the connected peer is
        checked too.
        """
        from app.research.crawler import (
            CrawlPolicy,
            ResponsibleCrawler,
            SSRFBlockedError,
        )

        def peer_response(address):
            response = httpx.Response(200, request=httpx.Request("GET", "https://r.test/"))
            response.extensions = {
                "network_stream": type(
                    "Stream",
                    (),
                    {"get_extra_info": staticmethod(lambda name: (address, 443))},
                )()
            }
            return response

        crawler = ResponsibleCrawler(
            policy=CrawlPolicy(delay_seconds=0.0, max_retries=0),
            client=httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, request=r)),
                follow_redirects=False,
            ),
        )
        with pytest.raises(SSRFBlockedError):
            crawler._assert_peer_is_public(peer_response("127.0.0.1"))
        with pytest.raises(SSRFBlockedError):
            crawler._assert_peer_is_public(peer_response("169.254.169.254"))
        # A genuinely public peer must still pass, or the guard is useless.
        crawler._assert_peer_is_public(peer_response("93.184.216.34"))

    def test_a_redirect_loop_is_bounded(self, monkeypatch):
        resolved_to(monkeypatch, {"loop.test": "93.184.216.34"})

        def handler(url, request):
            return httpx.Response(302, headers={"Location": url + "/next"})

        result = make_crawler(handler, max_redirects=3).fetch("https://loop.test/a")
        assert result.error, "an unbounded redirect chain did not terminate"
        assert "redirect" in result.error.lower(), result.error

    def test_robots_txt_is_fetched_under_the_same_rules(self, monkeypatch):
        """robots.txt is a URL we construct, so an attacker controls it too."""
        resolved_to(
            monkeypatch, {"good.test": "93.184.216.34", "metadata.test": "169.254.169.254"}
        )
        requested = []

        def handler(url, request):
            requested.append(url)
            if url.endswith("/robots.txt"):
                return httpx.Response(
                    302, headers={"Location": "http://metadata.test/creds"}, request=request
                )
            return httpx.Response(200, text="<html></html>", request=request)

        crawler = make_crawler(handler)
        assert crawler._allowed("https://good.test/page") in (True, False)
        assert not any("metadata.test" in url for url in requested), requested


class TestBodiesAreStreamedAndCapped:
    """`response.content[:cap]` buffers the whole body and then discards most
    of it, so the cap bounded memory *after* the download rather than during it.
    """

    def test_a_large_body_is_capped_while_streaming(self, monkeypatch):
        public_dns(monkeypatch)
        sent = {"bytes": 0}

        def handler(url, request):
            def body():
                for _ in range(80):  # 5 MB in 64 kB chunks
                    sent["bytes"] += 65536
                    yield b"x" * 65536

            return httpx.Response(200, content=body(), request=request)

        result = make_crawler(handler, max_body_bytes=200_000).fetch(
            "https://big.test/page"
        )
        assert result.error is None, result.error
        # We stopped reading near the cap rather than draining 5 MB.
        assert sent["bytes"] < 2_000_000, sent["bytes"]
        assert sent["bytes"] >= 200_000, sent["bytes"]

    def test_an_unbounded_body_is_still_capped(self, monkeypatch):
        public_dns(monkeypatch)
        """No Content-Length: a chunked response, which is what a hostile
        server actually sends and what the cap must not depend on."""

        def handler(url, request):
            def body():
                while True:
                    yield b"A" * 100_000

            return httpx.Response(200, content=body(), request=request)

        result = make_crawler(handler, max_body_bytes=50_000).fetch(
            "https://stream.test/page"
        )
        assert result.error is None, result.error

    def test_a_body_under_the_cap_is_read_whole(self, monkeypatch):
        public_dns(monkeypatch)
        """The cap must not truncate legitimate small pages."""

        def handler(url, request):
            return httpx.Response(200, text="<html><body>hi</body></html>", request=request)

        result = make_crawler(handler, max_body_bytes=200_000).fetch(
            "https://small.test/page"
        )
        assert result.error is None, result.error
        assert result.content_hash


class TestNonPublicAddressesAreRefused:
    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",          # loopback
            "10.0.0.5",           # private
            "192.168.1.1",        # private
            "172.16.0.1",         # private
            "169.254.169.254",    # link-local, cloud metadata
            "0.0.0.0",            # unspecified
            "100.64.0.1",         # carrier NAT - the case is_global catches
            "192.0.0.1",          # IETF protocol assignments
            "::1",                # IPv6 loopback
            "fe80::1",            # IPv6 link-local
            "fc00::1",            # IPv6 unique local
            "::",                 # IPv6 unspecified
        ],
    )
    def test_refused(self, address, monkeypatch):
        from app.research import crawler as crawler_mod

        resolved_to(monkeypatch, {"host.test": address})
        assert crawler_mod._is_public_http_url("https://host.test/x") is False

    @pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1::"])
    def test_public_addresses_are_allowed(self, address, monkeypatch):
        from app.research import crawler as crawler_mod

        resolved_to(monkeypatch, {"host.test": address})
        assert crawler_mod._is_public_http_url("https://host.test/x") is True

    def test_multicast_is_refused(self, monkeypatch):
        from app.research import crawler as crawler_mod

        resolved_to(monkeypatch, {"host.test": "224.0.0.1"})
        assert crawler_mod._is_public_http_url("https://host.test/x") is False

    def test_one_private_record_poisons_a_mixed_resolution(self, monkeypatch):
        """A name with one public and one private A record lands on the private
        one about half the time, so one is enough to refuse."""
        from app.research import crawler as crawler_mod

        resolved_to(monkeypatch, {"mixed.test": "93.184.216.34"})
        monkeypatch.setattr(
            crawler_mod,
            "_resolve_public_addresses",
            lambda host: ["93.184.216.34", "10.1.2.3"],
        )
        assert crawler_mod._is_public_http_url("https://mixed.test/x") is False

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://h/", "ftp://h/x"])
    def test_non_http_schemes_are_refused(self, url, monkeypatch):
        from app.research import crawler as crawler_mod

        resolved_to(monkeypatch, {"h": "93.184.216.34"})
        assert crawler_mod._is_public_http_url(url) is False

    def test_an_unresolvable_host_is_refused(self, monkeypatch):
        from app.research import crawler as crawler_mod

        def boom(hostname):
            raise OSError("Name or service not known")

        monkeypatch.setattr(crawler_mod, "_resolve_public_addresses", boom)
        assert crawler_mod._is_public_http_url("https://nope.test/x") is False


class TestProxyHeadersAreNotTakenOnTrust:
    """`X-Forwarded-For` is attacker-controlled.

    Trusting it unconditionally means rotating the header per request gives a
    fresh rate-limit bucket every time, which makes every limit a suggestion.
    """

    @staticmethod
    def _request(client_host, headers):
        from starlette.requests import Request

        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/x",
                "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                "client": (client_host, 12345),
            }
        )

    def test_a_spoofed_header_does_not_change_the_identity(self):
        from app.core.security.rate_limit import client_key

        request = self._request("203.0.113.9", {"x-forwarded-for": "1.2.3.4"})
        assert client_key(request) == "203.0.113.9", (
            "a spoofed X-Forwarded-For changed the rate-limit identity"
        )

    def test_a_trusted_proxy_may_speak_for_its_client(self):
        from app.core.security.rate_limit import client_key

        request = self._request("127.0.0.1", {"x-forwarded-for": "1.2.3.4"})
        assert client_key(request) == "1.2.3.4"

    def test_the_default_trust_list_is_loopback_only(self):
        from app.core.config import settings

        assert settings.forwarded_allow_ips == "127.0.0.1"

    def test_a_junk_forwarded_value_falls_back_to_the_socket(self):
        from app.core.security.rate_limit import client_key

        request = self._request("127.0.0.1", {"x-forwarded-for": "not-an-ip"})
        assert client_key(request) == "127.0.0.1"

    def test_rotating_a_spoofed_header_cannot_buy_fresh_buckets(self):
        """The end-to-end consequence: one attacker, one identity, one limit."""
        from app.core.security.rate_limit import client_key, reset_limits

        reset_limits()
        seen = set()
        for i in range(5):
            request = self._request("198.51.100.7", {"x-forwarded-for": f"10.0.0.{i}"})
            seen.add(client_key(request))
        assert seen == {"198.51.100.7"}, seen

    def test_the_dockerfile_does_not_trust_every_proxy(self):
        """`*` would make the check above meaningless."""
        cmd = (ROOT / "Dockerfile").read_text(encoding="utf-8").split("\nCMD", 1)[-1]
        assert "--forwarded-allow-ips='*'" not in cmd
        assert "FORWARDED_ALLOW_IPS" in cmd, "the Dockerfile hardcodes the trust list"


class TestInvoicesAreScopedToTheCallersOrganization:
    def test_a_user_with_no_organization_gets_nothing(self, client, db_session):
        """`organization_id IS NULL` returns every *unscoped* invoice in the
        table. No organization must mean no rows, not all of them."""
        from app.api.billing import invoices
        from app.core.database.billing_models import Invoice

        db_session.add(
            Invoice(
                organization_id=None,
                stripe_invoice_id="in_unscoped",
                status="paid",
                amount_cents=1000,
                currency="eur",
            )
        )
        db_session.commit()

        user = type("U", (), {"organization_id": None})()
        assert invoices(db=db_session, user=user) == {"count": 0, "invoices": []}

    def test_a_scoped_user_only_sees_their_own(self, client, auth_headers, db_session):
        from sqlalchemy import select

        from app.core.database.billing_models import Invoice
        from app.core.database.identity import Organization, User

        org_id = db_session.execute(
            select(User).where(User.email == "admin@example.com")
        ).scalars().first().organization_id
        # A real second tenant, not a made-up id: the FK constraint is what stops
        # a fabricated organization_id from being a shortcut around scoping.
        other = Organization(name="Other Co", slug="other-co")
        db_session.add(other)
        db_session.flush()
        db_session.add(
            Invoice(organization_id=org_id, stripe_invoice_id="in_mine", status="paid",
                    amount_cents=500, currency="eur")
        )
        db_session.add(
            Invoice(organization_id=other.id, stripe_invoice_id="in_theirs",
                    status="paid", amount_cents=700, currency="eur")
        )
        db_session.commit()

        response = client.get("/billing/invoices", headers=auth_headers)
        assert response.status_code == 200, response.text
        ids = [i["id"] for i in response.json()["invoices"]]
        assert "in_mine" in ids
        assert "in_theirs" not in ids


class TestConfiguredModelsStillExist:
    def test_the_defaults_are_not_retired_ids(self):
        """`anthropic/claude-3.5-sonnet` and `google/gemini-2.0-flash-001` are
        gone from OpenRouter: they 404 the first time a customer used a paid
        feature, which is the worst place to find out."""
        from app.core.config import settings

        for model in (settings.llm_model_strong, settings.llm_model_cheap):
            assert "claude-3.5" not in model, f"{model} is retired"
            assert "gemini-2.0-flash-001" not in model, f"{model} is retired"
        assert settings.llm_model_strong != settings.llm_model_cheap, (
            "strong and cheap routing to one model is not routing"
        )

    def test_no_shipped_assignment_still_names_a_retired_model(self):
        """Only real assignments count, not the comment explaining the change.
        
        A prose mention is the record of why the id changed; an assignment is
        what the app would actually call.'
        """
        retired = ("claude-3.5-sonnet", "gemini-2.0-flash-001", "claude-3.5-haiku")
        assignment = re.compile(r"""^[ \t]*(?:\w+[ \t]*:[^=\n]*)?=[ \t]*["\x27]([^"\x27]+)["\x27]""", re.M)
        offenders = []
        for path in list((ROOT / "app").rglob("*.py")) + [ROOT / ".env.example"]:
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue  # A comment is allowed to name what it replaced.
                match = assignment.match(line)
                value = match.group(1) if match else None
                offenders += [
                    f"{path.name}:{number} {value}"
                    for m in retired
                    if value and m in value
                ]
        assert not offenders, offenders

    def test_env_example_documents_the_current_models(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        from app.core.config import settings

        assert settings.llm_model_strong in text
        assert settings.llm_model_cheap in text


class TestTheRunbookCoversTheSharpEdges:
    def _docs(self) -> str:
        return (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    def test_forwarded_allow_ips_is_documented(self):
        docs = self._docs()
        assert "FORWARDED_ALLOW_IPS" in docs, "the proxy trust setting is undocumented"
        assert "forwarded-allow-ips" in docs, "uvicorn's flag is undocumented"

    def test_multi_replica_migrations_are_documented(self):
        """`alembic upgrade head` on every replica races with itself."""
        docs = self._docs()
        assert re.search(r"replica", docs, re.I), "replica behaviour is undocumented"
        assert re.search(r"one[- ]off|once|separately|single instance", docs, re.I), (
            "no guidance on running migrations once across replicas"
        )


class TestTheLaunchChecklistIsReal:
    """docs/LAUNCH.md is the file a person reads at 1am before pressing go.

    A checklist that has drifted from the application is worse than no
    checklist, so the things it asserts about the product are asserted here
    against the product.
    """

    @pytest.fixture(scope="class")
    def launch(self) -> str:
        path = ROOT / "docs" / "LAUNCH.md"
        assert path.exists(), "docs/LAUNCH.md does not exist"
        return path.read_text(encoding="utf-8")

    def test_it_covers_every_system_that_can_break_a_launch(self, launch):
        for topic, needle in (
            ("secrets", "STRIPE_WEBHOOK_SECRET"),
            ("SMTP", "SMTP_HOST"),
            ("Stripe webhook registration", "checkout.session.completed"),
            ("OpenRouter", "OPENROUTER"),
            ("migrations", "alembic"),
            ("backups", "pg_dump"),
            ("rollback", "downgrade"),
            ("monitoring", "/ready"),
            ("legal pages", "/legal/privacy"),
            ("go-live smoke", "go-live"),
        ):
            assert needle.lower() in launch.lower(), f"{topic} is undocumented"

    def test_the_health_endpoints_it_names_are_real(self, launch):
        from app.main import create_app

        paths = create_app().openapi()["paths"]
        for path in ("/live", "/ready"):
            assert path in launch, f"{path} is not in the checklist"
            assert path in paths, f"{path} does not exist"

    def test_the_legal_paths_it_names_are_real(self, launch):
        from app.main import create_app
        from fastapi.testclient import TestClient

        client = TestClient(create_app())
        for path in ("/legal/privacy", "/legal/terms", "/legal/dpa"):
            assert path in launch, f"{path} is not in the checklist"
            response = client.get(path)
            assert response.status_code == 200, f"{path} returns {response.status_code}"

    def test_the_checklist_names_the_anonymous_probe(self, launch):
        """The single most important line on the page: `/shops` must be 401 on
        the deployed host, not only in the suite."""
        assert "/shops" in launch
        assert "401" in launch

    def test_the_quoted_test_count_matches_the_suite(self, launch):
        """A stale count in a definition of done is a small lie that outlives
        the change that made it true."""
        import re

        quoted = re.search(r"(\d+) passed", launch)
        assert quoted, "the checklist does not state a test count"
        report = ROOT / "report.xml"
        if not report.exists():
            pytest.skip("no report.xml from the current run")
        actual = re.search(r'tests="(\d+)"', report.read_text(encoding="utf-8"))
        assert actual, "could not read the test count from report.xml"
        assert int(quoted.group(1)) == int(actual.group(1)), (
            f"LAUNCH.md says {quoted.group(1)} passed, the suite ran {actual.group(1)}"
        )

    def test_it_does_not_contain_a_real_secret(self, launch):
        for pattern in (r"sk-[A-Za-z0-9]{20,}", r"whsec_[A-Za-z0-9]{20,}", r"sk_live_"):
            assert not re.search(pattern, launch), "a real-looking secret is committed"
