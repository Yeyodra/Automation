'use strict';

/**
 * refresh-grok.js — HTTP-only refresh for still-valid grok-cli refresh tokens.
 *
 * NOT for revoked tokens (invalid_grant) → use reauth_device_oauth.py / HUD grok-reauth.
 *
 * Flow:
 *   1. Read providerConnections WHERE provider=grok-cli
 *   2. Pick rows with refreshToken whose access expires within EXPIRY_BUFFER
 *      (default 90 min — keep ahead of ~6h access lifetime when run every 4h)
 *   3. POST grant_type=refresh_token (minimal body, grok2api-compatible)
 *   4. On OK → write new access/refresh/expiresAt, testStatus=active
 *   5. On invalid_grant → mark unavailable (do NOT delete; reauth job handles that)
 *
 * Usage:
 *   node refresh-grok.js
 *   node refresh-grok.js --all          # try every row with a refreshToken
 *   node refresh-grok.js --dry-run
 *   node refresh-grok.js --buffer-min 90
 */

const https = require('https');
const querystring = require('querystring');
const path = require('path');
const os = require('os');

// ── Paths (9router default install) ──────────────────────────────────────────
const DB_PATH =
  process.env.NINEROUTER_DB ||
  path.join(process.env.APPDATA || '', '9router', 'db', 'data.sqlite');
const SQLITE_MODULE =
  process.env.NINEROUTER_SQLITE ||
  path.join(
    process.env.APPDATA || '',
    '9router',
    'runtime',
    'node_modules',
    'better-sqlite3'
  );

const CLIENT_ID = 'b1a00492-073a-47ea-816f-4c329264a828';
const TOKEN_ENDPOINT = 'https://auth.x.ai/oauth2/token';

const BATCH_SIZE = 15;
const DELAY_MS = 350;
// Access ~6h; scheduler every 4h → refresh when < 90 min left (safe margin)
const DEFAULT_BUFFER_MIN = 90;

// ── CLI ──────────────────────────────────────────────────────────────────────
const argv = process.argv.slice(2);
function hasFlag(name) {
  return argv.includes(name);
}
function flagVal(name, def) {
  const i = argv.indexOf(name);
  if (i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('-')) return argv[i + 1];
  return def;
}

const DRY_RUN = hasFlag('--dry-run');
const ALL = hasFlag('--all');
const BUFFER_MIN = Math.max(
  5,
  parseInt(flagVal('--buffer-min', String(DEFAULT_BUFFER_MIN)), 10) || DEFAULT_BUFFER_MIN
);
const EXPIRY_BUFFER_MS = BUFFER_MIN * 60 * 1000;

// ── Helpers ──────────────────────────────────────────────────────────────────
function sleep(ms) {
  return new Promise(function (resolve) {
    setTimeout(resolve, ms);
  });
}

function now() {
  return new Date().toISOString();
}

function log(msg) {
  console.log('[' + now() + '] ' + msg);
}

function expMs(expiresAt) {
  if (expiresAt == null || expiresAt === '') return null;
  if (typeof expiresAt === 'number') {
    return expiresAt > 1e12 ? expiresAt : expiresAt * 1000;
  }
  var t = new Date(expiresAt).getTime();
  return isNaN(t) ? null : t;
}

/**
 * POST form-encoded body to TOKEN_ENDPOINT.
 */
function postTokenRequest(body) {
  return new Promise(function (resolve, reject) {
    var encoded = querystring.stringify(body);
    var url = new URL(TOKEN_ENDPOINT);
    var options = {
      method: 'POST',
      hostname: url.hostname,
      path: url.pathname,
      port: 443,
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Content-Length': Buffer.byteLength(encoded),
        Accept: 'application/json',
      },
    };

    var req = https.request(options, function (res) {
      var chunks = [];
      res.on('data', function (c) {
        chunks.push(c);
      });
      res.on('end', function () {
        var raw = Buffer.concat(chunks).toString('utf8');
        var parsed;
        try {
          parsed = JSON.parse(raw);
        } catch (e) {
          parsed = { _raw: raw };
        }
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(parsed);
        } else {
          var err = new Error('HTTP ' + res.statusCode);
          err.statusCode = res.statusCode;
          err.body = parsed;
          reject(err);
        }
      });
    });

    req.on('error', reject);
    req.write(encoded);
    req.end();
  });
}

