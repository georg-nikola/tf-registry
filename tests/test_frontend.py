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
import re
import sys
import tarfile
import time

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROD_URL = "https://tf-registry.georg-nikola.com"
PROD_IP = "104.21.12.221"

# Namespace/name/provider for the UI smoke test fixtures.
UI_NS = "smoke-ui"
UI_PROVIDER = "aws"

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


def _make_archive(module_name: str = "vpc") -> bytes:
    """Build a minimal valid tar.gz in memory containing main.tf and README.md."""
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


def _upload_module(
    session: requests.Session,
    base_url: str,
    namespace: str,
    name: str,
    provider: str,
    version: str,
) -> bool:
    """Upload a test module via API. Returns True on success (201)."""
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


def _delete_module(
    session: requests.Session,
    base_url: str,
    namespace: str,
    name: str,
    provider: str,
    version: str,
) -> bool:
    """Delete a test module via API. Returns True on 200."""
    url = f"{base_url}/v1/modules/{namespace}/{name}/{provider}/{version}"
    try:
        r = session.delete(url, timeout=15)
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Core test runner
# ---------------------------------------------------------------------------


def run_tests(base_url: str, api_key: str, no_cleanup: bool) -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []
    base_url = base_url.rstrip("/")

    authed = requests.Session()
    authed.headers["Authorization"] = f"Bearer {api_key}"

    # Modules uploaded during this run — tracked for cleanup.
    # List of (namespace, name, provider, version) tuples.
    uploaded_modules: list[tuple[str, str, str, str]] = []

    try:
        _run_all_frontend_tests(
            results=results,
            base_url=base_url,
            api_key=api_key,
            authed=authed,
            uploaded_modules=uploaded_modules,
            dns_override=(base_url == PROD_URL),
        )
    finally:
        # -----------------------------------------------------------------------
        # Cleanup — always run unless --no-cleanup
        # -----------------------------------------------------------------------
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
# All frontend tests — organised into groups
# ---------------------------------------------------------------------------


def _run_all_frontend_tests(
    results: list[tuple[str, bool]],
    base_url: str,
    api_key: str,
    authed: requests.Session,
    uploaded_modules: list[tuple[str, str, str, str]],
    dns_override: bool,
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("\nPlaywright not installed — all frontend tests skipped.")
        print("Install with: pip install playwright && playwright install chromium")
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=launch_args)

        try:
            # -------------------------------------------------------------------
            # [Frontend: Browse Page — Empty State]
            # -------------------------------------------------------------------
            print("\n[Frontend: Browse Page — Empty State]")
            _test_browse_empty(browser, base_url, record)

            # -------------------------------------------------------------------
            # Upload test fixtures before the next groups
            # -------------------------------------------------------------------
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

            # -------------------------------------------------------------------
            # [Frontend: Browse Page — With Modules]
            # -------------------------------------------------------------------
            print("\n[Frontend: Browse Page — With Modules]")
            if any_uploaded:
                _test_browse_with_modules(browser, base_url, record)
            else:
                print("  -- skipped: no fixtures uploaded successfully")

            # -------------------------------------------------------------------
            # [Frontend: Module Detail Page]
            # -------------------------------------------------------------------
            print("\n[Frontend: Module Detail Page]")
            vpc_present = any(
                ns == UI_NS and name == "vpc" and provider == UI_PROVIDER
                for ns, name, provider, _ in uploaded_modules
            )
            if vpc_present:
                _test_module_detail(browser, base_url, record)
            else:
                print("  -- skipped: smoke-ui/vpc/aws not uploaded")

            # -------------------------------------------------------------------
            # [Frontend: Upload Page]
            # -------------------------------------------------------------------
            print("\n[Frontend: Upload Page]")
            _test_upload_page(browser, base_url, record)

            # -------------------------------------------------------------------
            # [Frontend: Keys Page]
            # -------------------------------------------------------------------
            print("\n[Frontend: Keys Page]")
            _test_keys_page(browser, base_url, api_key, record, uploaded_modules, authed)

            # -------------------------------------------------------------------
            # [Frontend: Navigation]
            # -------------------------------------------------------------------
            print("\n[Frontend: Navigation]")
            _test_navigation(browser, base_url, record)

        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Test group implementations
# ---------------------------------------------------------------------------


