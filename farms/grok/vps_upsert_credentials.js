#!/usr/bin/env node
/**
 * Upsert grok-cli credentials into 9router SQLite (merge by email).
 * Does NOT replace the whole DB — only rows in the payload.
 *
 * Usage (on VPS):
 *   echo '{"credentials":[...]}' | node vps_upsert_credentials.js
 *   node vps_upsert_credentials.js /path/to/batch.json
 *
 * Env:
 *   NINEROUTER_DB     default ~/.9router/db/data.sqlite
 *   NINEROUTER_SQLITE  default ./node_modules/better-sqlite3 (or sibling)
 */
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');

const HOME = process.env.HOME || os.homedir();
const DB_PATH =
  process.env.NINEROUTER_DB ||
  path.join(HOME, '.9router', 'db', 'data.sqlite');

function loadSqlite() {
  const candidates = [
    process.env.NINEROUTER_SQLITE,
    path.join(__dirname, 'node_modules', 'better-sqlite3'),
    path.join(HOME, 'scripts', 'grok-refresh', 'node_modules', 'better-sqlite3'),
  ].filter(Boolean);
  for (const p of candidates) {
    try {
      return require(p);
    } catch (e) {
      /* try next */
    }
  }
  return require('better-sqlite3');
}

function readPayload() {
  const arg = process.argv[2];
  if (arg && arg !== '-') {
    return JSON.parse(fs.readFileSync(arg, 'utf8'));
  }
  const raw = fs.readFileSync(0, 'utf8');
  if (!raw.trim()) {
    throw new Error('empty stdin — pass JSON file or pipe payload');
  }
  return JSON.parse(raw);
}

function nowIso() {
  return new Date().toISOString();
}

function main() {
  const Database = loadSqlite();
  const payload = readPayload();
  const list = payload.credentials || payload.items || [];
  if (!Array.isArray(list) || list.length === 0) {
    console.log(JSON.stringify({ ok: true, updated: 0, inserted: 0, skipped: 0 }));
    return;
  }

  if (!fs.existsSync(DB_PATH)) {
    throw new Error('DB not found: ' + DB_PATH);
  }

  const db = new Database(DB_PATH);
  db.pragma('journal_mode = WAL');
  db.pragma('busy_timeout = 15000');

  const select = db.prepare(
    "SELECT id, data FROM providerConnections WHERE provider = 'grok-cli' AND lower(email) = lower(?)"
  );
  const update = db.prepare(
    'UPDATE providerConnections SET data = ?, isActive = 1, updatedAt = ?, name = COALESCE(name, ?) WHERE id = ?'
  );
  const insert = db.prepare(
    `INSERT INTO providerConnections
      (id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt)
     VALUES (@id, 'grok-cli', 'oauth', @name, @email, 1, 1, @data, @createdAt, @updatedAt)`
  );

  let updated = 0;
  let inserted = 0;
  let skipped = 0;
  const ts = nowIso();

  const tx = db.transaction(function (creds) {
    for (const c of creds) {
      const email = String(c.email || '').trim();
      const access = c.accessToken || c.access_token || '';
      const refresh = c.refreshToken || c.refresh_token || '';
      if (!email || !access || !refresh) {
        skipped++;
        continue;
      }
      const expiresAt =
        c.expiresAt ||
        c.expires_at ||
        new Date(Date.now() + (c.expiresIn || c.expires_in || 21600) * 1000).toISOString();
      const expiresIn = c.expiresIn || c.expires_in || 21600;
      const scope = c.scope || '';
      const authMethod = c.authMethod || c.auth_mode || 'device_oauth';

      const row = select.get(email);
      if (row) {
        let data;
        try {
          data = JSON.parse(row.data || '{}');
        } catch (e) {
          data = {};
        }
        data.accessToken = access;
        data.refreshToken = refresh;
        data.expiresAt = expiresAt;
        data.expiresIn = expiresIn;
        if (scope) data.scope = scope;
        data.testStatus = 'active';
        data.lastRefreshAt = ts;
        data.lastError = null;
        data.lastErrorAt = null;
        data.errorCode = null;
        data.backoffLevel = 0;
        data.displayName = data.displayName || email;
        Object.keys(data).forEach(function (k) {
          if (String(k).indexOf('modelLock_') === 0) delete data[k];
        });
        const psd = Object.assign({}, data.providerSpecificData || {});
        psd.authMethod = authMethod;
        psd.email = email;
        if (c.idToken || c.id_token) psd.idToken = c.idToken || c.id_token;
        data.providerSpecificData = psd;
        update.run(JSON.stringify(data), ts, email, row.id);
        updated++;
      } else {
        const id = 'grok-sync-' + crypto.randomBytes(8).toString('hex');
        const data = {
          displayName: email,
          accessToken: access,
          refreshToken: refresh,
          expiresAt: expiresAt,
          expiresIn: expiresIn,
          scope: scope,
          testStatus: 'active',
          lastRefreshAt: ts,
          lastError: null,
          lastErrorAt: null,
          providerSpecificData: {
            authMethod: authMethod,
            email: email,
            idToken: c.idToken || c.id_token || null,
          },
        };
        insert.run({
          id: id,
          name: email,
          email: email,
          data: JSON.stringify(data),
          createdAt: ts,
          updatedAt: ts,
        });
        inserted++;
      }
    }
  });

  tx(list);
  db.close();
  console.log(
    JSON.stringify({
      ok: true,
      updated: updated,
      inserted: inserted,
      skipped: skipped,
      total: list.length,
      db: DB_PATH,
    })
  );
}

try {
  main();
} catch (e) {
  console.error(JSON.stringify({ ok: false, error: String(e && e.message ? e.message : e) }));
  process.exit(1);
}