// ── Main ─────────────────────────────────────────────────────────────────────
(async function main() {
  var Database;
  try {
    Database = require(SQLITE_MODULE);
  } catch (e) {
    console.error('Failed to load better-sqlite3 from', SQLITE_MODULE);
    console.error(e.message);
    process.exit(1);
  }

  var db;
  try {
    db = new Database(DB_PATH);
  } catch (e) {
    console.error('Failed to open SQLite DB:', e.message);
    process.exit(1);
  }

  log(
    'GrokTokenRefresh — HTTP only | buffer=' +
      BUFFER_MIN +
      'min | all=' +
      ALL +
      ' | dry=' +
      DRY_RUN
  );
  log('DB: ' + DB_PATH);

  var rows;
  try {
    rows = db
      .prepare(
        "SELECT id, name, email, data FROM providerConnections WHERE provider = 'grok-cli'"
      )
      .all();
  } catch (e) {
    console.error('DB query failed:', e.message);
    db.close();
    process.exit(1);
  }

  var cutoff = Date.now() + EXPIRY_BUFFER_MS;
  var toRefresh = [];
  var skippedFresh = 0;
  var skippedNoRt = 0;
  var skippedUnavailable = 0;

  for (var r = 0; r < rows.length; r++) {
    var row = rows[r];
    var data;
    try {
      data = JSON.parse(row.data);
    } catch (e) {
      continue;
    }
    if (!data.refreshToken) {
      skippedNoRt++;
      continue;
    }
    var st = String(data.testStatus || '').toLowerCase();
    // Skip known-dead unless --all (reauth job should fix those)
    if (!ALL && (st === 'unavailable' || st === 'error')) {
      skippedUnavailable++;
      continue;
    }
    if (ALL) {
      toRefresh.push(row);
      continue;
    }
    var exp = expMs(data.expiresAt);
    if (exp == null || exp <= cutoff) {
      toRefresh.push(row);
    } else {
      skippedFresh++;
    }
  }

  log(
    'Total: ' +
      rows.length +
      ' | To refresh: ' +
      toRefresh.length +
      ' | Skip fresh: ' +
      skippedFresh +
      ' | Skip no RT: ' +
      skippedNoRt +
      ' | Skip unavailable: ' +
      skippedUnavailable
  );

  if (toRefresh.length === 0) {
    log('Nothing to refresh.');
    log(
      'Summary — Refreshed: 0, Dead: 0, Failed: 0, Skipped: ' +
        (skippedFresh + skippedNoRt + skippedUnavailable)
    );
    db.close();
    process.exit(0);
  }

  if (DRY_RUN) {
    for (var d = 0; d < Math.min(15, toRefresh.length); d++) {
      var dr = toRefresh[d];
      var dd;
      try {
        dd = JSON.parse(dr.data);
      } catch (e) {
        dd = {};
      }
      log(
        '  [DRY] ' +
          (dr.email || dr.name) +
          ' exp=' +
          (dd.expiresAt || '?') +
          ' status=' +
          (dd.testStatus || '?')
      );
    }
    if (toRefresh.length > 15) log('  … +' + (toRefresh.length - 15) + ' more');
    db.close();
    process.exit(0);
  }

  var refreshed = 0;
  var dead = 0;
  var failed = 0;
  var anyError = false;

  var updateStmt = db.prepare(
    'UPDATE providerConnections SET data = ?, updatedAt = ? WHERE id = ?'
  );

  for (var batchStart = 0; batchStart < toRefresh.length; batchStart += BATCH_SIZE) {
    var batch = toRefresh.slice(batchStart, batchStart + BATCH_SIZE);

    for (var i = 0; i < batch.length; i++) {
      var brow = batch[i];
      var email = brow.email || brow.name || 'id:' + brow.id;
      var bdata;
      try {
        bdata = JSON.parse(brow.data);
      } catch (e) {
        log('  [SKIP] ' + email + ' — invalid JSON');
        failed++;
        anyError = true;
        continue;
      }

      try {
        log('  [TRY]  ' + email + ' — refresh_token grant…');
        // Minimal body — matches grok2api (no scope / redirect_uri)
        var result = await postTokenRequest({
          client_id: CLIENT_ID,
          grant_type: 'refresh_token',
          refresh_token: bdata.refreshToken,
        });

        var newExpiresAt = result.expires_in
          ? new Date(Date.now() + result.expires_in * 1000).toISOString()
          : bdata.expiresAt;

        bdata.accessToken = result.access_token || bdata.accessToken;
        if (result.refresh_token) {
          bdata.refreshToken = result.refresh_token;
        }
        bdata.expiresAt = newExpiresAt;
        bdata.expiresIn = result.expires_in || bdata.expiresIn || 21600;
        if (result.scope) bdata.scope = result.scope;
        bdata.testStatus = 'active';
        bdata.lastRefreshAt = now();
        bdata.lastError = null;
        bdata.lastErrorAt = null;
        bdata.errorCode = null;
        bdata.backoffLevel = 0;
        // Clear model locks from prior 401 thrash
        Object.keys(bdata).forEach(function (k) {
          if (String(k).indexOf('modelLock_') === 0) delete bdata[k];
        });

        var nowStr = now();
        updateStmt.run(JSON.stringify(bdata), nowStr, brow.id);
        log('  [OK]   ' + email + ' — expiresAt=' + newExpiresAt);
        refreshed++;
      } catch (err) {
        var errMsg = err.message || 'unknown error';
        var isDead = false;
        if (err.statusCode === 401 || err.statusCode === 403) {
          isDead = true;
        } else if (err.body) {
          var errorCode = err.body.error || '';
          var desc = String(err.body.error_description || '').toLowerCase();
          if (
            errorCode === 'invalid_grant' ||
            errorCode === 'invalid_token' ||
            desc.indexOf('revoked') >= 0
          ) {
            isDead = true;
          }
        }

        if (isDead) {
          log(
            '  [DEAD] ' +
              email +
              ' — refresh revoked/invalid (' +
              errMsg +
              ') → use grok-reauth'
          );
          bdata.testStatus = 'unavailable';
          dead++;
        } else {
          log('  [ERR]  ' + email + ' — ' + errMsg);
          failed++;
        }

        bdata.lastError =
          errMsg + (err.body ? ' | ' + JSON.stringify(err.body) : '');
        bdata.lastErrorAt = now();

        try {
          updateStmt.run(JSON.stringify(bdata), now(), brow.id);
        } catch (dbErr) {
          log('  [ERR]  DB update failed for ' + email + ': ' + dbErr.message);
        }
        anyError = true;
      }

      if (i < batch.length - 1 || batchStart + BATCH_SIZE < toRefresh.length) {
        await sleep(DELAY_MS);
      }
    }
  }

  db.close();
  log(
    'Summary — Refreshed: ' +
      refreshed +
      ', Dead: ' +
      dead +
      ', Failed: ' +
      failed +
      ', Host: ' +
      os.hostname()
  );
  // exit 0 if any refresh ok and only dead residuals; exit 1 if hard failures
  process.exit(failed > 0 && refreshed === 0 ? 1 : 0);
})().catch(function (e) {
  console.error('Fatal:', e);
  process.exit(1);
});
