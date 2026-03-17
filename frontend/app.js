/* Terraform Module Registry - Frontend Application */

const API_BASE = "";

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------

function qs(selector, root = document) {
  return root.querySelector(selector);
}

function qsa(selector, root = document) {
  return root.querySelectorAll(selector);
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function relativeTime(iso) {
  if (!iso) return "";
  const now = Date.now();
  const then = new Date(iso).getTime();
  const diff = Math.floor((now - then) / 1000);
  if (diff < 60) return "just now";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  if (diff < 2592000) return Math.floor(diff / 86400) + "d ago";
  return new Date(iso).toLocaleDateString();
}

async function apiFetch(path, opts = {}) {
  const res = await fetch(API_BASE + path, opts);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function showAlert(container, message, type = "error") {
  const div = document.createElement("div");
  div.className = `alert alert-${type}`;
  div.textContent = message;
  container.prepend(div);
  setTimeout(() => div.remove(), 6000);
}

// ---------------------------------------------------------------------------
// JWT localStorage persistence
// ---------------------------------------------------------------------------

function getToken() {
  return localStorage.getItem("tf_jwt") || "";
}

function setToken(token) {
  localStorage.setItem("tf_jwt", token);
}

function clearToken() {
  localStorage.removeItem("tf_jwt");
}

function authHeaders() {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// ---------------------------------------------------------------------------
// Browse page (index.html)
// ---------------------------------------------------------------------------

function initBrowsePage() {
  const listEl = qs("#module-list");
  const searchInput = qs("#search-input");
  const namespaceFilter = qs("#namespace-filter");
  const prevBtn = qs("#prev-btn");
  const nextBtn = qs("#next-btn");
  const pageInfo = qs("#page-info");

  if (!listEl) return;

  let currentOffset = 0;
  const limit = 20;

  async function loadModules() {
    listEl.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    const params = new URLSearchParams();
    params.set("offset", currentOffset);
    params.set("limit", limit);

    const q = searchInput ? searchInput.value.trim() : "";
    if (q) params.set("q", q);

    const ns = namespaceFilter ? namespaceFilter.value : "";
    if (ns) params.set("namespace", ns);

    try {
      const data = await apiFetch("/v1/modules?" + params.toString());
      renderModuleList(listEl, data.modules, data.meta);
      updatePagination(data.meta);
      updateNamespaceOptions(data.modules);
    } catch (err) {
      listEl.innerHTML = "";
      showAlert(listEl, err.message);
    }
  }

  function renderModuleList(container, modules, meta) {
    if (modules.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <h2>No modules found</h2>
          <p>Try adjusting your search or <a href="upload.html">upload a module</a>.</p>
        </div>`;
      return;
    }

    container.innerHTML = modules
      .map(
        (m) => `
      <div class="module-card" data-href="module.html?namespace=${encodeURIComponent(m.namespace)}&name=${encodeURIComponent(m.name)}&provider=${encodeURIComponent(m.provider)}">
        <div class="module-card-header">
          <div class="module-card-title">
            <span class="namespace">${escapeHtml(m.namespace)}</span> /
            <span class="name">${escapeHtml(m.name)}</span>
          </div>
          <span class="module-card-version">v${escapeHtml(m.version)}</span>
        </div>
        ${m.description ? `<div class="module-card-description">${escapeHtml(m.description)}</div>` : ""}
        <div class="module-card-meta">
          <span>Provider: ${escapeHtml(m.provider)}</span>
          <span>Downloads: ${m.downloads}</span>
          <span>${relativeTime(m.published_at)}</span>
        </div>
      </div>`
      )
      .join("");

    container.querySelectorAll(".module-card").forEach((card) => {
      card.addEventListener("click", () => {
        window.location.href = card.dataset.href;
      });
    });
  }

  function updatePagination(meta) {
    if (!prevBtn || !nextBtn || !pageInfo) return;
    const page = Math.floor(meta.offset / limit) + 1;
    const totalPages = Math.max(1, Math.ceil(meta.total / limit));
    pageInfo.textContent = `Page ${page} of ${totalPages} (${meta.total} modules)`;
    prevBtn.disabled = meta.offset === 0;
    nextBtn.disabled = meta.offset + limit >= meta.total;
  }

  function updateNamespaceOptions(modules) {
    if (!namespaceFilter || namespaceFilter.options.length > 1) return;
    const namespaces = [...new Set(modules.map((m) => m.namespace))].sort();
    namespaces.forEach((ns) => {
      const opt = document.createElement("option");
      opt.value = ns;
      opt.textContent = ns;
      namespaceFilter.appendChild(opt);
    });
  }

  if (searchInput) {
    let timer;
    searchInput.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        currentOffset = 0;
        loadModules();
      }, 300);
    });
  }

  if (namespaceFilter) {
    namespaceFilter.addEventListener("change", () => {
      currentOffset = 0;
      loadModules();
    });
  }

  if (prevBtn) {
    prevBtn.addEventListener("click", () => {
      currentOffset = Math.max(0, currentOffset - limit);
      loadModules();
    });
  }

  if (nextBtn) {
    nextBtn.addEventListener("click", () => {
      currentOffset += limit;
      loadModules();
    });
  }

  loadModules();
}

// ---------------------------------------------------------------------------
// Module detail page (module.html)
// ---------------------------------------------------------------------------

function initModulePage() {
  const detailEl = qs("#module-detail");
  if (!detailEl) return;

  const params = new URLSearchParams(window.location.search);
  const namespace = params.get("namespace");
  const name = params.get("name");
  const provider = params.get("provider");
  const version = params.get("version");

  if (!namespace || !name || !provider) {
    detailEl.innerHTML = '<div class="empty-state"><h2>Missing module parameters</h2></div>';
    return;
  }

  loadModuleDetail(detailEl, namespace, name, provider, version);
}

async function loadModuleDetail(container, namespace, name, provider, version) {
  container.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

  try {
    const versionPath = version ? `/${version}` : "";
    const moduleData = await apiFetch(
      `/v1/modules/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/${encodeURIComponent(provider)}${versionPath}`
    );

    const versionsData = await apiFetch(
      `/v1/modules/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/${encodeURIComponent(provider)}/versions`
    );

    const versions =
      versionsData.modules && versionsData.modules[0]
        ? versionsData.modules[0].versions
        : [];

    renderModuleDetail(container, moduleData, versions, namespace, name, provider);
  } catch (err) {
    container.innerHTML = "";
    showAlert(container, err.message);
  }
}

function renderModuleDetail(container, mod, versions, namespace, name, provider) {
  const registryHost = window.location.host;
  const usageSnippet = `module "${escapeHtml(name)}" {
  source  = "${registryHost}/${escapeHtml(namespace)}/${escapeHtml(name)}/${escapeHtml(provider)}"
  version = "${escapeHtml(mod.version)}"
}`;

  const versionsHtml = versions
    .map(
      (v) => `
    <tr>
      <td>
        <a class="version-link" href="module.html?namespace=${encodeURIComponent(namespace)}&name=${encodeURIComponent(name)}&provider=${encodeURIComponent(provider)}&version=${encodeURIComponent(v.version)}">
          v${escapeHtml(v.version)}
        </a>
      </td>
    </tr>`
    )
    .join("");

  container.innerHTML = `
    <div class="module-header">
      <h1>
        <span class="namespace">${escapeHtml(namespace)}</span> /
        ${escapeHtml(name)}
        <span class="provider-badge">${escapeHtml(provider)}</span>
      </h1>
      <div class="module-meta-row">
        <span>Version: <strong>v${escapeHtml(mod.version)}</strong></span>
        <span>Downloads: ${mod.downloads}</span>
        <span>Published: ${relativeTime(mod.published_at)}</span>
        ${mod.source_url ? `<span><a href="${escapeHtml(mod.source_url)}" target="_blank" rel="noopener">Source</a></span>` : ""}
      </div>
      ${mod.description ? `<p style="margin-top:0.75rem;color:var(--text-secondary)">${escapeHtml(mod.description)}</p>` : ""}
    </div>

    <div class="usage-section">
      <h2>Usage</h2>
      <pre><code>${usageSnippet}</code><button class="copy-btn" onclick="copyUsage(this)">Copy</button></pre>
    </div>

    ${
      mod.readme
        ? `
    <div class="readme-section">
      <h2>README</h2>
      <div class="readme-content">${escapeHtml(mod.readme)}</div>
    </div>`
        : ""
    }

    <div class="versions-section">
      <h2>Versions</h2>
      ${
        versions.length > 0
          ? `<table class="version-table">
              <thead><tr><th>Version</th></tr></thead>
              <tbody>${versionsHtml}</tbody>
            </table>`
          : '<p style="color:var(--text-secondary)">No versions available.</p>'
      }
    </div>
  `;
}

function copyUsage(btn) {
  const code = btn.previousElementSibling.textContent;
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = "Copied!";
    setTimeout(() => {
      btn.textContent = "Copy";
    }, 2000);
  });
}

// Make copyUsage globally available
window.copyUsage = copyUsage;

// ---------------------------------------------------------------------------
// Upload page (upload.html)
// ---------------------------------------------------------------------------

function initUploadPage() {
  const form = qs("#upload-form");
  if (!form) return;

  const authMsg = qs("#auth-required-msg");
  const token = getToken();

  if (!token) {
    if (authMsg) authMsg.style.display = "";
    form.querySelectorAll("input, textarea, button").forEach((el) => (el.disabled = true));
    return;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();

    const namespace = qs("#namespace", form).value.trim();
    const moduleName = qs("#module-name", form).value.trim();
    const provider = qs("#provider", form).value.trim();
    const version = qs("#version", form).value.trim();
    const description = qs("#description", form).value.trim();
    const sourceUrl = qs("#source-url", form).value.trim();
    const fileInput = qs("#archive-file", form);

    if (!namespace || !moduleName || !provider || !version) {
      showAlert(form, "Please fill in all required fields.");
      return;
    }

    if (!fileInput.files || fileInput.files.length === 0) {
      showAlert(form, "Please select a .tar.gz file.");
      return;
    }

    const file = fileInput.files[0];
    const formData = new FormData();
    formData.append("file", file);

    const params = new URLSearchParams();
    if (description) params.set("description", description);
    if (sourceUrl) params.set("source_url", sourceUrl);

    const submitBtn = qs('button[type="submit"]', form);
    submitBtn.disabled = true;
    submitBtn.textContent = "Uploading...";

    try {
      const url = `${API_BASE}/v1/modules/${encodeURIComponent(namespace)}/${encodeURIComponent(moduleName)}/${encodeURIComponent(provider)}/${encodeURIComponent(version)}?${params.toString()}`;

      const res = await fetch(url, {
        method: "POST",
        headers: authHeaders(),
        body: formData,
      });

      if (res.status === 401) {
        clearToken();
        showAlert(form, "Session expired. Please sign in again.");
        return;
      }

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }

      showAlert(form, `Successfully uploaded ${namespace}/${moduleName}/${provider} v${version}`, "success");

      qs("#namespace", form).value = "";
      qs("#module-name", form).value = "";
      qs("#provider", form).value = "";
      qs("#version", form).value = "";
      qs("#description", form).value = "";
      qs("#source-url", form).value = "";
      fileInput.value = "";
    } catch (err) {
      showAlert(form, err.message);
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Upload Module";
    }
  });
}

// ---------------------------------------------------------------------------
// Login page (login.html)
// ---------------------------------------------------------------------------

function initLoginPage() {
  const loginCard = qs("#login-card");
  const loggedInBox = qs("#logged-in-box");
  if (!loginCard && !loggedInBox) return;

  function showLoggedIn() {
    if (loginCard) loginCard.style.display = "none";
    if (loggedInBox) loggedInBox.style.display = "";
  }

  function showLoginForm() {
    if (loginCard) loginCard.style.display = "";
    if (loggedInBox) loggedInBox.style.display = "none";
  }

  if (getToken()) {
    showLoggedIn();
  } else {
    showLoginForm();
  }

  const form = qs("#login-form");
  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const username = qs("#username").value.trim();
      const password = qs("#password").value;
      const submitBtn = qs('button[type="submit"]', form);
      submitBtn.disabled = true;
      submitBtn.textContent = "Signing in...";

      try {
        const res = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username, password }),
        });

        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `HTTP ${res.status}`);
        }

        const data = await res.json();
        setToken(data.access_token);
        showLoggedIn();
      } catch (err) {
        showAlert(qs("#login-section"), err.message);
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "Sign in";
      }
    });
  }

  const logoutBtn = qs("#logout-btn");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", () => {
      clearToken();
      showLoginForm();
    });
  }
}

// ---------------------------------------------------------------------------
// Router — run the right init for each page
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  initBrowsePage();
  initModulePage();
  initUploadPage();
  initLoginPage();
});
