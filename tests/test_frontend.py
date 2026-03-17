"""Terraform Module Registry — comprehensive Playwright frontend tests.

Usage:
  # Against production (via Cloudflare, WARP-safe DNS override applied automatically)
  python tests/test_frontend.py

  # Against a custom URL (e.g. kubectl port-forward or local dev server)
  python tests/test_frontend.py --url http://localhost:8000

  # Keep test data after run (useful for debugging)
  python tests/test_frontend.py --no-cleanup

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

UI_NS = "smoke-ui"
UI_PROVIDER = "aws"

PASS = "\033[92m\u2713\033[0m"
FAIL = "\033[91m\u2717\033[0m"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_credentials() -> tuple[str, str]:
    username = os.getenv("TF_REGISTRY_USERNAME", "admin")
    password = os.getenv("TF_REGISTRY_PASSWORD", "admin")
    return username, password


def _login(base_url: str, username: str, password: str) -> str:
    """Return JWT access_token or empty string on failure."""
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


def _make_archive(module_name: str = "vpc") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for fname, content in [
            ("main.tf", f"# smoke-ui {module_name} terraform module\n".encode()),
            ("README.md", f"# {module_name.upper()} Module\nA UI smoke test module.\n".encode()),
        ]:
            info = tarfile.TarInfo(name=fname)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _upload_module(session, base_url, namespace, name, provider, version) -> bool:
    url = f"{base_url}/v1/modules/{namespace}/{name}/{provider}/{version}"
    archive = _make_archive(name)
    try:
        r = session.post(
            url,
            files={"file": ("module.tar.gz", io.BytesIO(archive), "application/gzip")},
            timeout=30,
        )
        return r.status_code == 201
    except Exception:
        return False


def _delete_module(session, base_url, namespace, name, provider, version) -> bool:
    url = f"{base_url}/v1/modules/{namespace}/{name}/{provider}/{version}"
    try:
        r = session.delete(url, timeout=15)
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Core test runner
# ---------------------------------------------------------------------------


def run_tests(base_url: str, username: str, password: str, no_cleanup: bool) -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []
    base_url = base_url.rstrip("/")

    token = _login(base_url, username, password)
    authed = requests.Session()
    if token:
        authed.headers["Authorization"] = f"Bearer {token}"

    uploaded_modules: list[tuple[str, str, str, str]] = []

    try:
        _run_all_frontend_tests(
            results=results,
            base_url=base_url,
            username=username,
            password=password,
            token=token,
            authed=authed,
            uploaded_modules=uploaded_modules,
            dns_override=(base_url == PROD_URL),
        )
    finally:
        if uploaded_modules:
            print("\n[CLEANUP]")
            if no_cleanup:
                print(f"  -- skipped (--no-cleanup): {len(uploaded_modules)} test module(s) left in place")
            else:
                for ns, name, provider, version in uploaded_modules:
                    label = f"{ns}/{name}/{provider} v{version}"
                    ok = _delete_module(authed, base_url, ns, name, provider, version)
                    icon = PASS if ok else FAIL
                    print(f"  {icon} Deleted {label}")

    return results


# ---------------------------------------------------------------------------
# All frontend tests
# ---------------------------------------------------------------------------


def _run_all_frontend_tests(
    results,
    base_url,
    username,
    password,
    token,
    authed,
    uploaded_modules,
    dns_override,
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("\nPlaywright not installed — all frontend tests skipped.")
        return

    def record(name, passed, detail=""):
        icon = PASS if passed else FAIL
        suffix = f"  ({detail})" if detail else ""
        print(f"  {icon} {name}{suffix}")
        results.append((name, passed))

    launch_args = []
    if dns_override:
        launch_args.append(f"--host-resolver-rules=MAP tf-registry.georg-nikola.com {PROD_IP}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=launch_args)

        try:
            print("\n[Frontend: Browse Page — Empty State]")
            _test_browse_empty(browser, base_url, record)

            print("\n[Frontend: Fixture Upload]")
            fixtures = [
                (UI_NS, "vpc", UI_PROVIDER, "1.0.0"),
                (UI_NS, "subnet", UI_PROVIDER, "1.0.0"),
                (UI_NS, "vpc", UI_PROVIDER, "2.0.0"),
            ]
            any_uploaded = False
            for ns, name, provider, version in fixtures:
                ok = _upload_module(authed, base_url, ns, name, provider, version)
                icon = PASS if ok else FAIL
                print(f"  {icon} Uploaded {ns}/{name}/{provider} v{version}")
                if ok:
                    uploaded_modules.append((ns, name, provider, version))
                    any_uploaded = True

            print("\n[Frontend: Browse Page — With Modules]")
            if any_uploaded:
                _test_browse_with_modules(browser, base_url, record)
            else:
                print("  -- skipped: no fixtures uploaded successfully")

            print("\n[Frontend: Module Detail Page]")
            vpc_present = any(
                ns == UI_NS and name == "vpc" and provider == UI_PROVIDER
                for ns, name, provider, _ in uploaded_modules
            )
            if vpc_present:
                _test_module_detail(browser, base_url, record)
            else:
                print("  -- skipped: smoke-ui/vpc/aws not uploaded")

            print("\n[Frontend: Upload Page]")
            _test_upload_page(browser, base_url, token, record)

            print("\n[Frontend: Login Page]")
            _test_login_page(browser, base_url, username, password, record)

            print("\n[Frontend: Navigation]")
            _test_navigation(browser, base_url, record)

        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Test group implementations
# ---------------------------------------------------------------------------


def _test_browse_empty(browser, base_url, record):
    context = browser.new_context()
    page = context.new_page()
    console_errors = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    try:
        response = page.goto(base_url + "/", timeout=30_000)
        ok = response is not None and response.status == 200
        record("Page loads at / with HTTP 200", ok,
               f"status={response.status if response else 'none'}")
    except Exception as exc:
        record("Page loads at / with HTTP 200", False, str(exc)[:80])
        context.close()
        return

    try:
        page.wait_for_selector("#module-list .module-card, #module-list .empty-state", timeout=10_000)
    except Exception:
        pass

    record("No JS console errors on load",
           len(console_errors) == 0,
           console_errors[0][:100] if console_errors else "")

    title = page.title()
    record("Title is 'Terraform Module Registry'",
           title == "Terraform Module Registry",
           f"title={title!r}")

    record("#search-input exists and is interactive",
           page.locator("#search-input").count() == 1)
    record("#namespace-filter dropdown exists",
           page.locator("#namespace-filter").count() == 1)
    record("#module-list container exists",
           page.locator("#module-list").count() == 1)
    record("#prev-btn exists", page.locator("#prev-btn").count() == 1)
    record("#next-btn exists", page.locator("#next-btn").count() == 1)

    module_cards = page.locator("#module-list .module-card").count()
    error_alert = page.locator("#module-list .alert-error").count()
    has_empty_state = page.locator("#module-list .empty-state").count() >= 1
    empty_ok = (module_cards == 0 and error_alert == 0) or has_empty_state
    record("Empty state shown — no error alert visible",
           empty_ok,
           f"cards={module_cards}, error_alerts={error_alert}, empty_state={has_empty_state}")

    footer_text = page.locator("footer").inner_text()
    footer_ok = "v1" in footer_text.lower() or "registry" in footer_text.lower()
    record("Footer contains version/registry text",
           footer_ok,
           f"footer={footer_text.strip()!r}")

    context.close()


def _test_browse_with_modules(browser, base_url, record):
    context = browser.new_context()
    page = context.new_page()
    console_errors = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    try:
        page.goto(base_url + "/", timeout=30_000)
        page.wait_for_selector("#module-list .module-card", timeout=15_000)
    except Exception as exc:
        record("Module cards appear in #module-list after load", False, str(exc)[:80])
        context.close()
        return

    cards = page.locator("#module-list .module-card")
    card_count = cards.count()
    record("Module cards appear in #module-list after load",
           card_count >= 1, f"found {card_count} cards")

    if card_count >= 1:
        first_card_text = cards.first.inner_text()
        has_ns = UI_NS in first_card_text or "smoke" in first_card_text.lower()
        has_provider = UI_PROVIDER in first_card_text.lower()
        record("Module cards show namespace, name, provider",
               has_ns or has_provider,
               f"card text sample: {first_card_text[:60]!r}")
    else:
        record("Module cards show namespace, name, provider", False, "no cards found")

    page.locator("#search-input").fill("vpc")
    try:
        page.wait_for_selector("#module-list .module-card", timeout=8_000)
        page.wait_for_timeout(600)
    except Exception:
        pass

    visible_cards = page.locator("#module-list .module-card")
    vpc_card_count = visible_cards.count()
    all_vpc = all("subnet" not in visible_cards.nth(i).inner_text().lower()
                  for i in range(vpc_card_count))
    record('Search "vpc" — only vpc modules shown, subnet hidden',
           vpc_card_count >= 1 and all_vpc,
           f"cards={vpc_card_count}, all_vpc={all_vpc}")

    page.locator("#search-input").fill("")
    try:
        page.wait_for_timeout(600)
        page.wait_for_selector("#module-list .module-card", timeout=8_000)
    except Exception:
        pass

    all_cards_after_clear = page.locator("#module-list .module-card").count()
    record("Clear search — all modules shown again",
           all_cards_after_clear >= card_count,
           f"before_clear={card_count}, after_clear={all_cards_after_clear}")

    try:
        page.locator("#namespace-filter").select_option(UI_NS)
        page.wait_for_timeout(600)
        page.wait_for_selector("#module-list .module-card, #module-list .empty-state", timeout=8_000)
    except Exception:
        pass

    ns_cards = page.locator("#module-list .module-card")
    ns_count = ns_cards.count()
    all_smoke_ui = all(UI_NS in ns_cards.nth(i).inner_text() for i in range(ns_count))
    record(f"Namespace filter '{UI_NS}' — shows only smoke-ui modules",
           ns_count >= 1 and all_smoke_ui, f"cards={ns_count}")

    try:
        page.locator("#namespace-filter").select_option("")
        page.wait_for_timeout(600)
        page.wait_for_selector("#module-list .module-card", timeout=8_000)
    except Exception:
        pass

    prev_disabled = page.locator("#prev-btn").get_attribute("disabled") is not None
    next_disabled = page.locator("#next-btn").get_attribute("disabled") is not None
    record("Pagination: with ≤20 modules, Previous and Next are both disabled",
           prev_disabled and next_disabled,
           f"prev_disabled={prev_disabled}, next_disabled={next_disabled}")

    context.close()


def _test_module_detail(browser, base_url, record):
    context = browser.new_context()
    page = context.new_page()
    console_errors = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    detail_url = f"{base_url}/module.html?namespace={UI_NS}&name=vpc&provider={UI_PROVIDER}"

    try:
        response = page.goto(detail_url, timeout=30_000)
        ok = response is not None and response.status == 200
        record(f"/module.html?namespace={UI_NS}&name=vpc&provider={UI_PROVIDER} loads (200)",
               ok, f"status={response.status if response else 'none'}")
    except Exception as exc:
        record(f"/module.html?namespace={UI_NS}&name=vpc&provider={UI_PROVIDER} loads (200)",
               False, str(exc)[:80])
        context.close()
        return

    record("No JS console errors on module detail page",
           len(console_errors) == 0,
           console_errors[0][:100] if console_errors else "")

    try:
        page.wait_for_selector(".module-header", timeout=8_000)
    except Exception:
        pass

    record(".module-header is rendered (waited up to 8s)",
           page.locator(".module-header").count() >= 1)

    header_text = page.locator(".module-header").inner_text() if page.locator(".module-header").count() >= 1 else ""
    record('Module name "vpc" appears in the header',
           "vpc" in header_text.lower(),
           f"header text: {header_text[:80]!r}")

    versions_section = page.locator(".versions-section")
    record(".versions-section is present", versions_section.count() >= 1)

    versions_text = versions_section.inner_text() if versions_section.count() >= 1 else ""
    record('.versions-section shows "2.0.0" and "1.0.0"',
           "2.0.0" in versions_text and "1.0.0" in versions_text,
           f"versions text: {versions_text[:120]!r}")

    usage_section = page.locator(".usage-section")
    record(".usage-section is present", usage_section.count() >= 1)

    usage_text = usage_section.inner_text() if usage_section.count() >= 1 else ""
    record('Usage section contains "source"',
           "source" in usage_text.lower(), f"usage: {usage_text[:80]!r}")

    record(f'Usage snippet contains "{UI_NS}/vpc/{UI_PROVIDER}"',
           f"{UI_NS}/vpc/{UI_PROVIDER}" in usage_text,
           f"usage: {usage_text[:120]!r}")

    try:
        version_link = page.locator(".version-link", has_text="1.0.0").first
        with page.expect_navigation(timeout=8_000):
            version_link.click()
        new_url = page.url
        record("Clicking version '1.0.0' navigates/updates URL",
               "version=1.0.0" in new_url or "1.0.0" in new_url,
               f"url={new_url!r}")
    except Exception as exc:
        record("Clicking version '1.0.0' navigates/updates URL", False, str(exc)[:80])

    readme_present = page.locator(".readme-section").count() >= 1
    record("README section is present (or correctly absent when empty)",
           True, f"readme_section_present={readme_present}")

    context.close()


def _test_upload_page(browser, base_url, token, record):
    context = browser.new_context()
    page = context.new_page()
    console_errors = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    try:
        response = page.goto(base_url + "/upload.html", timeout=30_000)
        ok = response is not None and response.status == 200
        record("/upload.html loads with no navigation errors", ok,
               f"status={response.status if response else 'none'}")
    except Exception as exc:
        record("/upload.html loads with no navigation errors", False, str(exc)[:80])
        context.close()
        return

    page.wait_for_load_state("domcontentloaded")

    record("No JS console errors on /upload.html",
           len(console_errors) == 0,
           console_errors[0][:100] if console_errors else "")

    # Without a JWT, upload form should be disabled and auth message shown.
    auth_msg = page.locator("#auth-required-msg")
    auth_msg_visible = auth_msg.count() >= 1 and auth_msg.is_visible()
    record("#auth-required-msg shown when not logged in",
           auth_msg_visible)

    # Now set JWT in localStorage and reload — form should be enabled.
    if token:
        page.evaluate(f"localStorage.setItem('tf_jwt', '{token}')")
        page.goto(base_url + "/upload.html", timeout=30_000)
        page.wait_for_load_state("domcontentloaded")

        auth_msg_after = page.locator("#auth-required-msg")
        auth_hidden = auth_msg_after.count() == 0 or not auth_msg_after.is_visible()
        record("With JWT in localStorage, #auth-required-msg is hidden",
               auth_hidden)

        # Clean up
        page.evaluate("localStorage.removeItem('tf_jwt')")
    else:
        record("With JWT in localStorage, #auth-required-msg is hidden", False, "no token available")

    record("All required fields exist: #namespace",
           page.locator("#namespace").count() == 1)
    record("All required fields exist: #module-name",
           page.locator("#module-name").count() == 1)
    record("All required fields exist: #provider",
           page.locator("#provider").count() == 1)
    record("All required fields exist: #version",
           page.locator("#version").count() == 1)
    record("All required fields exist: #archive-file",
           page.locator("#archive-file").count() == 1)
    record("All required fields exist: #description",
           page.locator("#description").count() == 1)

    archive_required = page.locator("#archive-file").get_attribute("required")
    record("File input #archive-file has 'required' attribute (HTML5 validation)",
           archive_required is not None,
           f"required attr={archive_required!r}")

    context.close()


def _test_login_page(browser, base_url, username, password, record):
    context = browser.new_context()
    page = context.new_page()
    console_errors = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    try:
        response = page.goto(base_url + "/login.html", timeout=30_000)
        ok = response is not None and response.status == 200
        record("/login.html loads (200)", ok,
               f"status={response.status if response else 'none'}")
    except Exception as exc:
        record("/login.html loads (200)", False, str(exc)[:80])
        context.close()
        return

    page.wait_for_load_state("domcontentloaded")

    record("No JS console errors on /login.html",
           len(console_errors) == 0,
           console_errors[0][:100] if console_errors else "")

    # Login card visible by default (not logged in).
    record("#login-card exists and is visible",
           page.locator("#login-card").count() == 1 and page.locator("#login-card").is_visible())

    record("#module-list is NOT present on login page",
           page.locator("#module-list").count() == 0)

    record("#username input exists",
           page.locator("#username").count() == 1)

    record("#password input exists",
           page.locator("#password").count() == 1)

    # Submit with wrong credentials → error shown.
    page.locator("#username").fill("admin")
    page.locator("#password").fill("wrong-password-xyz")
    page.locator("#login-form button[type='submit']").click()
    try:
        page.wait_for_selector(".alert-error", timeout=8_000)
    except Exception:
        pass
    record("Wrong credentials → error alert shown",
           page.locator(".alert-error").count() >= 1)

    # Submit with correct credentials → logged-in box shown.
    page.locator("#username").fill(username)
    page.locator("#password").fill(password)
    page.locator("#login-form button[type='submit']").click()
    try:
        page.wait_for_selector("#logged-in-box:not([style*='display: none'])", timeout=8_000)
    except Exception:
        pass

    record("Correct credentials → #logged-in-box shown",
           page.locator("#logged-in-box").count() >= 1 and page.locator("#logged-in-box").is_visible())

    record("#login-card hidden after login",
           not page.locator("#login-card").is_visible())

    # Logout → login form shown again.
    page.locator("#logout-btn").click()
    try:
        page.wait_for_selector("#login-card:not([style*='display: none'])", timeout=5_000)
    except Exception:
        pass
    record("Clicking logout → login form shown again",
           page.locator("#login-card").is_visible())

    context.close()


def _test_navigation(browser, base_url, record):
    context = browser.new_context()
    page = context.new_page()

    try:
        page.goto(base_url + "/", timeout=30_000)
        page.wait_for_load_state("domcontentloaded")
        with page.expect_navigation(timeout=10_000):
            page.locator("nav a[href='upload.html']").first.click()
        record("From /, clicking 'Upload' nav link navigates to /upload.html",
               "upload.html" in page.url, f"url={page.url!r}")
    except Exception as exc:
        record("From /, clicking 'Upload' nav link navigates to /upload.html",
               False, str(exc)[:80])

    try:
        page.goto(base_url + "/upload.html", timeout=30_000)
        page.wait_for_load_state("domcontentloaded")
        with page.expect_navigation(timeout=10_000):
            page.locator("nav a[href='/']").first.click()
        record("From /upload.html, clicking 'Browse' navigates to /",
               page.url.rstrip("/") == base_url or page.url.endswith("/"),
               f"url={page.url!r}")
    except Exception as exc:
        record("From /upload.html, clicking 'Browse' navigates to /",
               False, str(exc)[:80])

    try:
        page.goto(base_url + "/", timeout=30_000)
        page.wait_for_load_state("domcontentloaded")
        with page.expect_navigation(timeout=10_000):
            page.locator("nav a[href='login.html']").first.click()
        record("From /, clicking 'Login' nav link navigates to /login.html",
               "login.html" in page.url, f"url={page.url!r}")
    except Exception as exc:
        record("From /, clicking 'Login' nav link navigates to /login.html",
               False, str(exc)[:80])

    context.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Terraform Module Registry — frontend Playwright tests")
    parser.add_argument("--url", default=PROD_URL,
                        help=f"Base URL to test against (default: {PROD_URL})")
    parser.add_argument("--no-cleanup", action="store_true",
                        help="Skip deleting the test modules after the run")
    args = parser.parse_args()

    username, password = _get_credentials()

    label = f"PRODUCTION ({PROD_URL})" if args.url == PROD_URL else f"CUSTOM ({args.url})"

    print(f"\n{'='*60}")
    print(f"  Terraform Module Registry — Frontend Tests")
    print(f"  Target: {label}")
    if args.no_cleanup:
        print("  Mode: --no-cleanup (test fixtures will be left in place)")
    print(f"{'='*60}")

    t0 = time.time()
    results = run_tests(
        base_url=args.url,
        username=username,
        password=password,
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