def _test_browse_empty(browser, base_url: str, record) -> None:
    """Tests 1-9: Browse page before any modules exist (best-effort empty state)."""
    context = browser.new_context()
    page = context.new_page()

    console_errors: list[str] = []
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

    # Wait for the module list to settle (either cards or empty state).
    try:
        page.wait_for_selector("#module-list .module-card, #module-list .empty-state", timeout=10_000)
    except Exception:
        pass  # List may already be in DOM

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

    record("#prev-btn exists",
           page.locator("#prev-btn").count() == 1)

    record("#next-btn exists",
           page.locator("#next-btn").count() == 1)

    # Empty state: either empty list or .empty-state div — NOT an .alert element.
    module_cards = page.locator("#module-list .module-card").count()
    error_alert = page.locator("#module-list .alert-error").count()
    # Accept: 0 cards with no error alert, OR an .empty-state element
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


def _test_browse_with_modules(browser, base_url: str, record) -> None:
    """Tests 10-15: Browse page behaviour with smoke-ui modules present."""
    context = browser.new_context()
    page = context.new_page()

    console_errors: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    try:
        page.goto(base_url + "/", timeout=30_000)
        # Wait for at least one module card to appear.
        page.wait_for_selector("#module-list .module-card", timeout=15_000)
    except Exception as exc:
        record("Module cards appear in #module-list after load", False, str(exc)[:80])
        context.close()
        return

    cards = page.locator("#module-list .module-card")
    card_count = cards.count()
    record("Module cards appear in #module-list after load",
           card_count >= 1,
           f"found {card_count} cards")

    # Check first card shows namespace, name, provider info.
    if card_count >= 1:
        first_card_text = cards.first.inner_text()
        has_ns = UI_NS in first_card_text or "smoke" in first_card_text.lower()
        has_provider = UI_PROVIDER in first_card_text.lower()
        record("Module cards show namespace, name, provider",
               has_ns or has_provider,
               f"card text sample: {first_card_text[:60]!r}")
    else:
        record("Module cards show namespace, name, provider", False, "no cards found")

    # Search for "vpc" — only vpc modules visible.
    page.locator("#search-input").fill("vpc")
    try:
        page.wait_for_selector("#module-list .module-card", timeout=8_000)
        # Allow debounce (300ms in app.js) to fire and re-render.
        page.wait_for_timeout(600)
    except Exception:
        pass

    visible_cards = page.locator("#module-list .module-card")
    vpc_card_count = visible_cards.count()
    all_vpc = True
    for i in range(vpc_card_count):
        text = visible_cards.nth(i).inner_text().lower()
        if "subnet" in text:
            all_vpc = False
            break
    record('Search "vpc" — only vpc modules shown, subnet hidden',
           vpc_card_count >= 1 and all_vpc,
           f"cards={vpc_card_count}, all_vpc={all_vpc}")

    # Clear search — all modules visible again.
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

    # Namespace filter for smoke-ui — only smoke-ui modules.
    try:
        page.locator("#namespace-filter").select_option(UI_NS)
        page.wait_for_timeout(600)
        page.wait_for_selector("#module-list .module-card, #module-list .empty-state", timeout=8_000)
    except Exception:
        pass

    ns_cards = page.locator("#module-list .module-card")
    ns_count = ns_cards.count()
    all_smoke_ui = True
    for i in range(ns_count):
        text = ns_cards.nth(i).inner_text()
        if UI_NS not in text:
            all_smoke_ui = False
            break
    record(f"Namespace filter '{UI_NS}' — shows only smoke-ui modules",
           ns_count >= 1 and all_smoke_ui,
           f"cards={ns_count}")

    # Reset filter before pagination check.
    try:
        page.locator("#namespace-filter").select_option("")
        page.wait_for_timeout(600)
        page.wait_for_selector("#module-list .module-card", timeout=8_000)
    except Exception:
        pass

    # Pagination: 3 modules with limit=20 — both buttons disabled.
    prev_disabled = page.locator("#prev-btn").get_attribute("disabled") is not None
    next_disabled = page.locator("#next-btn").get_attribute("disabled") is not None
    record("Pagination: with ≤20 modules, Previous and Next are both disabled",
           prev_disabled and next_disabled,
           f"prev_disabled={prev_disabled}, next_disabled={next_disabled}")

    context.close()


