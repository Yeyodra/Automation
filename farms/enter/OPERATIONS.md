# Enter farm: architecture and operations

This document is the source of truth for the current Enter/Converge farm. It describes the signup flow, mailbox and domain handling, isolated lanes, NvRouter delivery, deployment, monitoring, and failure classification.

Older files such as `VPS-DEPLOY.md`, `HTTP_MIGRATION.md`, and `CHANGELOG.md` describe previous experiments. Do not use their old host lists, credentials, concurrency recommendations, or legacy 9router path for a new deployment.

## Scope

The supported production path is:

```text
Emailqu mailbox
  -> Enter referral landing page
  -> official Get Free Credits action
  -> FingerprintJS event
  -> Enter risk-session
  -> Auth0 signup
  -> Turnstile
  -> email OTP
  -> password
  -> Enter callback and authenticated gateway session
  -> referral/onboarding/workspace setup
  -> API key creation
  -> key-only native NvRouter import
```

The farm must not report success until it has created a non-empty API key and workspace ID. Reaching the OTP page, callback, or gateway session is not enough.

## Repository layout

```text
farms/enter/farm.py                 Main account flow
farms/enter/lane_supervisor.py      Serial isolated lane with domain rotation
farms/enter/test_auth_gateway.py    Gateway, callback, session, and CTA tests
farms/enter/test_post_auth.py       Mailbox, domain, referral, and post-auth tests
farms/enter/test_lane_supervisor.py Lane classification and lifecycle tests
core/ninerouter.py                  Event-driven remote credential pusher
jobs/runner.py                      Hub runner and environment mapping
farms/enter/results/                Runtime results; ignored by Git
farms/enter/results/lanes/*.log     Lane logs; mode 0600, ignored by Git
farms/enter/results/lanes/*.json    Last lane state; mode 0600, ignored by Git
```

The project runs from the repository-wide virtual environment. Do not create a separate environment under `farms/enter`.

## Authentication flow

### 1. Referral landing

The browser opens:

```text
https://enter.converge.ai/?gift={gift}&inviter={urlencoded_inviter}&inviteeReward={reward}
```

The three values must stay aligned throughout the flow:

- `ENTER_GIFT_CODE`
- `ENTER_INVITER`
- `ENTER_INVITEE_REWARD`

The same gift code is used again by the post-auth referral claim.

### 2. Invitation dialog and official CTA

The page may render `Get Free Credits`, then automatically open a referral dialog labeled `You've got an invite`. That dialog covers the CTA.

`_click_official_login_action()` performs the supported UI sequence:

1. dismiss cookie consent when present;
2. locate the visible invitation dialog;
3. click that dialog's `Close` button;
4. wait for the dialog to become hidden;
5. find the visible `Get Free Credits` action;
6. fire its DOM click and observe the resulting network flow.

The click is intentionally fire-and-observe. Waiting for Playwright's navigation completion made the critical fingerprint preflight slower and produced false `risk-aware gateway login not observed` failures.

### 3. Fingerprint and risk session

The current client contract is:

```text
FingerprintJS agent.get()
  -> event_id + visitor_id
POST https://api.enter.pro/code/api/v1/auth/risk-session
  body: fp_event_id, visitor_id, invite_code, platform
GET https://enter.converge.ai/auth/login?risk_session_id=...
```

The browser must generate a real FingerprintJS event. Random locally generated event IDs are not equivalent. A risk-session response alone also does not prove that the authorization policy will approve the signup later.

The live client uses these approximate budgets:

```text
Fingerprint collection: 8 seconds
risk-session request:    3 seconds
combined login preflight: 11.5 seconds
```

The farm accepts the gateway transition only when `/auth/login` includes a non-empty `risk_session_id`.

### 4. Auth0 identifier, OTP, and password

The browser continues through Auth0 Universal Login:

```text
POST /u/signup/identifier
  -> email challenge
POST /u/email-identifier/challenge
  -> password page
POST /u/signup/password
  -> /authorize/resume
```

The identifier request includes an actual Turnstile token. If the token is unavailable, the farm fails before polling the mailbox. Continuing without a token misclassifies captcha failure as OTP or domain failure.

### 5. Callback and gateway session

