# Launching SPARTON

The operational runbook is [DEPLOYMENT.md](DEPLOYMENT.md): how to build, run and
operate the thing. This file is the *go-live* checklist, the decisions and
values you must supply before real customers can use the product, and the
evidence you must produce before you tell anyone it is ready.

Nothing here is theoretical. Each item names the failure it prevents, because
"set the secrets" is not a checklist; "here is what happens if you don't" is.

---

## 1. Before you touch the server

### 1.1 Accounts you need, and what for

| What | Why | Where to get it |
|---|---|---|
| OpenRouter API key | Every report and alert summary. Without it the product still works, change detection is pure arithmetic, but reports return a deterministic placeholder instead of prose. | [openrouter.ai/keys](https://openrouter.ai/keys) |
| Stripe account | Payments. The product is free to try without one; `POST /billing/checkout` refuses to start rather than failing at the payment step. | [dashboard.stripe.com](https://dashboard.stripe.com) |
| SMTP provider | Verification and password-reset email. **Without it nobody can confirm an email address, so in production nobody can use the product.** | Any provider, or your own relay |
| A domain + TLS | `APP_URL` goes into every verification and reset link. Getting this wrong sends customers to `localhost:8000`. | Registrar + Let's Encrypt or equivalent |
| A host | One that can run Docker and Postgres. | Any VPS or container platform |

### 1.2 The two facts to check before you build

**Do the model ids still exist.** The defaults ship as
`anthropic/claude-sonnet-4.6` and `google/gemini-3.5-flash-lite`. They were
retired once already in this project's history, the previous defaults 404'd the
first time a customer used a paid feature. Verify before launch:

```bash
curl -s https://openrouter.ai/api/v1/models | grep -o '"id":"[^"]*"' | grep -E 'sonnet-4.6|flash-lite'
```

If either is missing, set `LLM_MODEL_STRONG` / `LLM_MODEL_CHEAP` to ids that are
present. `tests/test_ssrf_and_proxy.py` fails if a retired id is assigned again.

**Decide the email verification policy before you set `SPARTON_ENV=production`.**
`EMAIL_VERIFICATION_REQUIRED` defaults to on in production and off in
development. It is not cosmetic: with it off, anyone can register with someone
else's address and then trigger a password reset for that account.

---

## 2. Secrets

Every value below is a real secret. None belongs in git, in an image layer, in a
support ticket, or in Slack.

```bash
# Generate the ones that want randomness
openssl rand -hex 32   # POSTGRES_PASSWORD
openssl rand -hex 32   # OPENAI_API_KEY, if your provider issues one
```

| Variable | Exposure if leaked | Rotate how |
|---|---|---|
| `POSTGRES_PASSWORD` | Full read/write on all customer data: shops, competitors, price history, email addresses. | Change in the DB and the env together; requires a restart. |
| `STRIPE_SECRET_KEY` | Charges refunds, reads customer records. | Stripe dashboard → rotate. The old key stops working immediately. |
| `STRIPE_WEBHOOK_SECRET` | Lets an attacker forge subscription events, i.e. grant themselves a paid plan. | Stripe dashboard → rotate. Re-deliver any events in flight. |
| `OPENAI_API_KEY` / OpenRouter key | Someone else's bill, and access to your usage data. | Provider dashboard → rotate. |
| `SMTP_PASSWORD` | Send mail as you. The cheapest spam relay there is. | Provider → rotate. |
| `API_KEY` (if set) | Bypasses session auth entirely. | Restart. |

**Two ordering rules that are easy to get wrong:**

1. **Register the Stripe webhook *before* you enable paid plans.** Webhook events
   are the only authority that grants a paid plan. If you enable Pro pricing
   first, a customer can pay and receive nothing, because no endpoint exists to
   confirm it. Enable the webhook, confirm it delivers, *then* flip
   `BILLING_ENABLED`.
2. **Set `STRIPE_WEBHOOK_SECRET` in the same deploy as the webhook
   registration.** With no secret configured, `POST /stripe/webhook` returns
   `503`, it refuses loudly rather than accepting unsigned events, which is the
   correct behaviour, but it means payments silently do not apply.

### 2.1 Values that are not secrets but are still required

| Variable | Example | Consequence of getting it wrong |
|---|---|---|
| `APP_URL` | `https://sparton.ai` | Customers receive reset links pointing at localhost. Silent, and discovered only when someone clicks the email. |
| `CORS_ORIGINS` | `https://app.sparton.ai` | A wrong value silently breaks the dashboard's fetches; the API still answers `curl`. |
| `SPARTON_ENV` | `production` | In development, email verification is off and error detail is verbose. |
| `POSTGRES_PASSWORD` |, | No default, on purpose: compose refuses to start without it rather than booting a database anyone can reach. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | `*` lets any caller forge `X-Forwarded-For` and bypass per-client rate limits on login, signup and password reset. See DEPLOYMENT.md. |

---

## 3. Stripe, end to end

1. Create two recurring prices: **Pro** and **Business**, matching the amounts in
   `app/billing/plans.py`. The landing page renders from `GET /billing/plans`, so
   the advertised price and the charged price are the same number by
   construction, but they are still two places to check.
2. Put the price ids in `STRIPE_PRICE_PRO` and `STRIPE_PRICE_BUSINESS`.
3. Add a webhook endpoint: `https://<your-domain>/stripe/webhook`.
4. Subscribe it to at least:
   - `checkout.session.completed`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.paid`
   - `invoice.payment_failed`
5. Copy the signing secret into `STRIPE_WEBHOOK_SECRET`.
6. Deploy, then confirm delivery in the Stripe dashboard (it shows recent
   deliveries and their HTTP status).

**An unknown price id fails closed to Free.** That is deliberate: a typo in
`STRIPE_PRICE_PRO` downgrades rather than silently granting unlimited access. If
a paying customer reports they are on Free, check this first.

**Test the webhook before you need it.** In Stripe's dashboard, resend a past
event to your endpoint and confirm the plan changed. A webhook that has never
been observed working is not a webhook.

---

## 4. Email

Verification and password reset both depend on SMTP being correct. With
`SMTP_HOST` unset, messages are written to `./data/outbox` instead of being sent,
which is right for development and catastrophic in production, because it looks
like it worked.

Checklist:

- [ ] `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` set.
- [ ] `SMTP_FROM` is a domain you can send from, with SPF and DKIM published.
      Gmail and Outlook reject mail from unverified senders outright.
- [ ] `AUTH_TOKEN_TTL_MINUTES` is a deliberate choice. The default is 24 hours;
      for verification you probably want much less (1 hour is normal).
- [ ] Sign up with a real address and confirm the verification email *arrives*.
- [ ] Click the link, then request a password reset and confirm that arrives too.
- [ ] If you use an outbox for on-prem: `data/outbox` is a directory of `.eml`
      files. It contains live reset tokens. Treat it as a secret store.

---

## 5. First deploy

```bash
git clone <your-fork> && cd sparton
cp .env.example .env          # then edit it: see sections 2-4
docker compose up -d
docker compose ps             # wait for db/redis/api to report healthy
```

Then verify the deployment is actually serving, not merely running:

```bash
curl -fsS https://<domain>/live            # 200, the process is up
curl -fsS https://<domain>/ready           # 200, the database answers
curl -sS https://<domain>/billing/plans    # the pricing table
curl -sS -o /dev/null -w '%{http_code}\n' https://<domain>/shops   # 401, not 200
```

That last one is the single most important check on this page. A `200` means the
anonymous-reachable surface has grown, which is how Phase 1 of this project
found twelve such endpoints. It is pinned by
`tests/test_route_coverage.py`, which reads the route table from the app's own
OpenAPI document, so it cannot silently regress.

**Running more than one API replica?** Migrations become a one-off pre-deploy
step, not part of the start command. See DEPLOYMENT.md, three replicas running
`alembic upgrade head` simultaneously crash-loop against each other.

---

## 6. Backups and rollback

The data that cannot be regenerated: customers, shops, competitors, captures,
change history, invoices. A competitor's site is crawled again, but the price
history you sold the customer is only in your database.

```bash
# Nightly, to somewhere other than the database host
docker compose exec -T db pg_dump -U sparton sparton | gzip > sparton-$(date +%F).sql.gz

# Restore into a scratch database first, and prove it works
gunzip -c sparton-2026-01-01.sql.gz | docker compose exec -T db psql -U sparton -d sparton_restore
```

A backup you have never restored is a hypothesis. Restore one into a scratch
database and query it before you need it.

**Point-in-time recovery** is worth turning on for any deployment taking real
money. A `DELETE FROM invoices` with no recovery is unrecoverable regardless of
nightly dumps.

**Rollback** is: redeploy the previous image tag, then run
`alembic downgrade` only if the new release included a migration. Schema
downgrades are not always possible; treat "migration applied" as a one-way door
and prefer additive changes (add a column, backfill, switch reads) over
destructive ones.

---

## 7. Monitoring

Minimum, before the first paying customer:

- [ ] `/live` and `/ready` polled, with alerting on failure. `/ready` checks the
      database; `/live` only checks the process.
- [ ] `POSTGRES_PASSWORD` and disk space. A full disk is the most common
      self-inflicted outage, and it takes the queue with it.
- [ ] Stripe webhook delivery failures, a 500 from our endpoint means a paying
      customer did not get their plan. The handler returns 500 on purpose so
      Stripe retries; alert on it.
- [ ] OpenRouter error rate and spend. The provider is the only external
      dependency in the request path.
- [ ] Crawl failure rate. A crawl that fails repeatedly is usually a competitor
      blocking us, which is a product problem the customer will report.

The product degrades rather than fails: if the LLM is down, change detection
still works and reports return deterministic text. That is on purpose, and it
means an LLM outage should not page anyone at 3am.

---

## 8. Legal and customer-facing

- [ ] `/legal/privacy`, `/legal/terms`, `/legal/dpa` are reachable and reflect
      how the product actually behaves. Specifically: what you collect (store
      URLs, crawled product data, email addresses), where it goes (OpenRouter
      receives page content for summarisation, say so), how long you keep it,
      and how to delete it.
- [ ] The DPA is a real commitment if you sell to businesses. Read it.
- [ ] The landing page makes no claim you cannot honour. In particular the
      pricing table is served from `GET /billing/plans`, so it cannot drift from
      what the server enforces, but check that the copy around it is true.
- [ ] Decide your refund policy and put it where Stripe can enforce it.

---

## 9. Go-live smoke test

Run this yourself, in a browser, before telling anyone the product is live. Every
item is a real user action; every one of them was a bug at least once.

- [ ] **Landing page loads and shows prices.** If the pricing table says
      "Loading plans…" or "temporarily unavailable", stop. This exact failure
      shipped once: a 404 on a module import killed the whole script, and the
      page still looked finished because it is static HTML.
- [ ] **Every "Start free" link reaches the signup form**, not a login form. This
      also shipped once.
- [ ] **Sign up with a real email address.** The verification mail arrives and
      the link works.
- [ ] **Password reset**, request it, receive it, use it.
- [ ] **Add a shop** with a real public storefront URL. The API rejects
      non-public addresses, which is correct: if your own test shop is refused,
      the URL is not publicly resolvable.
- [ ] **Add a competitor** and trigger a crawl. Products appear.
- [ ] **Change a competitor's price by hand** (or wait for the next crawl) and
      confirm a change event with an evidence URL and a capture timestamp. The
      evidence URL is the product: an alert a customer cannot verify is an alert
      they will not act on.
- [ ] **Generate a report** and read it. Confirm the model ids are live.
- [ ] **Open billing**, start Checkout, complete it in Stripe, return. Confirm
      the plan actually changed, this is the end-to-end check that the webhook
      is registered, signed correctly, and mapping your price ids.
- [ ] **Cancel** in the Customer Portal and confirm the plan drops.
- [ ] **Sign out and back in** on a second browser. If logout appears broken,
      note that it returned 500 for every caller at one point.
- [ ] **Tenant isolation:** register a second account, log in as it, and confirm
      the first account's shop is neither listed nor reachable by direct URL.

---

## 10. Definition of done

Do not call it launched until every one of these is proven by a command you ran,
not by a test someone said passed.

- [ ] `pytest` is green: **574 passed, 0 failed**.
- [ ] `pytest tests/test_browser_smoke.py` is green in CI with a real Chromium.
- [ ] The image builds and runs non-root, and `/live` answers from the running
      container.
- [ ] `curl /shops` with no credentials returns `401` **on the deployed host**,
      not just in the suite.
- [ ] A real signup → verification → shop → competitor → crawl → report → payment
      → cancellation, performed by a person in a browser.
- [ ] A backup has been taken *and restored into a scratch database*.
- [ ] Alerting fires for `/ready`, webhook failures, and disk space, tested by
      waiting for it, not by assuming the integration is correct.
- [ ] Legal pages reflect reality.
- [ ] A rollback to the previous image has been rehearsed.

### Known limitations to disclose, not to discover

- The LLM writes prose; it does not decide whether a price changed. Change
  detection is arithmetic. If the provider is down, reports degrade to
  deterministic text rather than disappearing.
- Crawls are polite and bounded (one-second floor per host, page and depth caps).
  A competitor who blocks us produces failed crawls, not silent results.
- A DNS rebinding defence cannot be perfect from userspace. The crawler checks the
  resolved name on every redirect hop *and* the connected peer address, which
  closes the known attack, but a determined attacker with control over their own
  DNS could in principle still win. This is why the crawler is also rate-limited
  and capped, rather than trusted to a single check.
- The crawler respects robots.txt. A competitor who disallows us produces no
  data, and the report says so rather than inventing numbers.

### `shopfeed` -- the local, private dependency

`app/ecommerce/feeds.py` reads competitor catalogs through the **`shopfeed`**
library: the shop's own public feed when there is one, then `schema.org`
JSON-LD, and only then the existing HTML crawl. A feed price is the exact number
the shop charges; an HTML-parsed price is our best reading of a rendering, and
the competitor view says which of the two you are looking at.

`shopfeed` is **private** and is not installed from a git URL. Install it from
the local checkout:

```bash
pip install -e W:/shopfeed
```

In the image, add that line to the builder stage of the `Dockerfile` (alongside
`pip install -r requirements.txt`) and add `shopfeed` to the `COPY` paths.

**It is optional at runtime.** If the import fails, `read_feed_catalog` returns
an empty result and the crawl falls back to the HTML path: slower, costs tokens,
and the prices are marked `extracted from the page` instead of `exact`. Nothing
breaks and no crawl is lost -- the feature is an improvement, not a dependency.
The `tests/test_feeds.py` file skips when it is absent.

Verify after installing:

```bash
python -c "import shopfeed; print(shopfeed.__file__)"
# then, on a real Shopify competitor, the crawl should report
# data_source == "feed" and record no LLM usage
```

Two things worth knowing about the behaviour:

- **A sale is recorded as `compare_at`**, so a markdown that *ends* is visible.
  Without it the price before and after is identical and the change is silent.
- **Robots.txt still applies.** A shop that disallows crawling gets no data from
  any tier. The report says so rather than inventing numbers.

### The image is 82.5 MB, and why that is worth checking

Measured on the built image, not estimated:

| | Before | After |
|---|---|---|
| `docker image inspect --format {{.Size}}` | 9.8 GB | **86,554,591 bytes (82.5 MB)** |

The saving is the PyTorch stack, pulled in by `sentence-transformers` and
`faiss-cpu` for the feature-flagged documents/RAG, generation, datasets and
training domains. Those are off by default and are not what a customer pays for.

If you build the image and it is gigabytes again, something has re-imported an
experimental router at module scope. `app/main.py` and `app/api/__init__.py` both
have to stay free of `create` and `knowledge` at import time --
`tests/test_product_dependencies.py` fails if either does, by running the import
with those libraries made unimportable.

To build with the experimental domains:

```bash
docker build --build-arg INSTALL_EXPERIMENTAL=1 -t sparton:full .
```

Expect ~9 GB. A default deployment should never need it, and if you find
yourself reaching for it, the flag on the *app* is probably the thing to set:
`ENABLED_DOMAINS=...,documents,...`.

`shopfeed` is not in the image either: it is private and installed from a local
path. Without it the product still crawls -- the HTML path, which costs tokens
and labels its prices "extracted from the page" rather than "exact". The image
verifies this on startup, so a missing feed degrades the data's provenance
rather than the product's availability.