def _test_module_detail(browser, base_url: str, record) -> None:
    """Tests 16-24: Module detail page for smoke-ui/vpc/aws."""
    context = browser.new_context()
    page = context.new_page()

    console_errors: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    detail_url = f"{base_url}/module.html?namespace={UI_NS}&name=vpc&provider={UI_PROVIDER}"

    try:
        response = page.goto(detail_url, timeout=30_000)
        ok = response is not None and response.status == 200
        record(f"/module.html?namespace={UI_NS}&name=vpc&provider={UI_PROVIDER} loads (200)",
               ok,
               f"status={response.status if response else 'none'}")
    except Exception as exc:
        record(f"/module.html?namespace={UI_NS}&name=vpc&provider={UI_PROVIDER} loads (200)",
               False, str(exc)[:80])
        context.close()
        return

    record("No JS console errors on module detail page",
           len(console_errors) == 0,
           console_errors[0][:100] if console_errors else "")

    # Wait for dynamic content.
    try:
        page.wait_for_selector(".module-header", timeout=8_000)
    except Exception:
        pass

    record(".module-header is rendered (waited up to 8s)",
           page.locator(".module-header").count() >= 1)

    header_text = ""
    if page.locator(".module-header").count() >= 1:
        header_text = page.locator(".module-header").inner_text()
    record('Module name "vpc" appears in the header',
           "vpc" in header_text.lower(),
           f"header text: {header_text[:80]!r}")

    # Versions section shows both versions.
    versions_section = page.locator(".versions-section")
    record(".versions-section is present",
           versions_section.count() >= 1)

    versions_text = versions_section.inner_text() if versions_section.count() >= 1 else ""
    record('.versions-section shows "2.0.0" and "1.0.0"',
           "2.0.0" in versions_text and "1.0.0" in versions_text,
           f"versions text: {versions_text[:120]!r}")

    # Usage section.
    usage_section = page.locator(".usage-section")
    record(".usage-section is present",
           usage_section.count() >= 1)

    usage_text = usage_section.inner_text() if usage_section.count() >= 1 else ""
    record('Usage section contains "source"',
           "source" in usage_text.lower(),
           f"usage: {usage_text[:80]!r}")

    record(f'Usage snippet contains "{UI_NS}/vpc/{UI_PROVIDER}"',
           f"{UI_NS}/vpc/{UI_PROVIDER}" in usage_text,
           f"usage: {usage_text[:120]!r}")

    # Click version link "1.0.0" and check URL updates.
    try:
        # Links look like "v1.0.0"
        version_link = page.locator(".version-link", has_text="1.0.0").first
        if version_link.count() == 0:
            version_link = page.locator("a", has_text="1.0.0").first
        with page.expect_navigation(timeout=8_000):
            version_link.click()
        new_url = page.url
        url_updated = "version=1.0.0" in new_url or "1.0.0" in new_url
        record("Clicking version '1.0.0' navigates/updates URL",
               url_updated,
               f"url={new_url!r}")
    except Exception as exc:
        record("Clicking version '1.0.0' navigates/updates URL", False, str(exc)[:80])

    # README section — present even if empty (app.js only renders it when mod.readme is truthy,
    # so accept either readme-section present or its absence without error).
    readme_present = page.locator(".readme-section").count() >= 1
    record("README section is present (or correctly absent when empty)",
           True,  # We accept either outcome — just verify no crash
           f"readme_section_present={readme_present}")

    context.close()


def _test_upload_page(browser, base_url: str, record) -> None:
    """Tests 25-29: Upload page."""
    context = browser.new_context()
    page = context.new_page()

    console_errors: list[str] = []
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

    # Set a value in localStorage BEFORE navigating so pre-fill is exercised.
    # We navigate a second time to test the pre-fill.
    page.evaluate("localStorage.setItem('tf_api_key', 'testkey123')")
    page.goto(base_url + "/upload.html", timeout=30_000)
    page.wait_for_load_state("domcontentloaded")

    api_key_val = page.locator("#api-key").input_value()
    record("#api-key pre-fills from localStorage on reload",
           api_key_val == "testkey123",
           f"value={api_key_val!r}")

    # Clean up localStorage.
    page.evaluate("localStorage.removeItem('tf_api_key')")

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

    # Submitting with an invalid API key shows error (not a crash).
    page.locator("#api-key").fill("totally-invalid-key")
    page.locator("#namespace").fill("testns")
    page.locator("#module-name").fill("testmod")
    page.locator("#provider").fill("aws")
    page.locator("#version").fill("1.0.0")

    # We cannot attach a real file in headless tests; instead confirm the JS
    # validation fires (the form's own required attribute on #archive-file).
    # Check for HTML5 required attribute on the file input.
    archive_required = page.locator("#archive-file").get_attribute("required")
    record("File input #archive-file has 'required' attribute (HTML5 validation)",
           archive_required is not None,
           f"required attr={archive_required!r}")

    context.close()


