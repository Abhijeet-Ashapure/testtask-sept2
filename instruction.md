# Firmware Release Publisher

Release engineering rotated the firmware code-signing key. Bundles signed with
the revoked key are rejected by the distribution gateway with
`UNTRUSTED_SIGNATURE`. Implement a publisher that reconciles the build
manifest, signs with the **current** key, submits to the gateway, persists
receipts, and prints deterministic status lines.

## Deliverable

Create exactly this file:

```
/app/publisher/release-publisher.mjs
```

Run it with:

```
cd /app && npm run report
```

(`npm run report` is defined as `node publisher/release-publisher.mjs --report`.)

## Inputs and paths (absolute, inside the container)

| Path | Role |
| --- | --- |
| `/app/fixtures/build_manifest.csv` | Raw build manifest (CSV) to import and reconcile |
| `/app/reports/publications.expected.txt` | Golden CLI output your program must reproduce |
| `/app/releases.duckdb` | DuckDB file **you create** at runtime (do not pre-ship) |
| `/app/keys/current/current.key.pem` | Current private key (sign with this) |
| `/app/keys/current/current.cert.pem` | Current certificate (OpenSSL `-signer`) |
| `/app/keys/revoked/` | Revoked keypair — **do not sign with these** |
| `/app/distribution-gateway/` | Provided Express service — **do not modify** |
| `http://127.0.0.1:7070` | Gateway base URL |

Manifest columns:

```
entry_id,bundle_id,component_id,version,size_bytes,record_type,supersedes_id,recorded_at
```

`record_type` is `BUILD` or `WITHDRAWAL`. A `WITHDRAWAL` cancels the `BUILD`
whose `entry_id` equals the withdrawal's `supersedes_id`.

## Reconciliation rules (SQL over DuckDB)

Derive publishable bundles with SQL after loading the CSV into DuckDB:

1. **Collapse exact duplicates.** Rows identical across **every** column are the
   same record emitted twice — count them once.
2. **Apply withdrawals.** A `BUILD` whose `entry_id` appears as `supersedes_id`
   on any `WITHDRAWAL` is cancelled and must not contribute to any release.
3. A bundle is **publishable** if, after (1) and (2), it still has at least one
   surviving `BUILD`. A bundle whose every build was withdrawn is skipped
   entirely (it must not appear in stdout or be submitted).

For each publishable bundle compute:

- `artifact_count` — number of surviving builds
- `total_bytes` — sum of those builds' `size_bytes`

Process bundles in ascending `bundle_id` order.

## Canonical release descriptor

For each publishable bundle, the signed descriptor is UTF-8 JSON with
**lexicographically sorted object keys** and **no insignificant whitespace**.
The exact bytes you sign must be the exact bytes you send as `descriptor`.

Shape (keys sorted):

```json
{"artifact_count":<int>,"bundle_id":"<id>","total_bytes":<int>}
```

Example:

```json
{"artifact_count":1,"bundle_id":"BND-TEST","total_bytes":100}
```

## Signing (OpenSSL CMS)

Produce a detached CMS signature (PEM) over the canonical descriptor bytes:

```
openssl cms -sign -in <descriptor.bin> \
  -signer /app/keys/current/current.cert.pem \
  -inkey  /app/keys/current/current.key.pem \
  -outform PEM -binary
```

Signing with `/app/keys/revoked/` must not be used; the gateway rejects those
signatures as `UNTRUSTED_SIGNATURE`.

## Gateway contract

Start the provided gateway before publishing:

```
cd /app/distribution-gateway && node server.js
```

It listens on port `7070`.

1. `GET /v1/signing-key/current` →
   `{ key_id, algorithm, certificate_ref, status }`
   Use the returned `key_id` in your stdout lines.

2. `POST /v1/publications` with JSON body:
   ```json
   {
     "descriptor": "<canonical descriptor string>",
     "signature": "<detached CMS signature PEM>",
     "request_token": "<client token>"
   }
   ```
   Success: `{ publication_id, request_token, status: "PUBLISHED" }`.
   Failure (wrong key): `{ error: "UNTRUSTED_SIGNATURE" }`.

Use the deterministic request token `token-<bundle_id>` (e.g. `token-BND-101`).
Re-posting the same `request_token` replays the original receipt without creating
a duplicate publication.

Interact with the gateway **only over HTTP**. Do not read or write
`/app/distribution-gateway/data/gateway.json`. Do not disable or bypass
signature verification.

## Persistence and idempotency

Store each bundle's `request_token`, `publication_id`, and enough state in
`/app/releases.duckdb` so a second `npm run report` run:

- reuses stored receipts instead of creating new publications when possible, and
- produces **byte-identical** stdout to the first successful run.

## Required stdout

Emit exactly two lines per publishable bundle, ordered by ascending
`bundle_id`:

```
BUNDLE <bundle_id> SIGNED KEY=<key_id>
BUNDLE <bundle_id> PUBLISHED RECEIPT=<publication_id> TOKEN=<request_token> STATUS=PUBLISHED
```

`<key_id>` is the value from `GET /v1/signing-key/current`.
`<request_token>` must be `token-<bundle_id>`.
`<publication_id>` comes from the gateway receipt (it is non-deterministic;
graders mask it when comparing to the golden file).

Your stdout (with `RECEIPT=…` masked) must match
`/app/reports/publications.expected.txt`.

## Success condition

The task is complete when all of the following hold:

1. `/app/publisher/release-publisher.mjs` exists and `cd /app && npm run report`
   succeeds.
2. Stdout matches `/app/reports/publications.expected.txt` after masking
   `RECEIPT=<id>` values.
3. Publishable bundle membership reflects the reconciled manifest (withdrawals
   and exact duplicates handled; fully withdrawn bundles omitted).
4. Every submitted publication is accepted as `PUBLISHED` (signed with the
   current key — no `UNTRUSTED_SIGNATURE`).
5. `/app/releases.duckdb` contains the receipts and request tokens used.
6. Re-running `npm run report` yields identical stdout and does not create
   duplicate publications on the gateway.

## Hard constraints

- Do not hardcode golden text, receipt ids, or row counts — derive everything
  from the manifest and the live gateway so the program remains correct if the
  fixture changes.
- Do not modify `/app/distribution-gateway/`.
- Do not sign with the revoked key.
- Keep output ordering deterministic (sort by `bundle_id`).