Success and denial diverge after password:

```text
Success:
/authorize/resume
  -> /auth/callback?code=...
  -> authenticated /auth/session

Denied:
/authorize/resume
  -> /auth/callback?error=access_denied
```

A generic callback `access_denied` is an authorization/risk outcome. It is not proof that the email domain is blocked.

The gateway session parser requires:

- authenticated user object;
- `user.isNewUser == true` for a newly created account;
- non-empty access token;
- `expiresAt` as a 13-digit epoch-millisecond integer.

### 6. Post-auth setup

After authentication, the farm:

1. attempts the referral claim;
2. reads user information;
3. handles the client-visible merge state;
4. obtains an existing workspace and fails closed if none exists;
5. completes onboarding when required;
6. creates an API key;
7. saves the credential;
8. queues it for NvRouter.

Referral claim rejection is nonfatal for account creation because the official client catches the error and continues to the workspace. Operators must still report referral reward acceptance separately from signup success.

`merge_action=auto_link` without a merge candidate does not block setup. A pending candidate with no block reason remains unresolved and must not be silently linked.

## Emailqu mailbox contract

Emailqu is the current mailbox provider for isolated lanes. Its public API is keyless:

```text
GET /api/random-username
GET /api/domains/random
GET /api/domain/verify/{domain}
GET /api/public/emails/{address}?limit=20
```

A mailbox does not require a create call. The farm composes an address from a local part and a verified public apex domain.

### Custom local parts

Set:

```env
ENTER_EMAILQU_PREFIX=lane_a
```

The prefix is normalized to lowercase alphanumeric characters and truncated to 24 characters. The farm appends ten cryptographically random characters. For example:

```text
lanea8f3k1m9q2x@example.test
```

A custom prefix skips Emailqu's random-username endpoint. The domain catalog and verification checks still run.

### Domain pinning

Set:

```env
ENTER_EMAIL_MODE=emailqu
ENTER_EMAILQU_DOMAIN=example.test
```

A pinned domain must:

- exist in Emailqu's current public apex catalog;
- not be marked hidden or a subdomain;
- pass Emailqu's verification endpoint.

These checks prove that the domain can be selected. The inbox endpoint is first exercised during OTP polling. They do not prove inbox delivery or that Enter will accept the signup.

## Domain states and attribution

Keep mailbox health and Enter acceptance separate.

### Mailbox states

```text
mailbox_ok       Domain exists, verifies, and the inbox endpoint works
mailbox_failed   Emailqu cannot verify or read the inbox
otp_timeout      No OTP arrived within the configured timeout
```

### Enter states

```text
accepted            Signup reached API key creation
explicitly_blocked  Auth0 displayed a domain/provider-not-allowed message
access_denied       Authorization policy rejected the transaction
```

### Permanent blacklist rule

Permanently blacklist a domain only for explicit evidence such as:

```text
domain_not_allowed
email domain is not allowed
domain is not allowed to sign up
email provider is not allowed
```

Do not permanently blacklist for:

- `access_denied`;
- OTP timeout;
- Turnstile failure;
- missing gateway login;
- password-page stall;
- navigation timeout;
- referral claim 403.

An OTP timeout is a bounded mailbox retry. The same domain may have delivered OTPs successfully in earlier and later attempts.

### Whitelist semantics

A successful domain is a proven combination, not a universal property:

```text
domain + referral + proxy/egress + browser class + time window
```

The same domain can succeed in one lane and receive `access_denied` in another. A whitelist should therefore record the lane/referral context and timestamp rather than storing only a global boolean.

### Pure HTTP domain checks

A pure HTTP inventory can safely test:

- whether a provider currently lists a domain;
- whether Emailqu marks it verified;
- whether a random mailbox address can be composed;
- whether the public inbox endpoint returns successfully.

That result means `mailbox_ok`, not `accepted_by_enter`. Enter has no public `/check-domain` endpoint in the observed client or HAR flow. An authoritative Enter result requires the Auth0 identifier transaction with a valid transaction state, FingerprintJS-backed risk session, and Turnstile token.

Use these labels in an inventory:

```text
mailbox_ok          Pure HTTP provider checks passed
mailbox_failed      Provider checks failed
enter_accepted      A full signup created an API key
enter_blocked       Explicit domain/provider rejection
enter_ambiguous     access_denied, OTP timeout, gateway failure, or navigation failure
```

Never convert `mailbox_ok` directly into an Enter whitelist, and never convert an ambiguous browser result into a permanent blacklist.

## Why isolated lanes exist

Single-process parallel signup reused too many risk dimensions at once. Live tests showed:

- separate browser sessions and WARP exits alone were insufficient;
- overlapping signups under one domain/referral could be denied after password;
- two independently configured lanes could both succeed concurrently.

An isolated lane has its own:

- referral identity;
- sticky SOCKS proxy;
- mailbox prefix;
- serial browser lifecycle;
- domain cooldown map;
- state and log files.

Two lane supervisors produce aggregate c2 while each lane remains c1 internally.

## Lane supervisor

Run `farms/enter/lane_supervisor.py` once per lane.

Example:

```bash
python3 farms/enter/lane_supervisor.py \
  --lane lane-a \
  --gift GIFT_CODE_A \
  --inviter 'Inviter A' \
  --proxy socks5://127.0.0.1:40001 \
  --prefix lanea \
  --domains /home/auto/emailqu-candidates.txt \
  --preferred known-good-a.test \
  --gap 60 \
  --cooldown 900
```

A second lane uses a different referral, proxy, prefix, and preferably a different first-choice domain.

### Rotation algorithm

For each attempt, the supervisor:

1. parses and deduplicates the domain catalog;
2. removes permanently blocked domains;
3. skips domains still in lane cooldown;
4. selects the next available domain;
5. launches one `farm.py` account attempt;
6. classifies the terminal output;
7. rotates the selected domain to the queue tail;
8. sleeps for the lane gap.

Outcome handling:

```text
ok                  reuse later; no cooldown
explicit block      append to permanent blocklist
access_denied       lane cooldown, then select another domain
otp_timeout         lane cooldown, then select another domain
gateway_missing     lane cooldown, then select another domain
other               lane cooldown, then select another domain
```

The default ambiguous-failure cooldown is 900 seconds. The default inter-attempt gap is 60 seconds.

### Runtime files

```text
farms/enter/results/lanes/production-lane-{lane}.log
farms/enter/results/lanes/lane-{lane}-state.json
```

State example:

```json
{
  "lane": "lane-a",
  "domain": "example.test",
  "category": "access_denied",
  "at": "2026-08-07T13:00:00+00:00",
  "cooldown_domains": 2
}
```

The state file contains no email, OTP, password, API key, or token.

### Process cleanup

Each child attempt runs in its own process group. On timeout, the supervisor:

1. sends SIGTERM to the group;
2. waits up to 30 seconds;
3. checks whether any process in the group remains;
4. sends SIGKILL when necessary;
5. reaps the child.

This prevents orphaned Camoufox processes after a hung attempt.

## Concurrency and pacing

Do not treat `-c 4` as four independent identities. It creates overlapping signups inside one farm configuration.

Observed behavior on the current flow:

```text
single identity c2/c4 overlap: high post-password denial
single serial lane, 30s gap:   too aggressive
single serial lane, 60s gap:   productive
single serial lane, 120s gap:  conservative and productive
isolated two-lane c2:          productive when domains rotate independently
```

These are observations, not permanent upstream guarantees. Start with one lane, require a real API key and NvRouter import, then add a second isolated lane. Do not increase a lane's internal concurrency above one without a new bounded test.

## WARP and proxy affinity

The tested VPS layout uses ten local SOCKS endpoints:

```text
socks5://127.0.0.1:40001
...
socks5://127.0.0.1:40010
```

Each lane gets one sticky browser proxy for the full Enter/Auth0 attempt. Landing, fingerprint, risk-session, Auth0, callback, and browser-context post-auth traffic must not change egress mid-transaction.

Emailqu REST is separate from the browser context. `_emailqu_get()` tries direct HTTP first. On a network `URLError`, it falls back to the first configured SOCKS proxy. HTTP 4xx responses are not treated as transport failures and are not hidden by the fallback.

Preflight every configured port against the real Enter landing page, not only an IP echo service:

```bash
for port in $(seq 40001 40010); do
  code=$(curl -sS --proxy "socks5h://127.0.0.1:$port" \
    --max-time 20 -o /dev/null -w '%{http_code}' \
    https://enter.converge.ai/ || true)
  printf '%s %s\n' "$port" "$code"
done
```

HTTP 200 proves basic reachability. A real canary is still required.

## NvRouter delivery

Production uses `NinerouterPusher` in key-only command mode. Despite the historical class name, the target is the native NvRouter importer.

Required environment names:

```env
ENTER_9ROUTER_VPS_EVERY_N=1
NINEROUTER_VPS_HOST=<router-host>
NINEROUTER_VPS_USER=<restricted-user>
NINEROUTER_VPS_KEY=/home/auto/.ssh/nvrouter_push
NINEROUTER_VPS_COMMAND=<forced-import-command>
```

The remote key must be restricted to the importer command. Do not give the farm a general shell on the router. Do not use the legacy password/SQLite path for new deployments.

Delivery behavior:

- queue only complete credentials;
- auto-push after `every_n` successes;
- flush the final partial batch on normal exit;
- requeue a failed push in memory;
- let NvRouter encrypt/seal the imported credential.

An empty-payload smoke tests SSH and importer access, but it does not prove account delivery. Acceptance requires one real account, importer `accounts=1`, and a new enabled sealed NvRouter row.

## Fresh VPS deployment

### System packages

Ubuntu 24.04 example:

```bash
apt-get update
apt-get install -y \
  git python3-venv python3-pip tmux curl jq sqlite3 ca-certificates \
  docker.io docker-compose-v2 \
  libgtk-3-0 libasound2t64 libnss3 libxss1 libgbm1 fonts-liberation
systemctl enable --now docker
```

Create an unprivileged user:

```bash
id auto >/dev/null 2>&1 || useradd -m -s /bin/bash auto
usermod -aG docker auto
```

A 16 GB shared-CPU host should have at least 4 GB swap available. Do not add a duplicate swap entry.

### Clone and Python environment

```bash
su - auto -c '
  git clone https://github.com/Yeyodra/Automation.git /home/auto/Automation
  cd /home/auto/Automation
  git fetch origin
  git checkout --detach <reviewed-commit-sha>
  python3 -m venv .venv
  .venv/bin/pip install -U pip setuptools wheel
  .venv/bin/pip install -r requirements.txt paramiko requests[socks] curl-cffi
  .venv/bin/python -m camoufox fetch
'
```

Pin a reviewed commit SHA. Do not deploy whatever happens to be at `origin/main` without reviewing it first.

### Runtime environment template

Create `/home/auto/Automation/.env` with mode `0600`. Store real values only on the host or in an approved secret manager.

```env
ENTER_EMAIL_MODE=emailqu
ENTER_BROWSER_OS=linux
ENTER_HEADLESS=true
ENTER_9ROUTER_INJECT=false
ENTER_9ROUTER_VPS_EVERY_N=1
ENTER_BLOCKED_DOMAINS_FILE=/home/auto/Automation/farms/enter/results/gptmail_blocked_domains.txt

NINEROUTER_VPS_HOST=<router-host>
NINEROUTER_VPS_USER=<restricted-import-user>
NINEROUTER_VPS_KEY=/home/auto/.ssh/nvrouter_push
NINEROUTER_VPS_COMMAND=<forced-import-command>
```

Each lane supplies its own gift, inviter, proxy, prefix, and preferred domains through its launcher. Do not put multiple lane identities into one shared farm process.

### Multi-WARP

```bash
git clone https://github.com/Micolaabdi/multi-warp.git /opt/multi-warp
cd /opt/multi-warp
chmod +x scripts/*.sh
COUNT=10 ./scripts/up.sh
```

Require ten healthy containers and ten usable SOCKS ports before a canary.

### Mailbox domain catalog

Generate a newline-delimited list of current Emailqu public, non-hidden apex domains. Verify each candidate through Emailqu before placing it in the lane catalog.

Example location:

```text
/home/auto/emailqu-candidates.txt
```

The file is runtime input and should not contain credentials.

### Restricted NvRouter importer