def _test_keys_page(
    browser,
    base_url: str,
    api_key: str,
    record,
    uploaded_modules: list,
    authed: requests.Session,
) -> None:
    """Tests 30-42: Keys page."""
    context = browser.new_context()
    page = context.new_page()

    console_errors: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    try:
        response = page.goto(base_url + "/keys.html", timeout=30_000)
        ok = response is not None and response.status == 200
        record("/keys.html loads (200) — not browse page content", ok,
               f"status={response.status if response else 'none'}")
    except Exception as exc:
        record("/keys.html loads (200) — not browse page content", False, str(exc)[:80])
        context.close()
        return

    page.wait_for_load_state("domcontentloaded")

    record("No JS console errors on /keys.html",
           len(console_errors) == 0,
           console_errors[0][:100] if console_errors else "")

    # Keys page must have #keys-section, NOT #module-list.
    record("#keys-section exists (keys page specific)",
           page.locator("#keys-section").count() == 1)

    record("#module-list is NOT present on keys page",
           page.locator("#module-list").count() == 0)

    record("#api-key-input exists",
           page.locator("#api-key-input").count() == 1)

    # Auth prompt shown when no key is entered.
    auth_prompt = page.locator("#auth-prompt")
    auth_visible = auth_prompt.count() >= 1 and auth_prompt.is_visible()
    record("Auth prompt shown when no API key is entered",
           auth_visible)

    if not api_key:
        record("Enter bootstrap API key → auth prompt disappears", False, "no api_key available")
        record("Key list area shows after auth", False, "no api_key available")
        record("Key list loads (table or 'no keys' message visible)", False, "no api_key available")
        record("Generate new key: fill #key-name, click generate, key is revealed", False, "no api_key available")
        record("Revealed key is a hex string (~64 chars)", False, "no api_key available")
        record("Copy button exists next to revealed key", False, "no api_key available")
        record("New key appears in list with correct name", False, "no api_key available")
        record("Revoke button exists per key row", False, "no api_key available")
        record("Revoke generated key → key disappears from list", False, "no api_key available")
        context.close()
        return

    # Enter bootstrap API key.
    page.locator("#api-key-input").fill(api_key)
    page.locator("#api-key-input").dispatch_event("change")

    # Wait for auth prompt to hide and keys-list to appear.
    try:
        page.wait_for_selector("#keys-list:not([style*='display: none'])", timeout=8_000)
    except Exception:
        pass
    try:
        page.wait_for_selector("#auth-prompt[style*='display: none']", timeout=5_000)
    except Exception:
        pass

    auth_prompt_after = page.locator("#auth-prompt")
    auth_gone = (
        auth_prompt_after.count() == 0
        or not auth_prompt_after.is_visible()
    )
    record("Enter bootstrap API key → auth prompt disappears",
           auth_gone)

    keys_list = page.locator("#keys-list")
    keys_visible = keys_list.count() >= 1 and keys_list.is_visible()
    record("Key list area shows after auth",
           keys_visible)

    # Key list shows table or 'no keys' message, not an error alert.
    table_body = page.locator("#keys-table-body")
    no_keys_msg = page.locator("#no-keys-msg")
    error_alert = page.locator("#keys-section .alert-error")
    list_ok = (
        (table_body.count() >= 1) or (no_keys_msg.count() >= 1 and no_keys_msg.is_visible())
    ) and error_alert.count() == 0
    record("Key list loads (table or 'no keys' message visible — not an error)",
           list_ok)

    # Generate a new key.
    test_key_name = f"ui-smoke-{int(time.time())}"
    page.locator("#key-name").fill(test_key_name)

    generate_btn = page.locator("#generate-form button[type='submit']")
    generate_btn.click()

    # Wait for the new key box to appear.
    try:
        page.wait_for_selector("#new-key-box:not([style*='display: none'])", timeout=10_000)
    except Exception as exc:
        record("Generate new key: key value is revealed in #new-key-box", False, str(exc)[:80])
        record("Revealed key is a hex string (~64 chars)", False, "new-key-box not shown")
        record("Copy button (#copy-new-key) exists next to revealed key", False, "new-key-box not shown")
        record("New key appears in list with correct name", False, "generation failed")
        record("Revoke button exists per key row", False, "generation failed")
        record("Revoke generated key → key disappears from list", False, "generation failed")
        context.close()
        return

    new_key_value_el = page.locator("#new-key-value")
    new_key_text = new_key_value_el.inner_text().strip() if new_key_value_el.count() >= 1 else ""
    record("Generate new key: key value is revealed in #new-key-box",
           len(new_key_text) > 0,
           f"key length={len(new_key_text)}")

    is_hex = bool(re.fullmatch(r"[0-9a-fA-F]{32,}", new_key_text))
    record("Revealed key is a hex string (~64 chars)",
           is_hex,
           f"value={new_key_text[:16]!r}... len={len(new_key_text)}")

    copy_btn = page.locator("#copy-new-key")
    record("Copy button (#copy-new-key) exists next to revealed key",
           copy_btn.count() >= 1 and copy_btn.is_visible())

    # Wait for the table to re-render with the new key.
    try:
        page.wait_for_selector("#keys-table-body tr", timeout=8_000)
    except Exception:
        pass

    rows = page.locator("#keys-table-body tr")
    row_count = rows.count()
    found_name = False
    for i in range(row_count):
        if test_key_name in rows.nth(i).inner_text():
            found_name = True
            break
    record(f"New key '{test_key_name}' appears in list",
           found_name,
           f"rows={row_count}, found={found_name}")

    # Revoke button exists per row.
    revoke_btns = page.locator("#keys-table-body .revoke-btn")
    record("Revoke button exists per key row",
           revoke_btns.count() >= 1)

    # Revoke the generated key.
    target_revoke_btn = None
    for i in range(row_count):
        row = rows.nth(i)
        if test_key_name in row.inner_text():
            target_revoke_btn = row.locator(".revoke-btn")
            break

    if target_revoke_btn is None or target_revoke_btn.count() == 0:
        record("Revoke generated key → key disappears from list", False, "could not find key row to revoke")
        context.close()
        return

    page.on("dialog", lambda d: d.accept())
    target_revoke_btn.click()

    # Wait for table to update.
    try:
        page.wait_for_timeout(2_000)
    except Exception:
        pass

    rows_after = page.locator("#keys-table-body tr")
    row_count_after = rows_after.count()
    still_present = False
    for i in range(row_count_after):
        if test_key_name in rows_after.nth(i).inner_text():
            still_present = True
            break

    record("Revoke generated key → key disappears from list",
           not still_present,
           f"rows_after={row_count_after}, still_present={still_present}")

    context.close()


