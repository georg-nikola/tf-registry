"""Terraform Module Registry — comprehensive smoke tests.

Usage:
  # Against production (via Cloudflare, WARP-safe DNS override applied automatically)
  python tests/test_tf_registry.py

  # Against a custom URL (e.g. kubectl port-forward or local dev server)
  python tests/test_tf_registry.py --url http://localhost:8000

  # Skip frontend Playwright tests
  python tests/test_tf_registry.py --skip-frontend

  # Keep test data after run (useful for debugging)
  python tests/test_tf_registry.py --no-cleanup

Requirements:
  pip install requests playwright
  playwright install chromium
"""

import argparse
import io
import os
import sys
import tarfile
import time

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROD_URL = "https://tf-registry.georg-nikola.com"
PROD_IP = "104.21.12.221"

# Fixed identifiers for the smoke-test module — chosen to avoid collision with
# real data while being obviously synthetic.
TEST_NS = "smoke-test"
TEST_NAME = "test-vpc"
TEST_PROVIDER = "aws"
TEST_VERSION = "9.9.9"

PASS = "\033[92m\u2713\033[0m"
FAIL = "\033[91m\u2717\033[0m"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_api_key() -> str:
    """Return API key from file or environment variable."""
    key_file = os.path.expanduser("~/repos/tf-registry/.api_key")
    try:
        with open(key_file) as fh:
            return fh.read().strip()
    except OSError:
        pass
    return os.getenv("TF_REGISTRY_API_KEY", "")