Generate a dedicated key on the farm host:

```bash
su - auto -c '
  install -d -m 700 ~/.ssh
  ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/nvrouter_push
  chmod 600 ~/.ssh/nvrouter_push
'
```

Install the repository importer on the router. It reads JSON from stdin and calls NvRouter's loopback foreign-import API:

```bash
install -m 0755 \
  /path/to/Automation/scripts/enter_nvrouter_import.py \
  /usr/local/bin/enter-nvrouter-import
```

The importer defaults to `~/.keirouter/keirouter.db` and `http://127.0.0.1:20180`. Override `NVROUTER_DB` or `NVROUTER_BASE_URL` in its forced-command wrapper if the router uses different paths.

Authorize the farm public key with `restrict` and the importer as its forced command. Do not copy a router private key to the farm.

Conceptual `authorized_keys` entry on the router:

```text
restrict,command="/usr/local/bin/enter-nvrouter-import" ssh-ed25519 <farm-public-key>
```

Bootstrap host-key verification before unattended startup:

```bash
su - auto -c '
  ssh-keyscan -H <router-host> >> ~/.ssh/known_hosts
  chmod 600 ~/.ssh/known_hosts
'
```

Verify the imported host key through a trusted channel before accepting it. Then run an empty-payload smoke through the restricted key:

```bash
printf '%s' '{"credentials":[],"provider":"enter-converge"}' | \
  ssh -i /home/auto/.ssh/nvrouter_push \
  <restricted-import-user>@<router-host> \
  <forced-import-command>
```

The expected response is:

```json
{"ok": true, "accounts": 0, "skipped": 0}
```

### Lane launcher template

Keep launchers outside the Git checkout with mode `0700`:

```bash
#!/usr/bin/env bash
exec python3 /home/auto/Automation/farms/enter/lane_supervisor.py \
  --lane lane-a \
  --gift <gift-code-a> \
  --inviter '<inviter-a>' \
  --proxy socks5://127.0.0.1:40001 \
  --prefix lanea \
  --domains /home/auto/emailqu-candidates.txt \
  --preferred <known-good-domain-a> \
  --gap 60 \
  --cooldown 900
```

Create a second launcher with a different lane name, referral, sticky proxy, prefix, and preferably a different preferred domain.

### Tests

```bash
cd /home/auto/Automation
.venv/bin/python -m unittest -q \
  farms.enter.test_lane_supervisor \
  farms.enter.test_auth_gateway \
  farms.enter.test_post_auth \
  core.test_ninerouter \
  scripts.test_enter_nvrouter_import
.venv/bin/python -m py_compile \
  farms/enter/farm.py \
  farms/enter/lane_supervisor.py \
  scripts/enter_nvrouter_import.py
```

### Canary gate

Run one serial account on one lane. Promote only after:

```text
OTP found
callback_return
authenticated gateway session
API key created
OK
NvRouter pushed 1 account
```

Then start a second isolated lane and require both lanes to produce a real success while running concurrently.

## Starting and stopping lanes

Start each lane in a separate tmux session:

```bash
su - auto -c 'tmux new-session -d -s enter-a /home/auto/run-enter-supervisor-a.sh'
su - auto -c 'tmux new-session -d -s enter-b /home/auto/run-enter-supervisor-b.sh'
```

Stop without touching unrelated farms. First let the current child finish so `NinerouterPusher.flush()` can deliver its final batch. Then terminate the supervisor during its lane gap:

```bash
su - auto -c 'tmux send-keys -t enter-a C-c'
su - auto -c 'tmux send-keys -t enter-b C-c'
```

If a lane does not exit after the current account timeout, escalate deliberately:

```bash
su - auto -c 'tmux kill-session -t enter-a' 2>/dev/null || true
su - auto -c 'tmux kill-session -t enter-b' 2>/dev/null || true
pkill -u auto -f 'farms/enter/lane_supervisor.py|farms/enter/farm.py|camoufox-bin' || true
```

A forced stop can lose credentials that were queued only in memory after a failed or incomplete NvRouter push. Before deleting a disposable VPS, inspect successful local result files and replay them through the restricted importer. Never print the credential payload while recovering it.

Verify cleanup:

