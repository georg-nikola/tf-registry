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

TEST_NS = "smoke-test"
TEST_NAME = "test-vpc"
TEST_PROVIDER = "aws"
TEST_VERSION = "9.9.9"

PASS = "\033[92m\u2713\033[0m"
FAIL = "\033[91m\u2717\033[0m"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_credentials() -> tuple[str, str]:
    """Return (username, password) from environment variables."""
    username = os.getenv("TF_REGISTRY_USERNAME", "admin")
    password = os.getenv("TF_REGISTRY_PASSWORD", "admin")
    return username, password


def _login(base_url: str, username: str, password: str) -> str:
    """POST /api/auth/login, return JWT access_token on success or empty string."""
    try:
        r = requests.post(
            f"{base_url}/api/auth/login",
            json={"username": username, "password": password},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("access_token", "")
    except Exception:
        pass
    return ""


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


def run_tests(base_url: str, username: str, password: str, skip_frontend: bool, no_cleanup: bool) -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []
    base_url = base_url.rstrip("/")

    def record(name: str, passed: bool, detail: str = "") -> None:
        icon = PASS if passed else FAIL
        suffix = f"  ({detail})" if detail else ""
        print(f"  {icon} {name}{suffix}")
        results.append((name, passed))

    anon = requests.Session()
    uploaded = False
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

    # Login with correct credentials
    jwt_token = ""
    try:
        r = requests.post(
            f"{base_url}/api/auth/login",
            json={"username": username, "password": password},
            timeout=15,
        )
        ok = r.status_code == 200
        token = r.json().get("access_token", "") if ok else ""
        has_token = bool(token)
        record("POST /api/auth/login with correct credentials → 200 + access_token",
               ok and has_token,
               f"status={r.status_code}")
        if has_token:
            jwt_token = token
    except Exception as exc:
        record("POST /api/auth/login with correct credentials → 200 + access_token",
               False, str(exc)[:80])

    # Login with wrong password
    try:
        r = requests.post(
            f"{base_url}/api/auth/login",
            json={"username": username, "password": "definitely-wrong-password"},
            timeout=15,
        )
        record("POST /api/auth/login with wrong password → 401",
               r.status_code == 401,
               f"got {r.status_code}")
    except Exception as exc:
        record("POST /api/auth/login with wrong password → 401", False, str(exc)[:80])

    # Login with wrong username
    try:
        r = requests.post(
            f"{base_url}/api/auth/login",
            json={"username": "nobody", "password": password},
            timeout=15,
        )
        record("POST /api/auth/login with wrong username → 401",
               r.status_code == 401,
               f"got {r.status_code}")
    except Exception as exc:
        record("POST /api/auth/login with wrong username → 401", False, str(exc)[:80])

    authed = requests.Session()
    if jwt_token:
        authed.headers["Authorization"] = f"Bearer {jwt_token}"

    upload_path = f"{base_url}{_module_path(TEST_NS, TEST_NAME, TEST_PROVIDER, TEST_VERSION)}"
    archive_bytes = _make_archive()

    # Upload without Authorization header
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

    # Upload with invalid JWT
    try:
        r = requests.post(
            upload_path,
            files={"file": ("module.tar.gz", io.BytesIO(archive_bytes), "application/gzip")},
            headers={"Authorization": "Bearer this-is-not-a-valid-jwt"},
            timeout=15,
        )
        record("Upload with invalid JWT → 401",
               r.status_code == 401, f"got {r.status_code}")
    except Exception as exc:
        record("Upload with invalid JWT → 401", False, str(exc)[:80])

    # -----------------------------------------------------------------------
    # [API: Module Lifecycle]
    # -----------------------------------------------------------------------
    print("\n[API: Module Lifecycle]")

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

    try:
        if x_terraform_get:
            archive_fetch_url = x_terraform_get if x_terraform_get.startswith("http") else base_url + x_terraform_get
        else:
            archive_fetch_url = f"{base_url}{_module_path(TEST_NS, TEST_NAME, TEST_PROVIDER, TEST_VERSION, 'archive')}"
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
    # [CLEANUP]
    # -----------------------------------------------------------------------
    print("\n[CLEANUP]")
    deleted = False
    if no_cleanup:
        print(f"  -- skipped (--no-cleanup): test module left in place")
        needs_frontend_module = uploaded
    else:
        if uploaded:
            needs_frontend_module = True
            try:
                r = authed.delete(upload_path, timeout=15)
                record("DELETE test module returns 200", r.status_code == 200,
                       f"got {r.status_code}")
                deleted = r.status_code == 200
            except Exception as exc:
                record("DELETE test module returns 200", False, str(exc)[:80])

            try:
                r = anon.get(versions_url, timeout=15)
                record("After delete, GET /versions returns 404",
                       r.status_code == 404, f"got {r.status_code}")
            except Exception as exc:
                record("After delete, GET /versions returns 404", False, str(exc)[:80])

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

        if frontend_module_present and not no_cleanup:
            print("\n[CLEANUP: frontend module]")
            try:
                r = authed.delete(upload_path, timeout=15)
                icon = PASS if r.status_code == 200 else FAIL
                print(f"  {icon} Frontend test module deleted")
            except Exception as exc:
                print(f"  {FAIL} Frontend test module delete failed: {exc}")

    return results


# ---------------------------------------------------------------------------
# Frontend (Playwright) tests
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

    print("\n[Frontend: Browse Page]")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=launch_args)

        context = browser.new_context()
        page = context.new_page()

        console_errors: list[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
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
        record("Page title contains 'Terraform' or 'Registry'",
               "terraform" in title.lower() or "registry" in title.lower(),
               f"title={title!r}")

        record("#module-list container is present in DOM",
               page.locator("#module-list").count() == 1)

        record("Search input (#search-input) is present",
               page.locator("#search-input").count() == 1)

        context.close()

        # -----------------------------------------------------------------------
        # [Frontend: Upload Page]
        # -----------------------------------------------------------------------
        print("\n[Frontend: Upload Page]")

        context2 = browser.new_context()
        page2 = context2.new_page()

        up_console_errors: list[str] = []
        page2.on("console", lambda msg: up_console_errors.append(msg.text) if msg.type == "error" else None)

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

        record("Namespace input (#namespace) is present",
               page2.locator("#namespace").count() == 1)
        record("Module name input (#module-name) is present",
               page2.locator("#module-name").count() == 1)
        record("File upload input (#archive-file) is present",
               page2.locator("#archive-file").count() == 1)

        context2.close()

        # -----------------------------------------------------------------------
        # [Frontend: Module Detail Page]
        # -----------------------------------------------------------------------
        print("\n[Frontend: Module Detail Page]")

        if not frontend_module_present:
            print("  -- skipped: test module not present")
        else:
            context3 = browser.new_context()
            page3 = context3.new_page()

            detail_console_errors: list[str] = []
            page3.on("console", lambda msg: detail_console_errors.append(msg.text) if msg.type == "error" else None)

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

            try:
                page3.wait_for_selector(".module-header, .empty-state", timeout=10_000)
            except Exception:
                pass

            record("#module-detail container is present",
                   page3.locator("#module-detail").count() == 1)
            record("Module header (.module-header) rendered after load",
                   page3.locator(".module-header").count() >= 1)
            record("Versions section (.versions-section) is present",
                   page3.locator(".versions-section").count() >= 1)
            record("Usage section (.usage-section) is present",
                   page3.locator(".usage-section").count() >= 1)

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

    username, password = _get_credentials()
    if not password:
        print(
            "WARNING: TF_REGISTRY_PASSWORD not set — defaulting to 'admin'. "
            "Auth tests will fail if the password is different.\n"
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
        username=username,
        password=password,
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
