# Deploying SPARTON

This is the runbook for putting SPARTON in front of paying customers. It
assumes you can run `docker compose` on a Linux host and terminate TLS in
front of it.

Read [docs/PROGRESS.md](PROGRESS.md) for where the project actually is. This
document describes only how to deploy what exists.

---

## What you are deploying

Four services, all in one repository:

| Service | What it does | Scales with |
|---|---|---|
| `api` | Serves the landing page, the dashboard, and the whole REST API | Customer traffic |
| `worker` | Drains the crawl and report queue | How many shops you monitor |
| `db` | PostgreSQL — every tenant's data | Disk and IOPS |
| `redis` | Job queue and distributed rate limiting | Queue depth |

`api` and `worker` run the **same image** with different commands. They must
never diverge: a worker running older code will process jobs with an older
schema and fail in ways that are very hard to read.

---

## Before you start

You need:

- A host with Docker and the Compose plugin
- A domain with DNS pointing at it (for TLS, and for the links in emails)
- A PostgreSQL 16 instance — the Compose file provides one, but a managed one is
  better in production because it has backups you did not have to write
- A Stripe account, if you want to charge anyone
- An SMTP provider. **Without one, verification and password-reset emails go to
  a file on disk and nobody receives them.** See "Email" below.

Generate secrets. Do not reuse them across environments:

```bash
openssl rand -base64 32   # POSTGRES_PASSWORD
openssl rand -base64 48   # API_KEY
```

---

## First deploy

```bash
git clone <your-repo> sparton && cd sparton
cp .env.example .env
$EDITOR .env          # see "The settings that matter" below
docker compose up -d --build
docker compose logs -f api
```

Migrations run automatically in the `api` container's start command. There is
no separate migration step, and there deliberately is no `|| true` after it: a
failed migration must stop the deploy rather than leave a process serving
requests against a schema that does not match its code.

Check it came up:

```bash
curl -fsS https://your-domain/live     # {"status":"live"}
curl -fsS https://your-domain/ready    # {"status":"ready"}
```

`/live` and `/ready` are the health endpoints, mounted without a prefix. They
answer without authentication by design and say nothing about the database, the
model provider, or any tenant data.

If the container is running but `/ready` fails, the database is the thing to
look at: `docker compose logs db`.

---

## The settings that matter

Everything else in `.env.example` has a safe default. These do not.

### `SPARTON_ENV=production`

The single most consequential line. In `production` the email verification
gate is enforced and the strict auth checks run. Set it to `development` in
production and every new account is unverified but fully usable, and the checks
added in Phase 1 silently do nothing.

### `APP_URL`

The public URL, including the scheme. Used for CORS, the OpenRouter attribution
header, and **the links inside verification and password-reset emails**. Get it
wrong and the reset link in a customer's inbox points at `localhost`, which is
the worst possible moment to discover it. Set it before the first invitation.

### `CORS_ORIGINS`

The dashboard is served from the same origin, so this only needs to list extra
origins you actually use. Setting it to `*` on a service that authenticates
with bearer tokens means any site can make authenticated requests on a user's
behalf if that script is compromised.

### `POSTGRES_PASSWORD`

Compose refuses to start without it rather than booting a database anyone on
the network can reach. There is no default.

### `API_KEY`

The shared secret for the machine principal. **Unset means no machine access at
all**, which is the correct default: an unset key used to mean "everyone is an
admin". If you need server-to-server calls, set it and treat it like the
database password.


---

## Email

Verification and password reset both depend on real mail. With `SMTP_HOST`
unset, `app/core/email.py` writes `.eml` files into `/app/data/outbox` instead
of sending them. That is genuinely useful in development and catastrophic in
production: everyone who signs up is stuck, unable to verify or to reset.

```ini
SMTP_HOST=smtp.your-provider.com
SMTP_PORT=587
SMTP_STARTTLS=true
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_FROM=no-reply@your-domain
```

`SMTP_FROM` must be a domain you control SPF and DKIM for, or it will be
spam-foldered. Send a test signup and confirm the message actually arrives
before you invite anyone.

Do not mount the outbox volume in production. It contains live password-reset
links.

---

## Payments

Without Stripe keys the billing screens are visible but inert, and every
account is on the Free plan. That is a legitimate way to run a free tier.

To charge:

```ini
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_PRO=price_...      # must match your €29 Pro price
STRIPE_PRICE_BUSINESS=price_... # must match your €79 Business price
BILLING_ENABLED=true
```

