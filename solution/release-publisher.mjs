#!/usr/bin/env node
/**
 * Reference firmware release publisher.
 * Installed into /app/publisher/release-publisher.mjs by solution/publish.sh.
 */

import duckdb from 'duckdb';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const APP_ROOT = path.resolve(__dirname, '..');
const MANIFEST_PATH = path.join(APP_ROOT, 'fixtures', 'build_manifest.csv');
const DB_PATH = path.join(APP_ROOT, 'releases.duckdb');
const GATEWAY_BASE = process.env.GATEWAY_URL || 'http://127.0.0.1:7070';
const CURRENT_CERT =
  process.env.CURRENT_CERT_PATH ||
  path.join(APP_ROOT, 'keys', 'current', 'current.cert.pem');
const CURRENT_KEY =
  process.env.CURRENT_KEY_PATH ||
  path.join(APP_ROOT, 'keys', 'current', 'current.key.pem');

function runQuery(db, sql) {
  return new Promise((resolve, reject) => {
    db.all(sql, (err, rows) => {
      if (err) reject(err);
      else resolve(rows);
    });
  });
}

function runExec(db, sql) {
  return new Promise((resolve, reject) => {
    db.run(sql, (err) => {
      if (err) reject(err);
      else resolve();
    });
  });
}

function canonicalEncode(value) {
  if (Array.isArray(value)) {
    return '[' + value.map(canonicalEncode).join(',') + ']';
  }
  if (value !== null && typeof value === 'object') {
    const entries = Object.keys(value)
      .sort()
      .map((k) => JSON.stringify(k) + ':' + canonicalEncode(value[k]));
    return '{' + entries.join(',') + '}';
  }
  return JSON.stringify(value);
}

function signDescriptor(descriptor) {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'pub-sign-'));
  const descriptorFile = path.join(scratch, 'descriptor.bin');

  try {
    fs.writeFileSync(descriptorFile, descriptor);
    return execFileSync(
      'openssl',
      [
        'cms',
        '-sign',
        '-in',
        descriptorFile,
        '-signer',
        CURRENT_CERT,
        '-inkey',
        CURRENT_KEY,
        '-outform',
        'PEM',
        '-binary',
      ],
      { encoding: 'utf8' },
    );
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

async function initDatabase(db) {
  await runExec(
    db,
    `CREATE TABLE IF NOT EXISTS publications (
      bundle_id VARCHAR PRIMARY KEY,
      request_token VARCHAR NOT NULL,
      publication_id VARCHAR NOT NULL,
      descriptor VARCHAR NOT NULL
    )`,
  );
}

async function loadAndReconcile(db) {
  const csvPath = MANIFEST_PATH.replace(/\\/g, '/');
  await runExec(
    db,
    `CREATE OR REPLACE TABLE manifest AS
     SELECT DISTINCT *
     FROM read_csv('${csvPath}', header=true, auto_detect=true)`,
  );

  return runQuery(
    db,
    `SELECT
       bundle_id,
       COUNT(*)::INTEGER AS artifact_count,
       SUM(size_bytes)::BIGINT AS total_bytes
     FROM manifest m
     WHERE record_type = 'BUILD'
       AND entry_id NOT IN (
         SELECT supersedes_id
         FROM manifest
         WHERE record_type = 'WITHDRAWAL'
           AND supersedes_id IS NOT NULL
           AND supersedes_id != ''
       )
     GROUP BY bundle_id
     ORDER BY bundle_id`,
  );
}

async function getStoredPublication(db, bundleId) {
  const rows = await runQuery(
    db,
    `SELECT request_token, publication_id, descriptor
     FROM publications
     WHERE bundle_id = '${bundleId.replace(/'/g, "''")}'`,
  );
  return rows[0] ?? null;
}

async function storePublication(db, bundleId, requestToken, publicationId, descriptor) {
  await runExec(
    db,
    `INSERT OR REPLACE INTO publications (bundle_id, request_token, publication_id, descriptor)
     VALUES (
       '${bundleId.replace(/'/g, "''")}',
       '${requestToken.replace(/'/g, "''")}',
       '${publicationId.replace(/'/g, "''")}',
       '${descriptor.replace(/'/g, "''")}'
     )`,
  );
}

async function fetchSigningKey() {
  const res = await fetch(`${GATEWAY_BASE}/v1/signing-key/current`);
  if (!res.ok) {
    throw new Error(`Failed to fetch signing key: ${res.status}`);
  }
  return res.json();
}

async function submitPublication(descriptor, signature, requestToken) {
  const res = await fetch(`${GATEWAY_BASE}/v1/publications`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      descriptor,
      signature,
      request_token: requestToken,
    }),
  });
  const body = await res.json();
  if (!res.ok) {
    throw new Error(body.error || `Publication failed: ${res.status}`);
  }
  return body;
}

async function publishBundle(db, bundle, keyId) {
  const {
    bundle_id: bundleId,
    artifact_count: artifactCount,
    total_bytes: totalBytes,
  } = bundle;
  const requestToken = `token-${bundleId}`;
  const descriptor = canonicalEncode({
    artifact_count: artifactCount,
    bundle_id: bundleId,
    total_bytes: Number(totalBytes),
  });

  console.log(`BUNDLE ${bundleId} SIGNED KEY=${keyId}`);

  const stored = await getStoredPublication(db, bundleId);
  if (stored) {
    console.log(
      `BUNDLE ${bundleId} PUBLISHED RECEIPT=${stored.publication_id} TOKEN=${stored.request_token} STATUS=PUBLISHED`,
    );
    return;
  }

  const signature = signDescriptor(descriptor);
  const receipt = await submitPublication(descriptor, signature, requestToken);
  await storePublication(
    db,
    bundleId,
    receipt.request_token,
    receipt.publication_id,
    descriptor,
  );

  console.log(
    `BUNDLE ${bundleId} PUBLISHED RECEIPT=${receipt.publication_id} TOKEN=${receipt.request_token} STATUS=PUBLISHED`,
  );
}

async function main() {
  if (!process.argv.slice(2).includes('--report')) {
    console.error('Usage: node publisher/release-publisher.mjs --report');
    process.exit(1);
  }

  const database = new duckdb.Database(DB_PATH);
  const db = database.connect();

  try {
    await initDatabase(db);
    const bundles = await loadAndReconcile(db);
    const { key_id: keyId } = await fetchSigningKey();

    for (const bundle of bundles) {
      await publishBundle(db, bundle, keyId);
    }
  } finally {
    db.close();
    database.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