```bash
pgrep -u auto -af 'lane_supervisor.py|farms/enter/farm.py|camoufox-bin' || true
```

## Monitoring

Treat these as separate health signals:

- VPS reachable;
- supervisors running;
- child farm/browser currently active;
- completed OK/FAIL outcomes;
- NvRouter count and last push time.

Supervisors intentionally have idle periods between child attempts. Monitoring must count the supervisor process, otherwise the dashboard will flicker to `STOPPED` during the lane gap.

For multiple lanes on one VPS, aggregate:

```text
concurrency = number of lane supervisors
OK          = sum across production-lane-*.log
FAIL        = sum across production-lane-*.log
```

Never expose raw logs through the dashboard. Raw logs may contain disposable addresses and operational identifiers.

## Failure guide

### `risk-aware gateway login not observed`

Check that the invitation dialog is closed and the official CTA is visible. Verify FingerprintJS POST, risk-session POST, and `/auth/login?risk_session_id=...` in that order.

Do not blacklist the mailbox domain.

### Callback `access_denied`

The signup reached the private authorization policy and was rejected. Cool down that domain within the lane and try another candidate. Do not add it to the permanent blocklist.

Repeated denials across many domains may indicate lane/referral velocity rather than domain quality.

### Explicit `domain_not_allowed`

Persist the domain to the blocked-domain file and do not retry it. This is the only class that automatically creates a permanent domain block.

### OTP timeout

Retry with a new address or domain. Do not permanently block the domain from one timeout.

### Turnstile token missing

Stop before mailbox polling. Check the browser, proxy, and challenge state. This is not a domain result.

### `Target closed` after an OK

A Playwright response-finished callback may log `Target closed` while the browser is shutting down. If the same attempt already emitted `API key created`, `OK`, and a successful push, this warning is cleanup noise rather than account failure. Still track it separately so a real premature browser close is not hidden.

### Referral claim 403

Signup may still be valid. Record referral reward failure separately and continue only according to the current product-client behavior. Do not claim that credits were awarded unless the claim and credit balance were verified.

### SSH or VPS offline

Do not infer farm state from stale dashboard data. Check ICMP/TCP reachability and provider power state. Disposable VPS replacement must start from the Git commit plus separately transferred runtime configuration; never copy result directories, browser caches, or an old virtual environment.

## Security and data handling

- Never commit `.env`, passwords, API keys, OAuth tokens, mailbox messages, OTPs, or private SSH keys.
- Keep the NvRouter private key mode at `0600`.
- Use a forced-command public key on the router.
- Report domains and category counts, not full mailbox addresses.
- Keep raw failure artifacts private.
- Do not run `git add .` on a live farm checkout.
- Keep runtime lane state and logs ignored by Git.
- Set result, state, and log directories to mode `0700`; set credential-bearing files to `0600`.
- Retain local results only until NvRouter import is verified and any required backup is encrypted. Delete stale screenshots, raw failure artifacts, and disposable mailbox records on a defined schedule.
- Treat any credential found in Git history as exposed. Redacting the current tree does not revoke or erase it; rotate the credential and, if required, rewrite repository history separately.

## Operational checklist

Before starting:

```text
[ ] checkout matches the approved pinned commit SHA
[ ] tests and py_compile pass
[ ] Emailqu domain catalog is current
[ ] permanent blocklist is loaded
[ ] each lane has a distinct referral and proxy
[ ] WARP ports reach Enter
[ ] NvRouter empty importer smoke passes
[ ] no stale farm or Camoufox process exists
```

After starting:

```text
[ ] one supervisor process per lane
[ ] no lane has internal concurrency above one
[ ] first terminal outcomes are classified correctly
[ ] explicit domain blocks are persisted
[ ] ambiguous failures only create lane cooldowns
[ ] at least one real OK and NvRouter import is observed
[ ] dashboard aggregates all lanes without exposing logs
```

## Related documentation

- `API.md`: using an already-created Enter API key.
- `HTTP_MIGRATION.md`: historical HTTP-auth experiments, not the current production auth path.
- `VPS-DEPLOY.md`: historical single-process deployment notes; prefer this document.
- `CHANGELOG.md`: historical implementation timeline.
