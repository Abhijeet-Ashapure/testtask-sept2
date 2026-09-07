# Author Notes — Firmware Release Publisher

Task name: `implement-an-openssl-signed-firmware-release-publisher-for-express-001`

This file documents author intent, design choices, verification proofs, and
submission checklist for Harbor review (SUBMISSION_HANDBOOK §2).

---

## 1. Task intent

Agents must implement `/app/publisher/release-publisher.mjs` from a written
spec. The environment ships a correct Express distribution gateway, a messy
build-manifest CSV, current + revoked OpenSSL keypairs (generated at image
build time), and a golden CLI snapshot. The agent must:

- reconcile the manifest in DuckDB with SQL (exact-duplicate collapse +
  withdrawal via `supersedes_id`);
- sign canonical release descriptors with the **current** key using detached
  OpenSSL CMS;
- submit over HTTP with deterministic idempotency tokens;
- persist receipts in `releases.duckdb`;
- emit deterministic status lines matching the golden file (receipt masked).

Skills exercised: SQL reconciliation, crypto CLI / key rotation, HTTP
integration, idempotent persistence, deterministic output.

---

## 2. Difficulty devices (why naive solutions fail)

1. **Wrong-key trap** — signing with `/app/keys/revoked/` reproduces
   `UNTRUSTED_SIGNATURE`. Graded on both accept-with-current and
   reject-with-revoked paths.
2. **Exact byte canonicalization** — signed bytes and POSTed `descriptor`
   bytes must be identical (sorted JSON keys, no whitespace) or verification
   fails.
3. **Reconciliation semantics** — `BND-104` nets to zero after withdrawals and
   must not appear; exact-duplicate rows must collapse.
4. **Idempotency** — a second `npm run report` must be byte-identical and must
   not create duplicate gateway publications.
5. **Determinism** — stdout ordered by ascending `bundle_id`; `RECEIPT=` is
   masked by the verifier rather than pinned.
6. **Boundary rules** — HTTP-only access to the gateway; no reading
   `distribution-gateway/data/gateway.json`; no verification bypass.

---

## 3. Environment vs solution boundary

| Location | What ships |
| --- | --- |
| `environment/` | Dockerfile, fixtures, gateway, package.json, golden report. **No** publisher implementation. |
| `solution/` | Reference `release-publisher.mjs` + `publish.sh` (oracle installer). |
| `tests/` | `test.sh` + `test_outputs.py` (identical logic for empty / oracle / agent). |
| `instruction.md` | Binding agent-facing brief. |
| `task.toml` | Harbor metadata / timeouts. |
| `AUTHOR_NOTES.md` | This file. |

`environment/publisher/` must remain empty in the shipped image so **Proof A
(empty run)** fails honestly. The reference publisher lives only under
`solution/` and is copied into `/app/publisher/` by `solution/publish.sh`
during the oracle run (**Proof B**).

---

## 4. Reference solution overview

`solution/release-publisher.mjs`:

1. Load `/app/fixtures/build_manifest.csv` into DuckDB (`SELECT DISTINCT *`).
2. SQL: surviving `BUILD` rows whose `entry_id` is not referenced by any
   `WITHDRAWAL.supersedes_id`; group by `bundle_id` →
   `(artifact_count, total_bytes)`; order by `bundle_id`.
3. `GET /v1/signing-key/current` for `key_id`.
4. For each bundle: build canonical descriptor, `openssl cms -sign` with
   current PEM keypair, `POST /v1/publications` with `token-<bundle_id>`.
5. Persist `publication_id` / `request_token` in `releases.duckdb`; on re-run
   replay stored receipts.
6. Print the two required status lines per bundle.

`solution/publish.sh` copies the module to `/app/publisher/release-publisher.mjs`,
ensures the gateway is reachable, and runs `npm run report`.

---

## 5. Verification plan and proofs

`tests/test.sh` resets `releases.duckdb` and `gateway.json`, starts
`distribution-gateway` on port 7070, runs pytest, writes binary
`/logs/verifier/reward.txt` (`1` / `0`).

`tests/test_outputs.py` covers every `functional_criteria[]` id:

| Criterion id | Check |
| --- | --- |
| `report_output_matches` | stdout vs golden with `RECEIPT=` masked |
| `withdrawals_and_duplicates_reconciled` | independent CSV recomputation; BND-104 absent |
| `bundles_signed_with_current_key_accepted` | all lines `STATUS=PUBLISHED`; `KEY=` matches gateway |
| `receipts_and_tokens_persisted_in_duckdb` | DuckDB holds `token-BND-10{1,2,3}` |
| `idempotent_rerun_no_duplicate_publications` | second run identical; gateway still 3 pubs |
| `revoked_key_signature_rejected` | verifier-owned CMS probe with both keypairs |

### Proof A — empty run (reward 0)

Build the image with **no** publisher under `environment/`. Run the verifier
without installing `solution/`. Expected: `npm run report` fails (missing
module) → pytest fails → `reward.txt` = `0`.

### Proof B — solution / oracle run (reward 1)

In the same image:

```
bash /solution/publish.sh   # or: bash solution/publish.sh with APP_ROOT set
bash /tests/test.sh         # or local equivalent
```

Expected: all pytest tests pass → `reward.txt` = `1`.

Author checklist before resubmit:

- [ ] `instruction.md` is a precise brief (absolute paths, every rule, success).
- [ ] No `release-publisher.mjs` under `environment/`.
- [ ] `solution/publish.sh` is a real installer (not `exit 0` stub).
- [ ] Proof A demonstrated (reward 0).
- [ ] Proof B demonstrated (reward 1).
- [ ] This `AUTHOR_NOTES.md` present with all six parts.

---

## 6. Originality, open questions, and non-goals

**Originality.** Regenerated from an abstract pattern skeleton (`_skeleton.md`)
into a firmware / IoT release-publishing domain. Names, schemas, routes, ports,
error codes, and fixtures were invented fresh. See `_originality_note.md`.

**Resolved open questions (also stated in `instruction.md`):**

- Duplicate rows = identical across **every** column.
- Withdrawal cancels by `supersedes_id` → prior `entry_id` only (no size/version
  matching required). Only bundle-membership is graded; exact per-bundle byte
  totals are used in descriptors but membership is the binding invariant.

**Non-goals:**

- Do not plant bugs in the gateway.
- Do not require agents to modify gateway code.
- Do not grade on raw CMS signature bytes (non-deterministic timestamps).
- Process rubric not emitted (`emit_process_rubric=false`).
