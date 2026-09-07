"""Verifier tests for the firmware release publisher task.

Each test maps to a functional_criteria[] entry in scaffold_plan.yaml. The suite
assumes tests/test.sh has already started the distribution gateway on port 7070
and reset releases.duckdb / gateway.json.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import duckdb
import pytest
import requests

APP_ROOT = Path(os.environ.get("APP_ROOT", "/app"))
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:7070")
MANIFEST_PATH = APP_ROOT / "fixtures" / "build_manifest.csv"
EXPECTED_PATH = APP_ROOT / "reports" / "publications.expected.txt"
DB_PATH = APP_ROOT / "releases.duckdb"
PUBLISHER_PATH = APP_ROOT / "publisher" / "release-publisher.mjs"
CURRENT_CERT = APP_ROOT / "keys" / "current" / "current.cert.pem"
CURRENT_KEY = APP_ROOT / "keys" / "current" / "current.key.pem"
REVOKED_CERT = APP_ROOT / "keys" / "revoked" / "revoked.cert.pem"
REVOKED_KEY = APP_ROOT / "keys" / "revoked" / "revoked.key.pem"
GATEWAY_LEDGER = APP_ROOT / "distribution-gateway" / "data" / "gateway.json"

RECEIPT_RE = re.compile(r"RECEIPT=[^ ]+")


def mask_receipts(text: str) -> str:
    return RECEIPT_RE.sub("RECEIPT=<id>", text)


def canonical_encode(value):
    if isinstance(value, list):
        return "[" + ",".join(canonical_encode(v) for v in value) + "]"
    if isinstance(value, dict):
        parts = [
            json.dumps(k) + ":" + canonical_encode(value[k])
            for k in sorted(value.keys())
        ]
        return "{" + ",".join(parts) + "}"
    return json.dumps(value, separators=(",", ":"))


def reconcile_publishable_bundles():
    """Independent recomputation of publishable bundle ids from the CSV."""
    rows = list(csv.DictReader(MANIFEST_PATH.open(newline="", encoding="utf-8")))
    # Collapse exact duplicates across every column.
    unique = []
    seen = set()
    for row in rows:
        key = tuple(row[col] for col in row.keys())
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    withdrawn = {
        r["supersedes_id"]
        for r in unique
        if r["record_type"] == "WITHDRAWAL" and r.get("supersedes_id")
    }

    surviving = [
        r
        for r in unique
        if r["record_type"] == "BUILD" and r["entry_id"] not in withdrawn
    ]

    by_bundle: dict[str, list] = {}
    for r in surviving:
        by_bundle.setdefault(r["bundle_id"], []).append(r)

    result = []
    for bundle_id in sorted(by_bundle.keys()):
        builds = by_bundle[bundle_id]
        result.append(
            {
                "bundle_id": bundle_id,
                "artifact_count": len(builds),
                "total_bytes": sum(int(b["size_bytes"]) for b in builds),
            }
        )
    return result


def run_report() -> subprocess.CompletedProcess[str]:
    # Invoke the graded entry point directly to avoid npm banner noise that
    # varies by npm version; package.json still maps `npm run report` here.
    return subprocess.run(
        ["node", "publisher/release-publisher.mjs", "--report"],
        cwd=str(APP_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def openssl_cms_sign(cert: Path, key: Path, payload: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        content = Path(tmp) / "descriptor.bin"
        content.write_text(payload, encoding="utf-8")
        proc = subprocess.run(
            [
                "openssl",
                "cms",
                "-sign",
                "-in",
                str(content),
                "-signer",
                str(cert),
                "-inkey",
                str(key),
                "-outform",
                "PEM",
                "-binary",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout


def read_gateway_publication_count() -> int:
    if not GATEWAY_LEDGER.exists():
        return 0
    data = json.loads(GATEWAY_LEDGER.read_text(encoding="utf-8"))
    return len(data.get("publications") or {})


@pytest.fixture(scope="module")
def report_result():
    assert PUBLISHER_PATH.is_file(), (
        f"missing deliverable {PUBLISHER_PATH} — empty environment must not "
        "ship the publisher under environment/"
    )
    # Fresh DB for the graded report run. Do NOT delete gateway.json here: the
    # gateway process has already hydrated; deleting the file under its feet
    # desyncs memory from disk and breaks ledger-based assertions.
    for path in (DB_PATH, Path(str(DB_PATH) + ".wal")):
        if path.exists():
            path.unlink()
    assert not DB_PATH.exists(), f"failed to clear {DB_PATH}"

    result = run_report()
    assert result.returncode == 0, (
        "npm run report failed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _parse_published_receipts(stdout: str) -> list[tuple[str, str, str]]:
    """Return (bundle_id, publication_id, request_token) from PUBLISHED lines."""
    rows = []
    for line in stdout.splitlines():
        if " PUBLISHED RECEIPT=" not in line:
            continue
        parts = line.split()
        # BUNDLE <id> PUBLISHED RECEIPT=<pub> TOKEN=<tok> STATUS=PUBLISHED
        bundle_id = parts[1]
        receipt = next(p.split("=", 1)[1] for p in parts if p.startswith("RECEIPT="))
        token = next(p.split("=", 1)[1] for p in parts if p.startswith("TOKEN="))
        rows.append((bundle_id, receipt, token))
    return rows


def test_report_output_matches(report_result):
    """functional_criteria[id=report_output_matches]"""
    expected = EXPECTED_PATH.read_text(encoding="utf-8")
    actual = report_result.stdout
    assert mask_receipts(actual) == mask_receipts(expected)


def test_withdrawals_and_duplicates_reconciled(report_result):
    """functional_criteria[id=withdrawals_and_duplicates_reconciled]"""
    expected_bundles = [b["bundle_id"] for b in reconcile_publishable_bundles()]
    # Fully withdrawn BND-104 must be absent; BND-101/102/103 present.
    assert expected_bundles == ["BND-101", "BND-102", "BND-103"]

    lines = [
        line
        for line in report_result.stdout.splitlines()
        if line.startswith("BUNDLE ") and " SIGNED KEY=" in line
    ]
    reported = [line.split()[1] for line in lines]
    assert reported == expected_bundles


def test_bundles_signed_with_current_key_accepted(report_result):
    """functional_criteria[id=bundles_signed_with_current_key_accepted]"""
    assert "UNTRUSTED_SIGNATURE" not in report_result.stdout
    assert "UNTRUSTED_SIGNATURE" not in report_result.stderr
    for line in report_result.stdout.splitlines():
        if "PUBLISHED RECEIPT=" in line:
            assert line.endswith("STATUS=PUBLISHED")

    meta = requests.get(f"{GATEWAY_URL}/v1/signing-key/current", timeout=5)
    meta.raise_for_status()
    key_id = meta.json()["key_id"]
    for line in report_result.stdout.splitlines():
        if " SIGNED KEY=" in line:
            assert line.endswith(f"KEY={key_id}")


def test_receipts_and_tokens_persisted_in_duckdb(report_result):
    """functional_criteria[id=receipts_and_tokens_persisted_in_duckdb]"""
    assert DB_PATH.is_file(), "releases.duckdb was not created"
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        assert "publications" in tables or len(tables) >= 1

        # Prefer a publications table; otherwise scan all tables for token columns.
        rows = []
        if "publications" in tables:
            rows = con.execute(
                "SELECT * FROM publications ORDER BY 1"
            ).fetchall()
        else:
            for table in tables:
                cols = [
                    c[0]
                    for c in con.execute(f"DESCRIBE {table}").fetchall()
                ]
                if any("token" in c.lower() for c in cols):
                    rows = con.execute(f"SELECT * FROM {table}").fetchall()
                    break

        assert len(rows) >= 3, f"expected persisted receipts, got: {rows}"

        flat = " ".join(str(cell) for row in rows for cell in row)
        for bundle_id in ("BND-101", "BND-102", "BND-103"):
            assert f"token-{bundle_id}" in flat
    finally:
        con.close()


def test_idempotent_rerun_no_duplicate_publications(report_result):
    """functional_criteria[id=idempotent_rerun_no_duplicate_publications]"""
    published = _parse_published_receipts(report_result.stdout)
    assert [b for b, _, _ in published] == ["BND-101", "BND-102", "BND-103"]

    # Tokens must be live on the gateway (proves the first run actually POSTed,
    # not merely replayed from a stale local DB). Replay ignores signature.
    for _bundle_id, publication_id, token in published:
        replay = requests.post(
            f"{GATEWAY_URL}/v1/publications",
            json={
                "descriptor": "{}",
                "signature": "not-checked-on-replay",
                "request_token": token,
            },
            timeout=10,
        )
        assert replay.status_code == 200, replay.text
        body = replay.json()
        assert body["publication_id"] == publication_id
        assert body["request_token"] == token
        assert body["status"] == "PUBLISHED"

    count_before = read_gateway_publication_count()
    assert count_before >= 3

    second = run_report()
    assert second.returncode == 0, second.stderr
    assert second.stdout == report_result.stdout

    count_after = read_gateway_publication_count()
    assert count_after == count_before


def test_revoked_key_signature_rejected():
    """functional_criteria[id=revoked_key_signature_rejected]"""
    assert CURRENT_CERT.is_file() and REVOKED_CERT.is_file()

    descriptor = canonical_encode(
        {
            "artifact_count": 1,
            "bundle_id": "BND-VERIFIER-REVOKED",
            "total_bytes": 42,
        }
    )
    bad_sig = openssl_cms_sign(REVOKED_CERT, REVOKED_KEY, descriptor)
    good_sig = openssl_cms_sign(CURRENT_CERT, CURRENT_KEY, descriptor)

    rejected = requests.post(
        f"{GATEWAY_URL}/v1/publications",
        json={
            "descriptor": descriptor,
            "signature": bad_sig,
            "request_token": "token-verifier-revoked-probe",
        },
        timeout=10,
    )
    assert rejected.status_code == 400
    assert rejected.json().get("error") == "UNTRUSTED_SIGNATURE"

    accepted = requests.post(
        f"{GATEWAY_URL}/v1/publications",
        json={
            "descriptor": descriptor,
            "signature": good_sig,
            "request_token": "token-verifier-current-probe",
        },
        timeout=10,
    )
    assert accepted.status_code == 200
    assert accepted.json().get("status") == "PUBLISHED"