def _make_archive() -> bytes:
    """Build a minimal valid tar.gz in memory containing main.tf and README.md."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for fname, content in [
            ("main.tf", b"# smoke-test terraform module\n"),
            ("README.md", b"# Test VPC Module\nA smoke test module.\n"),
        ]:
            info = tarfile.TarInfo(name=fname)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _module_path(*parts: str) -> str:
    return "/v1/modules/" + "/".join(parts)


# ---------------------------------------------------------------------------
# Core test runner
# ---------------------------------------------------------------------------


def run_tests(base_url: str, api_key: str, skip_frontend: bool, no_cleanup: bool) -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []
    base_url = base_url.rstrip("/")

    def record(name: str, passed: bool, detail: str = "") -> None:
        icon = PASS if passed else FAIL
        suffix = f"  ({detail})" if detail else ""
        print(f"  {icon} {name}{suffix}")
        results.append((name, passed))

    # Shared sessions
    anon = requests.Session()
    authed = requests.Session()
    authed.headers["Authorization"] = f"Bearer {api_key}"

    # Track whether we actually uploaded the test module so cleanup knows what to do.
    uploaded = False
    # Track whether we need a fresh upload for frontend tests.
    needs_frontend_module = False

    # -----------------------------------------------------------------------
    # [API: Health & Discovery]
    # -----------------------------------------------------------------------
    print("\n[API: Health & Discovery]")

    try:
        r = anon.get(f"{base_url}/api/health", timeout=15)
        ok = r.status_code == 200
        body_ok = r.json() == {"status": "ok"} if ok else False
        record("GET /api/health returns 200 + {\"status\": \"ok\"}", ok and body_ok,
               f"status={r.status_code}" if not ok else "")
    except Exception as exc:
        record("GET /api/health returns 200 + {\"status\": \"ok\"}", False, str(exc)[:80])

    try:
        r = anon.get(f"{base_url}/.well-known/terraform.json", timeout=15)
        ok = r.status_code == 200
        body_ok = r.json() == {"modules.v1": "/v1/modules/"} if ok else False
        record("GET /.well-known/terraform.json returns discovery document", ok and body_ok,
               f"status={r.status_code}" if not ok else "")
    except Exception as exc:
        record("GET /.well-known/terraform.json returns discovery document", False, str(exc)[:80])

    # -----------------------------------------------------------------------
    # [API: Authentication]
    # -----------------------------------------------------------------------
    print("\n[API: Authentication]")

    upload_path = f"{base_url}{_module_path(TEST_NS, TEST_NAME, TEST_PROVIDER, TEST_VERSION)}"
    archive_bytes = _make_archive()

    # Upload without any Authorization header
    try:
        r = anon.post(
            upload_path,
            files={"file": ("module.tar.gz", io.BytesIO(archive_bytes), "application/gzip")},
            timeout=15,
        )
        record("Upload without Authorization header → 422 or 401",
               r.status_code in (401, 422), f"got {r.status_code}")
    except Exception as exc:
        record("Upload without Authorization header → 422 or 401", False, str(exc)[:80])

    # Upload with wrong key
    try:
        r = requests.post(
            upload_path,
            files={"file": ("module.tar.gz", io.BytesIO(archive_bytes), "application/gzip")},
            headers={"Authorization": "Bearer this-is-definitely-wrong"},
            timeout=15,
        )
        record("Upload with wrong API key → 403", r.status_code == 403, f"got {r.status_code}")
    except Exception as exc:
        record("Upload with wrong API key → 403", False, str(exc)[:80])

    # Delete without Authorization header
    try:
        r = anon.delete(upload_path, timeout=15)
        record("Delete without Authorization header → 422 or 401",
               r.status_code in (401, 422), f"got {r.status_code}")
    except Exception as exc:
        record("Delete without Authorization header → 422 or 401", False, str(exc)[:80])

    # Delete with wrong key
    try:
        r = requests.delete(
            upload_path,
            headers={"Authorization": "Bearer this-is-definitely-wrong"},
            timeout=15,
        )
        record("Delete with wrong API key → 403", r.status_code == 403, f"got {r.status_code}")
    except Exception as exc:
        record("Delete with wrong API key → 403", False, str(exc)[:80])

    # -----------------------------------------------------------------------
    # [API: Key Management]
    # -----------------------------------------------------------------------
    print("\n[API: Key Management]")

    created_key_id = None
    created_key_token = None

    # Create a new key
    try:
        r = authed.post(
            f"{base_url}/api/keys",
            json={"name": "smoke-test-key"},
            timeout=15,
        )
        ok = r.status_code == 201
        record("POST /api/keys with valid auth + name → 201", ok, f"got {r.status_code}")
    except Exception as exc:
        record("POST /api/keys with valid auth + name → 201", False, str(exc)[:80])
        r = None
        ok = False

    # Verify response shape
    if ok and r is not None:
        try:
            body = r.json()
            has_fields = all(k in body for k in ("id", "name", "key_prefix", "key"))
            correct_name = body.get("name") == "smoke-test-key"
            no_hash = "key_hash" not in body
            record(
                "Response contains id, name, key_prefix, key (full) — no key_hash",
                has_fields and correct_name and no_hash,
                str({k: body.get(k) for k in ("id", "name", "key_prefix") if k in body}),
            )
            if has_fields:
                created_key_id = body["id"]
                created_key_token = body["key"]
        except Exception as exc:
            record("Response contains id, name, key_prefix, key (full) — no key_hash", False, str(exc)[:80])
    else:
        record("Response contains id, name, key_prefix, key (full) — no key_hash", False, "create failed")

    # List keys — should include the new one
    try:
        r = authed.get(f"{base_url}/api/keys", timeout=15)
        ok = r.status_code == 200
        body = r.json() if ok else {}
        found = created_key_id is not None and any(
            k.get("id") == created_key_id for k in body.get("keys", [])
        )
        # Confirm full key is never in list response
        full_key_absent = created_key_token is not None and all(
            "key" not in k or k.get("key") is None for k in body.get("keys", [])
        )
        record(
            "GET /api/keys → 200, list contains new key (without full key value)",
            ok and found and full_key_absent,
            f"status={r.status_code}, found={found}",
        )
    except Exception as exc:
        record("GET /api/keys → 200, list contains new key (without full key value)", False, str(exc)[:80])

    # Use the new key for an upload, then delete that upload
    new_key_upload_ok = False
    if created_key_token:
        new_key_authed = requests.Session()
        new_key_authed.headers["Authorization"] = f"Bearer {created_key_token}"
        alt_version = "9.9.8"
        alt_path = f"{base_url}{_module_path(TEST_NS, TEST_NAME, TEST_PROVIDER, alt_version)}"
        try:
            r = new_key_authed.post(
                alt_path,
                files={"file": ("module.tar.gz", io.BytesIO(archive_bytes), "application/gzip")},
                timeout=30,
            )
            new_key_upload_ok = r.status_code == 201
            record(
                "New DB key works for module upload → 201",
                new_key_upload_ok,
                f"got {r.status_code}",
            )
        except Exception as exc:
            record("New DB key works for module upload → 201", False, str(exc)[:80])

        # Clean up the test upload made with the new key
        if new_key_upload_ok:
            try:
                authed.delete(alt_path, timeout=15)
            except Exception:
                pass

    # Delete the key
    try:
        r = authed.delete(f"{base_url}/api/keys/{created_key_id}", timeout=15)
        ok = r.status_code == 200
        record("DELETE /api/keys/{id} with valid auth → 200", ok, f"got {r.status_code}")
    except Exception as exc:
        record("DELETE /api/keys/{id} with valid auth → 200", False, str(exc)[:80])

    # Revoked key should no longer work
    if created_key_token:
        revoked_session = requests.Session()
        revoked_session.headers["Authorization"] = f"Bearer {created_key_token}"
        alt_version2 = "9.9.7"
        alt_path2 = f"{base_url}{_module_path(TEST_NS, TEST_NAME, TEST_PROVIDER, alt_version2)}"
        try:
            r = revoked_session.post(
                alt_path2,
                files={"file": ("module.tar.gz", io.BytesIO(archive_bytes), "application/gzip")},
                timeout=15,
            )
            record(
                "Revoked key no longer works for upload → 403",
                r.status_code == 403,
                f"got {r.status_code}",
            )
        except Exception as exc:
            record("Revoked key no longer works for upload → 403", False, str(exc)[:80])
    else:
        record("Revoked key no longer works for upload → 403", False, "key not created")

    # -----------------------------------------------------------------------
    # [API: Module Lifecycle]
    # -----------------------------------------------------------------------
    print("\n[API: Module Lifecycle]")

    # Test 7 — upload
    try:
        r = authed.post(
            upload_path,
            files={"file": ("module.tar.gz", io.BytesIO(archive_bytes), "application/gzip")},
            timeout=30,
        )
        if r.status_code == 201:
            uploaded = True
        record("Upload test module returns 201", r.status_code == 201, f"got {r.status_code}")
    except Exception as exc:
        record("Upload test module returns 201", False, str(exc)[:80])

    # Test 8 — verify upload response fields
    if uploaded:
        try:
            body = r.json()
            fields_ok = all(
                body.get(k) == v
                for k, v in [
                    ("namespace", TEST_NS),
                    ("name", TEST_NAME),
                    ("provider", TEST_PROVIDER),
                    ("version", TEST_VERSION),
                ]
            )
            record("Upload response contains correct namespace/name/provider/version", fields_ok,
                   str({k: body.get(k) for k in ("namespace", "name", "provider", "version")}))
        except Exception as exc:
            record("Upload response contains correct namespace/name/provider/version", False, str(exc)[:80])
    else:
        record("Upload response contains correct namespace/name/provider/version", False, "upload failed")

    # Test 9 — duplicate returns 409
    try:
        r2 = authed.post(
            upload_path,
            files={"file": ("module.tar.gz", io.BytesIO(archive_bytes), "application/gzip")},
            timeout=15,
        )
        record("Uploading same version again returns 409", r2.status_code == 409,
               f"got {r2.status_code}")
    except Exception as exc:
        record("Uploading same version again returns 409", False, str(exc)[:80])

    # Test 10 — versions list
    versions_url = f"{base_url}{_module_path(TEST_NS, TEST_NAME, TEST_PROVIDER, 'versions')}"
    try:
        r = anon.get(versions_url, timeout=15)
        ok = r.status_code == 200
        body = r.json() if ok else {}
        has_version = False
        if ok and "modules" in body and body["modules"]:
            versions_found = [v["version"] for v in body["modules"][0].get("versions", [])]
            has_version = TEST_VERSION in versions_found
        record(
            f"GET /versions returns list including v{TEST_VERSION}",
            ok and has_version,
            f"status={r.status_code}, versions={versions_found if ok else '?'}",
        )
    except Exception as exc:
        record(f"GET /versions returns list including v{TEST_VERSION}", False, str(exc)[:80])

    # Test 11 — latest version
    latest_url = f"{base_url}{_module_path(TEST_NS, TEST_NAME, TEST_PROVIDER)}"
    try:
        r = anon.get(latest_url, timeout=15)
        ok = r.status_code == 200
        body = r.json() if ok else {}
        record(
            f"GET latest returns v{TEST_VERSION}",
            ok and body.get("version") == TEST_VERSION,
            f"status={r.status_code}, version={body.get('version')}",
        )
    except Exception as exc:
        record(f"GET latest returns v{TEST_VERSION}", False, str(exc)[:80])

    # Test 12 — specific version
    specific_url = f"{base_url}{_module_path(TEST_NS, TEST_NAME, TEST_PROVIDER, TEST_VERSION)}"
    try:
        r = anon.get(specific_url, timeout=15)
        ok = r.status_code == 200
        body = r.json() if ok else {}
        correct = (
            ok
            and body.get("namespace") == TEST_NS
            and body.get("name") == TEST_NAME
            and body.get("provider") == TEST_PROVIDER
            and body.get("version") == TEST_VERSION
        )
        record(
            f"GET /{{version}} returns correct module info",
            correct,
            f"status={r.status_code}",
        )
    except Exception as exc:
        record(f"GET /{{version}} returns correct module info", False, str(exc)[:80])

    # Test 13 — list includes our module
    try:
        r = anon.get(f"{base_url}/v1/modules", timeout=15)
        ok = r.status_code == 200
        body = r.json() if ok else {}
        found = any(
            m.get("namespace") == TEST_NS and m.get("name") == TEST_NAME
            for m in body.get("modules", [])
        )
        record("GET /v1/modules includes uploaded module", ok and found,
               f"status={r.status_code}, found={found}")
    except Exception as exc:
        record("GET /v1/modules includes uploaded module", False, str(exc)[:80])

    # Test 14 — search by name
    try:
        r = anon.get(f"{base_url}/v1/modules", params={"q": TEST_NAME}, timeout=15)
        ok = r.status_code == 200
        body = r.json() if ok else {}
        found = any(
            m.get("namespace") == TEST_NS and m.get("name") == TEST_NAME
            for m in body.get("modules", [])
        )
        record(f"GET /v1/modules?q={TEST_NAME} finds the module", ok and found,
               f"status={r.status_code}, found={found}")
    except Exception as exc:
        record(f"GET /v1/modules?q={TEST_NAME} finds the module", False, str(exc)[:80])

    # Test 15 — search by namespace
    try:
        r = anon.get(f"{base_url}/v1/modules", params={"namespace": TEST_NS}, timeout=15)
        ok = r.status_code == 200
        body = r.json() if ok else {}
        found = any(
            m.get("namespace") == TEST_NS
            for m in body.get("modules", [])
        )
        record(f"GET /v1/modules?namespace={TEST_NS} finds the module", ok and found,
               f"status={r.status_code}, found={found}")
    except Exception as exc:
        record(f"GET /v1/modules?namespace={TEST_NS} finds the module", False, str(exc)[:80])

    # Test 16 — download endpoint (204 + X-Terraform-Get)
    download_url = f"{base_url}{_module_path(TEST_NS, TEST_NAME, TEST_PROVIDER, TEST_VERSION, 'download')}"
    x_terraform_get = None
    try:
        r = anon.get(download_url, allow_redirects=False, timeout=15)
        ok = r.status_code == 204
        x_terraform_get = r.headers.get("X-Terraform-Get", "")
        has_header = bool(x_terraform_get)
        points_to_archive = "archive" in x_terraform_get
        record(
            "GET /download returns 204 with X-Terraform-Get pointing to /archive",
            ok and has_header and points_to_archive,
            f"status={r.status_code}, X-Terraform-Get={x_terraform_get!r}",
        )
    except Exception as exc:
        record("GET /download returns 204 with X-Terraform-Get pointing to /archive",
               False, str(exc)[:80])

    # Test 17 — archive endpoint returns gzip bytes
    try:
        # Build the archive URL from the X-Terraform-Get header when available,
        # else construct it directly.
        if x_terraform_get:
            if x_terraform_get.startswith("http"):
                archive_fetch_url = x_terraform_get
            else:
                archive_fetch_url = base_url + x_terraform_get
        else:
            archive_fetch_url = (
                f"{base_url}{_module_path(TEST_NS, TEST_NAME, TEST_PROVIDER, TEST_VERSION, 'archive')}"
            )
        r = anon.get(archive_fetch_url, timeout=30)
        ok = r.status_code == 200
        is_gzip = ok and r.content[:2] == b"\x1f\x8b"
        record(
            "GET /archive returns gzip bytes (magic \\x1f\\x8b)",
            ok and is_gzip,
            f"status={r.status_code}, bytes={len(r.content) if ok else '?'}",
        )
    except Exception as exc:
        record("GET /archive returns gzip bytes (magic \\x1f\\x8b)", False, str(exc)[:80])

    # -----------------------------------------------------------------------
    # [CLEANUP] — delete test module (unless --no-cleanup)
    # -----------------------------------------------------------------------
    print("\n[CLEANUP]")
    deleted = False
    if no_cleanup:
        print(f"  -- skipped (--no-cleanup): test module {TEST_NS}/{TEST_NAME}/{TEST_PROVIDER} v{TEST_VERSION} left in place")
        needs_frontend_module = uploaded
    else:
        if uploaded:
            # We will re-upload after cleanup for the frontend detail page test.
            needs_frontend_module = True
            try:
                r = authed.delete(upload_path, timeout=15)

                # Test 18 — delete returns 200
                record("DELETE test module returns 200", r.status_code == 200,
                       f"got {r.status_code}")
                deleted = r.status_code == 200
            except Exception as exc:
                record("DELETE test module returns 200", False, str(exc)[:80])

            # Test 19 — versions 404 after delete
            try:
                r = anon.get(versions_url, timeout=15)
                record("After delete, GET /versions returns 404",
                       r.status_code == 404, f"got {r.status_code}")
            except Exception as exc:
                record("After delete, GET /versions returns 404", False, str(exc)[:80])

            # Test 20 — specific version 404 after delete
            try:
                r = anon.get(specific_url, timeout=15)
                record("After delete, GET /{{version}} returns 404",
                       r.status_code == 404, f"got {r.status_code}")
            except Exception as exc:
                record("After delete, GET /{{version}} returns 404", False, str(exc)[:80])
        else:
            print("  -- nothing to clean up (upload did not succeed)")

    # -----------------------------------------------------------------------
    # [API: Edge Cases]
    # -----------------------------------------------------------------------
    print("\n[API: Edge Cases]")

    ghost_base = f"{base_url}/v1/modules/nonexistent/module/aws"

    edges: list[tuple[str, str]] = [
        ("GET /nonexistent/.../versions returns 404", f"{ghost_base}/versions"),
        ("GET /nonexistent/... (latest) returns 404", ghost_base),
        ("GET /nonexistent/.../1.0.0 returns 404", f"{ghost_base}/1.0.0"),
        ("GET /nonexistent/.../1.0.0/download returns 404", f"{ghost_base}/1.0.0/download"),
    ]

    for label, url in edges:
        try:
            r = anon.get(url, allow_redirects=False, timeout=15)
            record(label, r.status_code == 404, f"got {r.status_code}")
        except Exception as exc:
            record(label, False, str(exc)[:80])

    # -----------------------------------------------------------------------
    # Frontend tests (Playwright)
    # -----------------------------------------------------------------------
    if skip_frontend:
        print("\n[Frontend] skipped (--skip-frontend)")
    else:
        # Re-upload the test module so the detail page has something to show.
        frontend_module_present = False
        if needs_frontend_module and not no_cleanup:
            try:
                r2 = authed.post(
                    upload_path,
                    files={"file": ("module.tar.gz", io.BytesIO(archive_bytes), "application/gzip")},
                    timeout=30,
                )
                frontend_module_present = r2.status_code == 201
            except Exception:
                pass
        elif no_cleanup and uploaded:
            frontend_module_present = True

        _run_frontend_tests(
            results=results,
            base_url=base_url,
            dns_override=(base_url == PROD_URL),
            frontend_module_present=frontend_module_present,
        )

        # Clean up the re-uploaded frontend module
        if frontend_module_present and not no_cleanup:
            print("\n[CLEANUP: frontend module]")
            try:
                r = authed.delete(upload_path, timeout=15)
                if r.status_code == 200:
                    print(f"  {PASS} Frontend test module deleted")
                else:
                    print(f"  {FAIL} Frontend test module delete returned {r.status_code}")
            except Exception as exc:
                print(f"  {FAIL} Frontend test module delete failed: {exc}")

    return results


# ---------------------------------------------------------------------------
# Frontend (Playwright) tests — extracted into a helper so the main flow is clean
# ---------------------------------------------------------------------------


def _run_frontend_tests(
    results: list[tuple[str, bool]],
    base_url: str,
    dns_override: bool,
    frontend_module_present: bool,
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("\n[Frontend] skipped — playwright not installed")
        return

    def record(name: str, passed: bool, detail: str = "") -> None:
        icon = PASS if passed else FAIL
        suffix = f"  ({detail})" if detail else ""
        print(f"  {icon} {name}{suffix}")
        results.append((name, passed))

    launch_args: list[str] = []
    if dns_override:
        launch_args.append(
            f"--host-resolver-rules=MAP tf-registry.georg-nikola.com {PROD_IP}"
        )

    # -----------------------------------------------------------------------
    # [Frontend: Browse Page]
    # -----------------------------------------------------------------------
    print("\n[Frontend: Browse Page]")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=launch_args)

        # --- Browse page ---
        context = browser.new_context()
        page = context.new_page()

        console_errors: list[str] = []
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        failed_requests: list[str] = []
        page.on("requestfailed", lambda req: failed_requests.append(req.url))

        try:
            page.goto(base_url + "/", timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=20_000)
            record("Browse page (/) loads successfully", True)
        except Exception as exc:
            record("Browse page (/) loads successfully", False, str(exc)[:80])
            browser.close()
            return

        record("No failed resource requests on browse page",
               len(failed_requests) == 0,
               f"failed: {failed_requests}" if failed_requests else "")

        record("No JS console errors on browse page",
               len(console_errors) == 0,
               console_errors[0] if console_errors else "")

        title = page.title()
        record(
            "Page title contains 'Terraform' or 'Registry'",
            "terraform" in title.lower() or "registry" in title.lower(),
            f"title={title!r}",
        )

        record(
            "#module-list container is present in DOM",
            page.locator("#module-list").count() == 1,
        )

        record(
            "Search input (#search-input) is present",
            page.locator("#search-input").count() == 1,
        )

        context.close()

        # -----------------------------------------------------------------------
        # [Frontend: Upload Page]
        # -----------------------------------------------------------------------
        print("\n[Frontend: Upload Page]")

        context2 = browser.new_context()
        page2 = context2.new_page()

        up_console_errors: list[str] = []
        page2.on(
            "console",
            lambda msg: up_console_errors.append(msg.text) if msg.type == "error" else None,
        )

        try:
            page2.goto(base_url + "/upload.html", timeout=30_000)
            page2.wait_for_load_state("networkidle", timeout=20_000)
            record("Upload page (/upload.html) loads successfully", True)
        except Exception as exc:
            record("Upload page (/upload.html) loads successfully", False, str(exc)[:80])
            browser.close()
            return

        record("No JS console errors on upload page",
               len(up_console_errors) == 0,
               up_console_errors[0] if up_console_errors else "")

        record("API key input (#api-key) is present",
               page2.locator("#api-key").count() == 1)
        record("Namespace input (#namespace) is present",
               page2.locator("#namespace").count() == 1)
        record("Module name input (#module-name) is present",
               page2.locator("#module-name").count() == 1)
        record("Provider input (#provider) is present",
               page2.locator("#provider").count() == 1)
        record("Version input (#version) is present",
               page2.locator("#version").count() == 1)
        record("File upload input (#archive-file) is present",
               page2.locator("#archive-file").count() == 1)
        record("Upload submit button is present",
               page2.locator("button[type='submit']").count() >= 1)

        context2.close()

        # -----------------------------------------------------------------------
        # [Frontend: Module Detail Page]
        # -----------------------------------------------------------------------
        print("\n[Frontend: Module Detail Page]")

        if not frontend_module_present:
            print("  -- skipped: test module not present (upload failed or already cleaned up)")
        else:
            context3 = browser.new_context()
            page3 = context3.new_page()

            detail_console_errors: list[str] = []
            page3.on(
                "console",
                lambda msg: detail_console_errors.append(msg.text) if msg.type == "error" else None,
            )

            detail_url = (
                f"{base_url}/module.html"
                f"?namespace={TEST_NS}&name={TEST_NAME}&provider={TEST_PROVIDER}"
            )

            try:
                page3.goto(detail_url, timeout=30_000)
                page3.wait_for_load_state("networkidle", timeout=20_000)
                record("Module detail page loads successfully", True)
            except Exception as exc:
                record("Module detail page loads successfully", False, str(exc)[:80])
                context3.close()
                browser.close()
                return

            record("No JS console errors on module detail page",
                   len(detail_console_errors) == 0,
                   detail_console_errors[0] if detail_console_errors else "")

            # The detail content is rendered dynamically — wait for .module-header
            # or .empty-state to appear (whichever comes first).
            try:
                page3.wait_for_selector(
                    ".module-header, .empty-state",
                    timeout=10_000,
                )
            except Exception:
                pass

            record(
                "#module-detail container is present",
                page3.locator("#module-detail").count() == 1,
            )
            record(
                "Module header (.module-header) rendered after load",
                page3.locator(".module-header").count() >= 1,
            )
            record(
                "Versions section (.versions-section) is present",
                page3.locator(".versions-section").count() >= 1,
            )
            record(
                "Usage section (.usage-section) is present",
                page3.locator(".usage-section").count() >= 1,
            )

            context3.close()

        browser.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Terraform Module Registry smoke tests")
    parser.add_argument("--url", default=PROD_URL,
                        help=f"Base URL to test against (default: {PROD_URL})")
    parser.add_argument("--no-cleanup", action="store_true",
                        help="Skip deleting the test module after the run")
    parser.add_argument("--skip-frontend", action="store_true",
                        help="Skip Playwright frontend tests")
    args = parser.parse_args()

    api_key = _load_api_key()
    if not api_key:
        print(
            "WARNING: No API key found. "
            "Set TF_REGISTRY_API_KEY or create ~/repos/tf-registry/.api_key. "
            "Auth-protected tests will fail.\n"
        )

    label = f"PRODUCTION ({PROD_URL})" if args.url == PROD_URL else f"CUSTOM ({args.url})"

    print(f"\n{'='*60}")
    print(f"  Terraform Module Registry — Smoke Tests")
    print(f"  Target: {label}")
    if args.no_cleanup:
        print("  Mode: --no-cleanup (test data will be left in place)")
    if args.skip_frontend:
        print("  Mode: --skip-frontend")
    print(f"{'='*60}")

    t0 = time.time()
    results = run_tests(
        base_url=args.url,
        api_key=api_key,
        skip_frontend=args.skip_frontend,
        no_cleanup=args.no_cleanup,
    )
    elapsed = time.time() - t0

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    failed = total - passed

    color = "\033[92m" if failed == 0 else "\033[91m"
    reset = "\033[0m"

    print(f"\n{'='*60}")
    print(f"  {color}{passed}/{total} passed{reset}  |  {failed} failed  |  {elapsed:.1f}s")
    print(f"{'='*60}\n")

    if failed:
        failing_names = [name for name, ok in results if not ok]
        print("Failed tests:")
        for name in failing_names:
            print(f"  {FAIL} {name}")
        print()

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
