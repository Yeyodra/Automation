'use strict';

const path = require('path');
const https = require('https');
const querystring = require('querystring');

// ── Constants ────────────────────────────────────────────────────────────────
const DB_PATH = 'C:\\Users\\Nazril\\AppData\\Roaming\\9router\\db\\data.sqlite';
const SQLITE_MODULE = 'C:\\Users\\Nazril\\AppData\\Roaming\\9router\\runtime\\node_modules\\better-sqlite3';

const CLIENT_ID = 'b1a00492-073a-47ea-816f-4c329264a828';
const TOKEN_ENDPOINT = 'https://auth.x.ai/oauth2/token';
const SCOPE = 'openid profile email offline_access grok-cli:access api:access conversations:read conversations:write';
const REDIRECT_URI = 'http://127.0.0.1:56121/callback';

const BATCH_SIZE = 10;
const DELAY_MS = 500;
// Refresh accounts expiring within 10 minutes from now
const EXPIRY_BUFFER_MS = 10 * 60 * 1000;

// ── Helpers ──────────────────────────────────────────────────────────────────
function sleep(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}

function now() {
  return new Date().toISOString();
}

function log(msg) {
  console.log('[' + now() + '] ' + msg);
}

/**
 * POST form-encoded body to TOKEN_ENDPOINT, returns parsed JSON response.
 * Rejects on HTTP error or network error.
 */
function postTokenRequest(body) {
  return new Promise(function (resolve, reject) {
    var encoded = querystring.stringify(body);
    var options = {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Content-Length': Buffer.byteLength(encoded),
      },
    };

    var url = new URL(TOKEN_ENDPOINT);
    options.hostname = url.hostname;
    options.path = url.pathname;
    options.port = 443;

    var req = https.request(options, function (res) {
      var chunks = [];
      res.on('data', function (c) { chunks.push(c); });
      res.on('end', function () {
        var raw = Buffer.concat(chunks).toString('utf8');
        var parsed;
        try { parsed = JSON.parse(raw); } catch (e) { parsed = { _raw: raw }; }
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

  // Query all grok-cli accounts that are expired OR expiring within buffer
  var cutoff = Date.now() + EXPIRY_BUFFER_MS; // epoch ms
  var rows;
  try {
    // expiresAt stored as ISO string or epoch ms — handle both formats
    rows = db.prepare(
      "SELECT id, name, email, data FROM providerConnections WHERE provider = 'grok-cli'"
    ).all();
  } catch (e) {
    console.error('DB query failed:', e.message);
    db.close();
    process.exit(1);
  }

  // Filter to only expired/expiring accounts
  var toRefresh = rows.filter(function (row) {
    var data;
    try { data = JSON.parse(row.data); } catch (e) { return false; }
    if (!data.refreshToken) return false;
    if (!data.expiresAt) return true; // unknown expiry — try anyway
    // expiresAt may be ISO string or epoch ms
    var expMs = typeof data.expiresAt === 'number'
      ? data.expiresAt
      : new Date(data.expiresAt).getTime();
    return expMs <= cutoff;
  });

  var skipped = rows.length - toRefresh.length;
  log('Total grok-cli accounts: ' + rows.length + ' | To refresh: ' + toRefresh.length + ' | Skipped (valid): ' + skipped);

  if (toRefresh.length === 0) {
    log('Nothing to refresh.');
    log('Summary — Refreshed: 0, Failed: 0, Skipped: ' + skipped);
    db.close();
    process.exit(0);
  }

  var refreshed = 0;
  var failed = 0;
  var anyError = false;

  var updateStmt = db.prepare(
    "UPDATE providerConnections SET data = ?, updatedAt = ? WHERE id = ?"
  );

  // Process in batches
  for (var batchStart = 0; batchStart < toRefresh.length; batchStart += BATCH_SIZE) {
    var batch = toRefresh.slice(batchStart, batchStart + BATCH_SIZE);

    for (var i = 0; i < batch.length; i++) {
      var row = batch[i];
      var email = row.email || row.name || ('id:' + row.id);
      var data;
      try { data = JSON.parse(row.data); } catch (e) {
        log('  [SKIP] ' + email + ' — invalid JSON in data column');
        failed++;
        anyError = true;
        continue;
      }

      try {
        log('  [TRY]  ' + email + ' — refreshing token...');
        var result = await postTokenRequest({
          client_id: CLIENT_ID,
          grant_type: 'refresh_token',
          refresh_token: data.refreshToken,
          scope: SCOPE,
          redirect_uri: REDIRECT_URI,
        });

        // Success — update stored tokens
        var newExpiresAt = result.expires_in
          ? new Date(Date.now() + result.expires_in * 1000).toISOString()
          : data.expiresAt;

        data.accessToken = result.access_token || data.accessToken;
        data.refreshToken = result.refresh_token || data.refreshToken;
        data.expiresAt = newExpiresAt;
        data.expiresIn = result.expires_in || data.expiresIn;
        data.scope = result.scope || data.scope;
        data.testStatus = 'untested';
        // Clear previous errors on success
        delete data.lastError;
        delete data.lastErrorAt;

        var nowStr = new Date().toISOString();
        updateStmt.run(JSON.stringify(data), nowStr, row.id);
        log('  [OK]   ' + email + ' — refreshed, new expiresAt: ' + newExpiresAt);
        refreshed++;

      } catch (err) {
        // Determine if this is a permanent failure (invalid/expired refresh token)
        var isDead = false;
        var errMsg = err.message || 'unknown error';

        if (err.statusCode === 401 || err.statusCode === 403) {
          isDead = true;
        } else if (err.body) {
          var errorCode = err.body.error || '';
          if (errorCode === 'invalid_grant' || errorCode === 'invalid_token') {
            isDead = true;
          }
        }

        if (isDead) {
          log('  [DEAD] ' + email + ' — refresh_token expired/invalid (' + errMsg + ')');
          data.testStatus = 'unavailable';
        } else {
          log('  [ERR]  ' + email + ' — network/server error (' + errMsg + ')');
          data.testStatus = 'unavailable';
        }

        data.lastError = errMsg + (err.body ? ' | ' + JSON.stringify(err.body) : '');
        data.lastErrorAt = new Date().toISOString();

        var nowStr2 = new Date().toISOString();
        try {
          updateStmt.run(JSON.stringify(data), nowStr2, row.id);
        } catch (dbErr) {
          log('  [ERR]  Failed to update DB for ' + email + ': ' + dbErr.message);
        }

        failed++;
        anyError = true;
      }

      // Delay between requests (skip after last item)
      if (i < batch.length - 1 || batchStart + BATCH_SIZE < toRefresh.length) {
        await sleep(DELAY_MS);
      }
    }
  }

  db.close();

  log('Summary — Refreshed: ' + refreshed + ', Failed: ' + failed + ', Skipped: ' + skipped);
  process.exit(anyError ? 1 : 0);
})();
