#!/usr/bin/env node
/**
 * inject-to-9router.js
 * Auto-inject grok-farm results directly into 9router SQLite DB.
 * Usage: node inject-to-9router.js [batch_dir]
 *   - No args: picks latest batch in ./results/
 *   - With arg: uses specified batch folder path
 */

const Database = require('C:\\Users\\Nazril\\AppData\\Roaming\\9router\\runtime\\node_modules\\better-sqlite3');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const DB_PATH = 'C:\\Users\\Nazril\\AppData\\Roaming\\9router\\db\\data.sqlite';
const RESULTS_DIR = path.join(__dirname, 'results');

// ── Helpers ──────────────────────────────────────────────────────────────────

function getLatestBatch() {
  const entries = fs.readdirSync(RESULTS_DIR)
    .filter(n => n.startsWith('batch_'))
    .map(n => ({ name: n, mtime: fs.statSync(path.join(RESULTS_DIR, n)).mtime }))
    .sort((a, b) => b.mtime - a.mtime);
  if (!entries.length) throw new Error('No batch folders found in ' + RESULTS_DIR);
  return path.join(RESULTS_DIR, entries[0].name);
}

function findAccountsJson(batchDir) {
  const files = fs.readdirSync(batchDir).filter(f => f.startsWith('accounts') && f.endsWith('.json'));
  if (!files.length) throw new Error('No accounts*.json in ' + batchDir);
  return path.join(batchDir, files[0]);
}

// ── Main ─────────────────────────────────────────────────────────────────────

const batchDir = process.argv[2] || getLatestBatch();
console.log('Batch dir:', batchDir);

const accountsPath = findAccountsJson(batchDir);
const accounts = JSON.parse(fs.readFileSync(accountsPath, 'utf8'));
console.log(`Found ${accounts.length} account(s) in batch`);

const db = new Database(DB_PATH);

const checkExisting = db.prepare("SELECT id FROM providerConnections WHERE email = ? AND provider = 'grok-cli'");
const insert = db.prepare(`
  INSERT INTO providerConnections (id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt)
  VALUES (@id, @provider, @authType, @name, @email, @priority, @isActive, @data, @createdAt, @updatedAt)
`);

const now = new Date().toISOString();
let injected = 0;
let skipped = 0;

const injectAll = db.transaction(() => {
  for (const acc of accounts) {
    const tokens = acc.tokens;
    if (!tokens) {
      console.log(`  SKIP (no tokens): ${acc.email}`);
      skipped++;
      continue;
    }

    // Check duplicate
    const existing = checkExisting.get(acc.email);
    if (existing) {
      console.log(`  SKIP (already exists): ${acc.email}`);
      skipped++;
      continue;
    }

    const id = 'grok-farm-' + crypto.randomBytes(8).toString('hex');
    const expiresAt = tokens.expires_at || new Date(Date.now() + tokens.expires_in * 1000).toISOString();

    const data = {
      displayName: acc.email,
      accessToken: tokens.access_token,
      refreshToken: tokens.refresh_token,
      expiresAt,
      scope: tokens.scope,
      testStatus: 'untested',
      expiresIn: tokens.expires_in || 21600,
      providerSpecificData: {
        authMethod: tokens.auth_mode || 'oidc',
        idToken: tokens.id_token || null,
        email: acc.email,
        userId: null,
        hasGrokCodeAccess: true,
        subscriptionTier: null
      },
      lastError: null,
      lastErrorAt: null
    };

    insert.run({
      id,
      provider: 'grok-cli',
      authType: 'oauth',
      name: acc.email,
      email: acc.email,
      priority: 1,
      isActive: 1,
      data: JSON.stringify(data),
      createdAt: now,
      updatedAt: now
    });

    console.log(`  INJECTED: ${acc.email} (id=${id})`);
    injected++;
  }
});

injectAll();
db.close();

console.log(`\nDone. Injected: ${injected}, Skipped: ${skipped}`);