Then register the webhook endpoint, which is `POST /stripe/webhook`, for:

- `checkout.session.completed`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `invoice.paid`
- `invoice.payment_failed`

Use `stripe listen --forward-to localhost:8000/stripe/webhook` in development
and copy the printed `whsec_...` into `.env`. In production the endpoint must
be reachable over HTTPS, and the signing secret must be the one Stripe shows
for that endpoint.

**Webhooks are the only thing that grants a paid plan.** Checkout hands the
customer to Stripe and the webhook brings them back. A customer who completes
payment but whose webhook is blocked stays on Free until you resend the event,
so check the endpoint is reachable before you launch.

An unknown price id fails closed to Free rather than granting the highest plan
to anyone whose price id we do not recognise.

---

## The model provider

SPARTON calls OpenRouter for report prose. Product prices, stock and change
detection are computed locally and are never sent to a model — if the provider
is down, the numbers still work and only the narrative is missing.

```ini
LLM_PROVIDER=openai_compatible
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_API_KEY=sk-or-v1-...
LLM_MODEL_STRONG=anthropic/claude-3.5-sonnet
LLM_MODEL_CHEAP=google/gemini-2.0-flash-001
```

`LLM_MODEL_CHEAP` handles extraction, `LLM_MODEL_STRONG` handles report writing
and the agent. Every call is recorded in `llm_usage` against the organization,

---

## Operating it

### Scaling

Add API replicas behind your load balancer with no other change:

```bash
docker compose up -d --scale api=3
```

`worker` scales the same way and is usually the one that needs it: crawl and
report jobs are I/O-bound and slow. More workers means more requests to
competitor sites, so raise it deliberately.

Do **not** scale `db` or `redis` with `--scale`. They hold state and need
replicas, backups and failover, not more containers on one host.

### Backups

Back up PostgreSQL, and test restoring it. The Compose volume is not a backup.

```bash
docker compose exec -T db pg_dump -U sparton sparton | gzip > sparton-$(date +%F).sql.gz
```

`product_snapshots` and `change_events` are the append-only history your
customers' reports are computed from. Lose them and past reports stop being
reproducible, which is the whole basis on which we claim they are trustworthy.

### Logs

Structured JSON; `LOG_LEVEL` controls verbosity. They carry the request id and
the organization id, so you can trace one customer's request end to end.

### Upgrades

```bash
git pull
docker compose up -d --build
```

Migrations run on start. Before deploying, read the new migration's `upgrade()`
and check whether it is reversible — the test suite asserts a full
downgrade/upgrade round trip, but a destructive change (dropping or narrowing a
column) is worth a second pair of eyes and a backup regardless.

---

## Security checklist

Before you point a domain at this:

- [ ] `SPARTON_ENV=production`
- [ ] `APP_URL` is the real public URL, and a test signup really arrives
- [ ] `POSTGRES_PASSWORD` and `API_KEY` are long, random, and not in git
- [ ] Postgres is not reachable from the public internet
- [ ] TLS is terminated in front, and HTTP redirects to HTTPS
- [ ] `CORS_ORIGINS` does not contain `*`
- [ ] `STRIPE_WEBHOOK_SECRET` is set and the endpoint is reachable
- [ ] `SMTP_HOST` is set — the outbox fallback is for development only
- [ ] `.env` is not in the image and not in git
- [ ] `pytest` is green
- [ ] You have restored a database backup at least once, on this host

---

## If something is wrong

| Symptom | Look at |
|---|---|
| Container restarts on boot | `docker compose logs api` — almost always a failed migration |
| `/ready` fails, `/live` works | `docker compose logs db`; the database is not accepting connections |
| Signup works, no email arrives | `SMTP_HOST` unset — check `/app/data/outbox` in development |
| Customer paid, still on Free | The Stripe webhook is not reaching `/stripe/webhook`; resend the event from the Stripe dashboard |
| Reports have no narrative | The model provider is unreachable; the numbers are still correct, see "The model provider" |
| A competitor shows 0 products | We respect `robots.txt` and the page may forbid crawling; check the crawl status on the Shops screen |
| Requests return 403 "confirm your email" | Correct in production. The customer must verify; if they cannot receive mail, the problem is SMTP |

which is how you check the unit economics before they check you.

The crawler identifies itself honestly in its user agent and respects
`robots.txt`. A site owner can see SPARTON in their logs, contact you, or block
you. That is the intended behaviour — please do not disable it to raise crawl
volume.
