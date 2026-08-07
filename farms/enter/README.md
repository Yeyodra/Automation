# Enter / Converge farm

The Enter farm creates a mailbox, completes the official browser signup flow, creates an `ek_` API key, and sends the finished credential to NvRouter.

Read [OPERATIONS.md](OPERATIONS.md) before deploying or changing production. It is the current source of truth for:

- the referral, FingerprintJS, risk-session, Auth0, callback, and post-auth flow;
- Emailqu mailbox and domain handling;
- permanent blacklist versus temporary lane cooldown rules;
- isolated multi-lane production;
- WARP proxy affinity;
- native key-only NvRouter delivery;
- fresh VPS setup, monitoring, and troubleshooting.

`VPS-DEPLOY.md`, `HTTP_MIGRATION.md`, and parts of `CHANGELOG.md` contain historical experiments. Do not copy old host addresses, passwords, Xvfb commands, legacy 9router settings, or single-identity c3/c4 recommendations from those files.

## Current entry points

```text
farm.py             One account flow and direct single-process runner
lane_supervisor.py  Serial lane with domain rotation and strict block attribution
```

Run tests from the repository root:

```bash
python3 -m unittest -q \
  farms.enter.test_lane_supervisor \
  farms.enter.test_auth_gateway \
  farms.enter.test_post_auth \
  core.test_ninerouter
```

Basic single-account dry run:

```bash
python3 -m jobs run enter --dry-run --warp-every-n 1 -- -n 1 -c 1 -y --headless
```

A production lane must keep internal concurrency at one. Aggregate c2 or higher by running multiple isolated lane supervisors with different referrals and sticky proxies, not by raising one farm process to `-c 2` or `-c 4`.