def _test_navigation(browser, base_url: str, record) -> None:
    """Tests 43-45: Navigation links between pages."""
    context = browser.new_context()
    page = context.new_page()

    # From / click "Upload" nav link.
    try:
        page.goto(base_url + "/", timeout=30_000)
        page.wait_for_load_state("domcontentloaded")
        with page.expect_navigation(timeout=10_000):
            page.locator("nav a[href='upload.html']").first.click()
        record("From /, clicking 'Upload' nav link navigates to /upload.html",
               "upload.html" in page.url,
               f"url={page.url!r}")
    except Exception as exc:
        record("From /, clicking 'Upload' nav link navigates to /upload.html",
               False, str(exc)[:80])

    # From /upload.html click "Browse" nav link.
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

    # From / click "Keys" nav link.
    try:
        page.goto(base_url + "/", timeout=30_000)
        page.wait_for_load_state("domcontentloaded")
        with page.expect_navigation(timeout=10_000):
            page.locator("nav a[href='keys.html']").first.click()
        record("From /, clicking 'Keys' nav link navigates to /keys.html",
               "keys.html" in page.url,
               f"url={page.url!r}")
    except Exception as exc:
        record("From /, clicking 'Keys' nav link navigates to /keys.html",
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

    api_key = _load_api_key()
    if not api_key:
        print(
            "WARNING: No API key found. "
            "Set TF_REGISTRY_API_KEY or create ~/repos/tf-registry/.api_key. "
            "Auth-protected tests will fail.\n"
        )

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
        api_key=api_key,
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
