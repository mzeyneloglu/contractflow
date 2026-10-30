const STATUS_TEXT = {
  PASS: "GEÇTİ", FAIL: "BAŞARISIZ", INCONCLUSIVE: "BELİRSİZ", ERROR: "HATA", CANCELLED: "İPTAL", SKIPPED: "ATLANDI",
  RUNNING: "ÇALIŞIYOR", NEEDS_CLARIFICATION: "ONAY / AÇIKLAMA", WAITING_FOR_SERVICE: "SERVİS BEKLENİYOR",
  WAITING_FOR_OLLAMA: "OLLAMA BEKLENİYOR", WAITING_FOR_DOCUMENTS: "BELGE BEKLENİYOR", AUTH_REQUIRED: "YETKİ GEREKLİ",
  ready: "hazır", pending: "bekliyor", failed: "hatalı", unknown: "bilinmiyor", queued: "kuyrukta", parsing: "ayrıştırılıyor",
  embedding: "embed ediliyor", needs_ocr: "OCR gerekli", superseded: "eski sürüm",
};
const TERMINAL = new Set(["PASS", "FAIL", "INCONCLUSIVE", "ERROR", "CANCELLED"]);
const STAGES = [
  { node: "preflight", title: "Ön kontrol", icon: "🛫", hint: "Ollama, belge, sözleşme ve Oracle hazır mı" },
  { node: "planner", title: "Planlama", icon: "🧭", hint: "Talepten kaynak, filtre ve beklenti" },
  { node: "rules", title: "İş Kuralı", icon: "📚", hint: "Belgelerde kural ve istisna araştırması" },
  { node: "api", title: "API", icon: "🔌", hint: "Doğru GET operasyonu ve parametreler" },
  { node: "code", title: "Kod Analizi", icon: "🧩", hint: "Endpoint → servis → SQL zinciri" },
  { node: "oracle", title: "Oracle", icon: "🗄️", hint: "Şema keşfi ve eşdeğer SELECT" },
  { node: "gate", title: "Onay", icon: "🛡️", hint: "Eşleştirme ve SQL onayı" },
  { node: "execute", title: "Yürütme", icon: "⚡", hint: "API tüm sayfalar + MCP SELECT" },
  { node: "compare", title: "Karşılaştırma", icon: "⚖️", hint: "Üç bağımsız assertion" },
  { node: "analyst", title: "Analiz", icon: "📝", hint: "Kanıtlı özet ve hipotezler" },
];
const WAITING_STATUSES = new Set(["NEEDS_CLARIFICATION", "WAITING_FOR_SERVICE", "WAITING_FOR_OLLAMA", "WAITING_FOR_DOCUMENTS", "AUTH_REQUIRED"]);

const app = { projects: [], projectId: localStorage.getItem("cf.project") || "", cleanup: [] };
const SNAPSHOT = new URLSearchParams(location.search).has("snapshot");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));
}

function badge(status) {
  return `<span class="badge ${escapeHtml(status)}">${escapeHtml(STATUS_TEXT[status] || status)}</span>`;
}

function toast(message, isError = false) {
  const element = document.createElement("div");
  element.className = `toast${isError ? " error" : ""}`;
  element.textContent = message;
  document.getElementById("toasts").appendChild(element);
  setTimeout(() => element.remove(), isError ? 7000 : 3500);
}

async function api(path, options = {}) {
  const init = { ...options };
  if (init.json !== undefined) {
    init.body = JSON.stringify(init.json);
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
    delete init.json;
  }
  const response = await fetch(path, init);
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const detail = data && (data.message || data.detail);
    throw new Error(detail ? (typeof detail === "string" ? detail : JSON.stringify(detail)) : `HTTP ${response.status}`);
  }
  return data;
}

async function withBusy(button, action) {
  button.disabled = true;
  try {
    return await action();
  } catch (error) {
    toast(error.message, true);
    return null;
  } finally {
    button.disabled = false;
  }
}

function time(iso) {
  return iso ? new Date(iso).toLocaleTimeString("tr-TR") : "";
}

function duration(seconds) {
  if (seconds == null) return "";
  const total = Math.max(0, Math.round(seconds));
  return total >= 60 ? `${Math.floor(total / 60)} dk ${total % 60} sn` : `${total} sn`;
}

function sourceLabel(source) {
  const location = source.location || {};
  const parts = [];
  if (location.section_path && location.section_path.length) parts.push(location.section_path.join(" › "));
  if (location.page) parts.push(`sayfa ${location.page}`);
  if (location.line_start) parts.push(`satır ${location.line_start}${location.line_end && location.line_end !== location.line_start ? "-" + location.line_end : ""}`);
  return `${source.source_filename}${parts.length ? ` (${parts.join(", ")})` : ""}`;
}

function progressBar(value, total, label = "") {
  const percent = total ? Math.round((value / total) * 100) : 0;
  return `${label ? `<div class="progress-label"><span>${escapeHtml(label)}</span><span>${value}/${total}</span></div>` : ""}<div class="progress"><span style="width:${percent}%"></span></div>`;
}

function readinessCount(project) {
  return Object.values(project.readiness).filter((status) => status.state === "ready").length;
}

function selectProject(projectId) {
  app.projectId = projectId;
  localStorage.setItem("cf.project", projectId);
  renderProjectPicker();
}

async function loadProjects() {
  app.projects = await api("/api/projects");
  if (!app.projects.some((project) => project.id === app.projectId)) app.projectId = app.projects[0] ? app.projects[0].id : "";
  renderProjectPicker();
}

function renderProjectPicker() {
  const select = document.getElementById("project-select");
  select.innerHTML = app.projects.length ? app.projects.map((project) => `<option value="${project.id}" ${project.id === app.projectId ? "selected" : ""}>${escapeHtml(project.name)}</option>`).join("") : `<option value="">Proje yok</option>`;
  document.querySelectorAll("#nav a[data-needs-project]").forEach((link) => {
    link.classList.toggle("disabled", !app.projectId);
    link.href = app.projectId ? `#/projects/${app.projectId}/${link.dataset.view}` : "#/projects";
  });
}

document.getElementById("project-select").addEventListener("change", (event) => {
  selectProject(event.target.value);
  const current = location.hash.split("/")[3] || "verify";
  location.hash = `#/projects/${app.projectId}/${["setup", "verify", "history"].includes(current) ? current : "verify"}`;
});

function setActiveNav(view) {
  document.querySelectorAll("#nav a").forEach((link) => link.classList.toggle("active", link.dataset.view === view));
}

function cleanup() {
  app.cleanup.forEach((dispose) => dispose());
  app.cleanup = [];
}

async function route() {
  cleanup();
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const container = document.getElementById("view");
  container.innerHTML = "";
  container.style.animation = "none";
  void container.offsetWidth;
  container.style.animation = "";
  try {
    if (parts[0] === "projects" && parts[1]) {
      selectProject(parts[1]);
      const view = parts[2] || "verify";
      setActiveNav(view);
      if (view === "setup") return await renderSetup(container, parts[1], parts[3]);
      if (view === "history") return await renderHistory(container, parts[1]);
      return await renderVerify(container, parts[1], parts[3]);
    }
    if (parts[0] === "runs" && parts[1]) {
      setActiveNav("history");
      return await renderReport(container, parts[1]);
    }
    setActiveNav("projects");
    await renderProjects(container);
  } catch (error) {
    container.innerHTML = `<div class="card callout fail">${escapeHtml(error.message)}</div>`;
  }
}

window.addEventListener("hashchange", route);

async function renderProjects(container) {
  await loadProjects();
  const cards = app.projects.map((project) => {
    const ready = readinessCount(project);
    const items = [["Ollama", project.readiness.ollama], ["Oracle", project.readiness.oracle], ["API", project.readiness.api], ["Bilgi tabanı", project.readiness.knowledge_base]];
    return `<article class="card project-card ${project.id === app.projectId ? "selected" : ""}" data-project="${project.id}">
      <header><div><h2>${escapeHtml(project.name)}</h2><span class="muted">${escapeHtml(project.environment)} · ${escapeHtml(project.api.base_url || "API tanımsız")}</span></div>
      <span class="badge ${ready === 4 ? "ready" : "pending"}">${ready === 4 ? "Hazır" : "Kurulum sürüyor"}</span></header>
      <div>${progressBar(ready, 4, "Hazırlık")}</div>
      <div class="ready-list">${items.map(([title, status]) => `<div class="ready-item ${status.state}" title="${escapeHtml(status.message)}"><i></i>${title}</div>`).join("")}</div>
      <div class="row" style="margin-top:0">
        <a class="button" href="#/projects/${project.id}/verify">Doğrula</a>
        <a class="button secondary" href="#/projects/${project.id}/setup">Kurulum</a>
        <a class="button secondary" href="#/projects/${project.id}/history">Geçmiş</a>
      </div>
    </article>`;
  }).join("");
  container.innerHTML = `
    <div class="page-head"><div><h1>Projeler</h1><p>Her servis/repo ayrı bir projedir; bağlantılar, belgeler, kod ve eşleştirme hafızası projeler arasında paylaşılmaz.</p></div></div>
    <div class="grid three">${cards}
      <article class="card new-card">
        <h2>Yeni proje</h2>
        <label for="new-name">Proje adı</label><input id="new-name" placeholder="ör. Hedef servisi">
        <label for="new-env">Ortam</label><input id="new-env" value="local">
        <div class="row"><button id="create-project">Taslak oluştur</button></div>
        <p class="muted" style="font-size:13px">Eksik bağlantılarla kaydedilir; kurulumda tamamlanır.</p>
      </article>
    </div>`;
  container.querySelectorAll(".project-card").forEach((card) => card.addEventListener("click", (event) => {
    if (event.target.closest("a")) return;
    selectProject(card.dataset.project);
    container.querySelectorAll(".project-card").forEach((item) => item.classList.toggle("selected", item === card));
  }));
  container.querySelector("#create-project").addEventListener("click", (event) => withBusy(event.target, async () => {
    const project = await api("/api/projects", { method: "POST", json: { name: container.querySelector("#new-name").value, environment: container.querySelector("#new-env").value } });
    selectProject(project.id);
    location.hash = `#/projects/${project.id}/setup`;
  }));
}

async function renderSetup(container, projectId, requestedStep) {
  let project = await api(`/api/projects/${projectId}`);
  let activeStep = requestedStep || sessionStorage.getItem("cf.setupStep") || "oracle";
  const value = (id) => container.querySelector(`#${id}`).value;
  const setValue = (id, data) => { container.querySelector(`#${id}`).value = data ?? ""; };

  function steps() {
    return [
      { id: "oracle", title: "Oracle", done: project.readiness.oracle.state === "ready" },
      { id: "api", title: "API ve OpenAPI", done: project.readiness.api.state === "ready" && !project.is_draft },
      { id: "code", title: "Kod ve SQL izi", done: (project.code_paths || []).length > 0 || project.oracle.capture_runtime_sql || Boolean(project.sql_log_path) },
      { id: "documents", title: "Belgeler ve model", done: project.readiness.knowledge_base.state === "ready" && project.readiness.ollama.state === "ready" },
      { id: "advanced", title: "Gelişmiş", done: false },
    ];
  }

  function renderHeader() {
    const list = steps();
    const done = list.slice(0, 4).filter((step) => step.done).length;
    container.querySelector("#setup-head").innerHTML = `
      <div class="page-head"><div><h1>Kurulum · ${escapeHtml(project.name)}</h1><p>Gizli bilgiler işletim sistemi deposunda saklanır; modele, loglara ve Git'e gönderilmez.</p></div>
      <div style="min-width:260px">${progressBar(done, 4, "Kurulum ilerlemesi")}</div></div>
      <div class="stepper">${list.map((step, index) => `<div class="step ${step.id === activeStep ? "active" : ""} ${step.done ? "done" : ""}" data-step="${step.id}"><b><span class="num">${step.done ? "✓" : index + 1}</span>${step.title}</b><small>${step.done ? "tamam" : "bekliyor"}</small></div>`).join("")}</div>`;
    container.querySelectorAll(".step").forEach((element) => element.addEventListener("click", () => { activeStep = element.dataset.step; sessionStorage.setItem("cf.setupStep", activeStep); showStep(); }));
  }

  function showStep() {
    renderHeader();
    container.querySelectorAll(".setup-step").forEach((section) => section.classList.toggle("hidden", section.dataset.step !== activeStep));
  }

  function accept(updated) {
    if (updated) { project = updated; fill(); renderHeader(); }
  }

  container.innerHTML = `<div id="setup-head"></div>
  <section class="card setup-step" data-step="oracle">
    <h2>Oracle bağlantısı</h2>
    <div class="grid two">
      <div><label for="o-host">Host</label><input id="o-host" placeholder="db.sirket.local"></div>
      <div><label for="o-port">Port</label><input id="o-port" type="number" value="1521"></div>
      <div><label for="o-service">Service name</label><input id="o-service"></div>
      <div><label for="o-user">Kullanıcı</label><input id="o-user" autocomplete="off"></div>
      <div><label for="o-password">Parola <span id="o-password-state"></span></label><input id="o-password" type="password" autocomplete="new-password" placeholder="Değiştirmek için girin"></div>
      <div><label for="o-schemas">İzinli şemalar (virgülle)</label><input id="o-schemas"></div>
    </div>
    <h3>Çalışma anı SQL yakalama</h3>
    <label class="check"><input id="o-capture" type="checkbox"> Servis tetiklendiğinde çalıştırdığı SQL'i V$SQL'den yakala (V$SQL / V$SESSION okuma yetkisi gerekir)</label>
    <label for="o-service-users">Servisin DB kullanıcıları (virgülle; boşsa izinli şemalar)</label><input id="o-service-users" placeholder="ör. SERVICE_APP">
    <div class="row"><button id="save-oracle">Kaydet</button><button id="test-oracle" class="secondary">Bağlantıyı test et</button></div>
    <div id="oracle-status"></div>
  </section>
  <section class="card setup-step" data-step="api">
    <h2>Hedef servis</h2>
    <div class="grid two">
      <div><label for="a-base">Base URL</label><input id="a-base" placeholder="http://localhost:8080"></div>
      <div><label for="a-health">Sağlık kontrol yolu (opsiyonel)</label><input id="a-health" placeholder="/actuator/health"></div>
      <div><label for="a-auth">Kimlik doğrulama</label><select id="a-auth"><option value="none">Yok</option><option value="bearer">Bearer token</option><option value="basic">Basic</option><option value="api_key_header">API key (header)</option><option value="api_key_query">API key (query)</option></select></div>
      <div><label for="a-user">Kullanıcı (Basic)</label><input id="a-user" autocomplete="off"></div>
      <div><label for="a-key-name">API key adı</label><input id="a-key-name"></div>
      <div><label for="a-credential">Token / parola / API key <span id="a-credential-state"></span></label><input id="a-credential" type="password" autocomplete="new-password" placeholder="Değiştirmek için girin"></div>
    </div>
    <h3>Sabit header'lar</h3>
    <p class="muted" style="font-size:13px">Değerler gizli depoda saklanır; raporda ve curl'de gizlenir. <code>{uuid}</code> her istekte yeni UUID üretir. Kayıtlı değeri boş bırakırsanız korunur.</p>
    <table class="header-rows"><thead><tr><th style="width:38%">Ad</th><th>Değer</th><th style="width:48px"></th></tr></thead><tbody id="header-rows"></tbody></table>
    <div class="row"><button id="add-header" class="secondary" type="button">+ Header</button><button id="required-headers" class="secondary" type="button">Swagger'daki ortak zorunlu header'ları getir</button></div>
    <label class="check"><input id="a-tls" type="checkbox" checked> TLS sertifikasını doğrula</label>
    <div class="row"><button id="save-api">Kaydet</button><button id="test-api" class="secondary">Servisi kontrol et</button></div>
    <h3>OpenAPI / Swagger</h3>
    <div class="row" style="margin-top:0"><input id="openapi-file" type="file" accept=".json,.yaml,.yml" style="max-width:360px"><button id="upload-openapi" class="secondary">Yükle ve kataloğa al</button></div>
    <div id="catalog"></div>
  </section>
  <section class="card setup-step" data-step="code">
    <h2>Servis kaynak kodu</h2>
    <p class="muted">Repo klasörlerini her satıra bir tane yazın. ContractFlow, endpoint'in handler → servis → repository zincirini, servisin kendi SQL'ini ve entity eşlemelerini koddan çıkarır; Oracle eşleştirmesi tahmin yerine bu kanıta dayanır. Kod yalnızca yerelde okunur. Spring MVC, JAX-RS, JPA/Hibernate, Spring Data, MyBatis ve JDBC kalıpları desteklenir.</p>
    <textarea id="code-paths" placeholder="C:\\repos\\servis-a&#10;C:\\repos\\ortak-kutuphane"></textarea>
    <div class="row"><button id="save-code-paths">Kaydet ve indeksle</button><span id="code-paths-state" class="muted"></span></div>
    <h3>Korelasyonlu SQL logu (opsiyonel)</h3>
    <p class="muted" style="font-size:13px">Servis SQL'i datasource-proxy veya p6spy ile logluyor ve isteğin <code>X-ContractFlow-Trace</code> başlığını MDC'ye koyuyorsa, log dosyasının yolunu verin; isteğe ait SQL'ler buradan eşlenir.</p>
    <div class="row" style="margin-top:0"><input id="sql-log" placeholder="C:/logs/servis-sql.log" style="max-width:520px"><button id="save-sql-log" class="secondary">Kaydet</button></div>
    <h3>OpenTelemetry (opsiyonel)</h3>
    <p class="muted" style="font-size:13px">Servis OpenTelemetry Java agent ile başlatılırsa JDBC span'leri ContractFlow'a gönderilebilir. ContractFlow her isteğe W3C <code>traceparent</code> başlığı ekler; aynı trace'e ait SQL'ler birebir eşlenir.</p>
    <pre>-javaagent:opentelemetry-javaagent.jar
OTEL_EXPORTER_OTLP_PROTOCOL=http/json
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:8000/v1/traces
OTEL_METRICS_EXPORTER=none
OTEL_LOGS_EXPORTER=none</pre>
  </section>
  <section class="card setup-step" data-step="documents">
    <h2>İş kuralı belgeleri</h2>
    <label class="check"><input id="documents-optional" type="checkbox"> Belge zorunlu değil — belge yoksa doğrulama beklemeden yapılır, rapor bu kapsam eksikliğini belirtir</label>
    <div class="row"><input id="doc-files" type="file" accept=".pdf,.md,.markdown" multiple style="max-width:360px"><button id="upload-docs" class="secondary">Yükle ve indeksle</button></div>
    <div id="documents" class="stack" style="margin-top:12px"></div>
    <h3>Model</h3>
    <div class="grid two">
      <div><label for="m-url">Ollama adresi</label><input id="m-url"></div>
      <div><label for="m-model">Karar modeli</label><input id="m-model"></div>
      <div><label for="m-embed">Embedding modeli</label><input id="m-embed"></div>
      <div><label for="m-temp">Temperature</label><input id="m-temp" type="number" step="0.05"></div>
      <div><label for="m-ctx">num_ctx</label><input id="m-ctx" type="number"></div>
      <div><label for="m-steps">Ajan başına adım</label><input id="m-steps" type="number"></div>
    </div>
    <div class="row"><button id="save-model">Kaydet</button><button id="probe-model" class="secondary">Yetenek testi (gerçek model)</button></div>
  </section>
  <section class="card setup-step" data-step="advanced">
    <h2>Kayıtlı eşleştirmeler (proje hafızası)</h2>
    <table><thead><tr><th>Operasyon</th><th>Kaynak</th><th>Tablo</th><th>Filtreler</th><th></th></tr></thead><tbody id="mappings"></tbody></table>
    <h3>Yanıt profili düzeltmesi</h3>
    <div class="grid two">
      <div><label for="r-operation">Operasyon</label><select id="r-operation"></select></div>
      <div><label for="r-items">Kayıt listesi yolu</label><input id="r-items" placeholder="result.content"></div>
      <div><label for="r-total">Toplam yolu</label><input id="r-total" placeholder="result.totalElements"></div>
      <div><label for="r-id">ID alanı</label><input id="r-id" value="id"></div>
      <div><label for="r-pagination">Sayfalama</label><select id="r-pagination"><option value="page_number">page/size</option><option value="offset_limit">offset/limit</option><option value="none">yok</option></select></div>
      <div><label for="r-page">Sayfa parametresi</label><input id="r-page" value="page"></div>
      <div><label for="r-size">Boyut parametresi</label><input id="r-size" value="size"></div>
      <div><label for="r-first">İlk sayfa</label><input id="r-first" type="number" value="0"></div>
      <div><label for="r-page-size">Sayfa boyutu</label><input id="r-page-size" type="number" value="100"></div>
    </div>
    <div class="row"><button id="save-override">Kaydet</button><button id="clear-override" class="secondary">Otomatiğe dön</button></div>
    <h3>İzinli şemadaki tablolar</h3>
    <button id="load-tables" class="secondary">Şemayı MCP ile listele</button>
    <div id="tables"></div>
  </section>`;

  function headerRow(row) {
    return `<tr><td><input class="h-name" value="${escapeHtml(row.name)}" autocomplete="off"></td><td><input class="h-value" value="${escapeHtml(row.value)}" placeholder="${row.stored ? "kayıtlı — değiştirmek için girin" : "değer"}" autocomplete="off"></td><td><button type="button" class="danger h-remove">×</button></td></tr>`;
  }

  function readHeaderRows() {
    return [...container.querySelectorAll("#header-rows tr")].map((row) => ({ name: row.querySelector(".h-name").value.trim(), value: row.querySelector(".h-value").value.trim() })).filter((row) => row.name);
  }

  function fill() {
    setValue("o-host", project.oracle.host); setValue("o-port", project.oracle.port); setValue("o-service", project.oracle.service_name);
    setValue("o-user", project.oracle.username); setValue("o-schemas", project.oracle.schema_allowlist.join(", "));
    container.querySelector("#o-capture").checked = project.oracle.capture_runtime_sql;
    setValue("o-service-users", (project.oracle.service_db_users || []).join(", "));
    setValue("sql-log", project.sql_log_path || "");
    container.querySelector("#o-password-state").textContent = project.oracle.password_set ? "(kayıtlı)" : "";
    container.querySelector("#oracle-status").innerHTML = project.readiness.oracle.message ? `<div class="callout ${project.readiness.oracle.state === "ready" ? "ok" : project.readiness.oracle.state === "failed" ? "fail" : ""}">${escapeHtml(project.readiness.oracle.message)}</div>` : "";
    setValue("a-base", project.api.base_url); setValue("a-health", project.api.health_path); setValue("a-auth", project.api.auth_type);
    setValue("a-user", project.api.username); setValue("a-key-name", project.api.api_key_name);
    container.querySelector("#a-tls").checked = project.api.verify_tls;
    container.querySelector("#a-credential-state").textContent = project.api.credential_set ? "(kayıtlı)" : "";
    container.querySelector("#header-rows").innerHTML = project.api.header_names.map((name) => headerRow({ name, value: "", stored: true })).join("");
    setValue("code-paths", (project.code_paths || []).join("\n"));
    container.querySelector("#documents-optional").checked = project.documents_optional;
    setValue("m-url", project.model.base_url); setValue("m-model", project.model.model); setValue("m-embed", project.model.embedding_model);
    setValue("m-temp", project.model.temperature); setValue("m-ctx", project.model.num_ctx); setValue("m-steps", project.model.max_steps_per_agent);
  }

  async function loadCatalog() {
    const catalog = await api(`/api/projects/${projectId}/operations`);
    const target = container.querySelector("#catalog");
    if (!catalog.loaded) { target.innerHTML = `<p class="muted">Henüz sözleşme yüklenmedi.</p>`; return catalog; }
    const executable = catalog.operations.filter((operation) => operation.executable);
    target.innerHTML = `<div class="callout">${escapeHtml(catalog.title)} · ${escapeHtml(catalog.version)} · ${catalog.operations.length} operasyon, ${executable.length} okuma operasyonu</div>
      <div style="max-height:360px;overflow:auto"><table><thead><tr><th>Metod</th><th>Yol</th><th>Yanıt profili</th></tr></thead><tbody>
      ${executable.slice(0, 200).map((operation) => `<tr><td><span class="badge ready">${operation.method}</span></td><td><code>${escapeHtml(operation.path)}</code><br><small class="muted">${escapeHtml(operation.summary || operation.operation_id)}</small></td><td class="source">${operation.response_profile ? `${escapeHtml(operation.response_profile.items_path || "(kök)")} · ${escapeHtml(operation.response_profile.total_path || "-")} · ${escapeHtml(operation.response_profile.pagination)}` : ""}</td></tr>`).join("")}
      </tbody></table></div>`;
    container.querySelector("#r-operation").innerHTML = executable.map((operation) => `<option value="${escapeHtml(operation.operation_id)}">${escapeHtml(operation.path)}${project.response_overrides[operation.operation_id] ? " (ayarlı)" : ""}</option>`).join("");
    return catalog;
  }

  async function loadDocuments() {
    const documents = await api(`/api/projects/${projectId}/documents`);
    const busy = documents.some((item) => ["queued", "parsing", "embedding"].includes(item.status));
    const stage = { queued: 1, parsing: 2, embedding: 3, ready: 4, failed: 4, needs_ocr: 4, superseded: 4 };
    container.querySelector("#documents").innerHTML = documents.map((item) => `<div class="card" style="padding:14px"><div style="display:flex;justify-content:space-between;gap:10px"><b>${escapeHtml(item.filename)}</b>${badge(item.status)}</div>
      <div class="progress ${["parsing", "embedding"].includes(item.status) ? "indeterminate" : ""}" style="margin-top:10px"><span style="width:${(stage[item.status] || 0) * 25}%"></span></div>
      <small class="muted">${item.chunk_count} parça · ${escapeHtml(item.status_message)}</small></div>`).join("") || `<p class="muted">Belge yok.</p>`;
    if (busy) {
      const timer = setTimeout(async () => { await loadDocuments(); accept(await api(`/api/projects/${projectId}`)); }, 1500);
      app.cleanup.push(() => clearTimeout(timer));
    }
  }

  async function loadMappings() {
    const mappings = await api(`/api/projects/${projectId}/mappings`);
    container.querySelector("#mappings").innerHTML = mappings.map((row) => {
      const mapping = JSON.parse(row.payload);
      const filters = mapping.source === "code" ? `<code>${escapeHtml((mapping.code_references || []).join(", "))}</code>` : mapping.scope_filters.map((item) => `<code>${escapeHtml(item.column)} ${escapeHtml(item.operator)} ${escapeHtml(JSON.stringify(item.values))}</code>`).join("<br>") || "-";
      return `<tr><td>${escapeHtml(row.operation_id)}</td><td>${mapping.source === "code" ? "Kod" : "Ajan"}</td><td>${escapeHtml(mapping.base_table)}</td><td>${filters}</td><td><button class="danger" data-delete="${escapeHtml(row.operation_id)}">Sil</button></td></tr>`;
    }).join("") || `<tr><td colspan="5" class="muted">Kayıtlı eşleştirme yok</td></tr>`;
  }

  const oraclePayload = () => ({ host: value("o-host"), port: Number(value("o-port") || 1521), service_name: value("o-service"), username: value("o-user"), password: value("o-password"), schema_allowlist: value("o-schemas").split(",").map((item) => item.trim()).filter(Boolean), capture_runtime_sql: container.querySelector("#o-capture").checked, service_db_users: value("o-service-users").split(",").map((item) => item.trim()).filter(Boolean) });
  const apiPayload = () => ({ base_url: value("a-base"), health_path: value("a-health"), auth_type: value("a-auth"), username: value("a-user"), api_key_name: value("a-key-name"), credential: value("a-credential"), verify_tls: container.querySelector("#a-tls").checked, headers: readHeaderRows() });
  const on = (id, handler) => container.querySelector(`#${id}`).addEventListener("click", (event) => withBusy(event.target, () => handler(event)));

  on("save-oracle", async () => { accept(await api(`/api/projects/${projectId}/oracle`, { method: "PUT", json: oraclePayload() })); setValue("o-password", ""); toast("Oracle ayarları kaydedildi"); });
  on("test-oracle", async () => { accept(await api(`/api/projects/${projectId}/oracle`, { method: "PUT", json: oraclePayload() })); setValue("o-password", ""); accept(await api(`/api/projects/${projectId}/oracle/test`, { method: "POST" })); });
  on("save-api", async () => { accept(await api(`/api/projects/${projectId}/api-profile`, { method: "PUT", json: apiPayload() })); setValue("a-credential", ""); toast("API ayarları kaydedildi"); });
  on("test-api", async () => { accept(await api(`/api/projects/${projectId}/api-profile`, { method: "PUT", json: apiPayload() })); setValue("a-credential", ""); accept(await api(`/api/projects/${projectId}/api-profile/test`, { method: "POST" })); toast(project.readiness.api.message, project.readiness.api.state !== "ready"); });
  on("add-header", async () => { container.querySelector("#header-rows").insertAdjacentHTML("beforeend", headerRow({ name: "", value: "" })); });
  on("required-headers", async () => {
    const catalog = await api(`/api/projects/${projectId}/operations`);
    const present = new Set(readHeaderRows().map((row) => row.name.toLowerCase()));
    const missing = (catalog.required_headers || []).filter((name) => !present.has(name.toLowerCase()));
    missing.forEach((name) => container.querySelector("#header-rows").insertAdjacentHTML("beforeend", headerRow({ name, value: "" })));
    toast(missing.length ? `${missing.length} header eklendi; değerlerini girin` : "Ortak zorunlu header'ların hepsi listede");
  });
  container.querySelector("#header-rows").addEventListener("click", (event) => { if (event.target.classList.contains("h-remove")) event.target.closest("tr").remove(); });
  on("upload-openapi", async () => {
    const file = container.querySelector("#openapi-file").files[0];
    if (!file) throw new Error("Önce bir dosya seçin");
    const form = new FormData(); form.append("file", file);
    await api(`/api/projects/${projectId}/openapi`, { method: "POST", body: form });
    accept(await api(`/api/projects/${projectId}`)); await loadCatalog(); toast("Sözleşme kataloğa alındı");
  });
  on("save-code-paths", async () => {
    const paths = value("code-paths").split("\n").map((path) => path.trim()).filter(Boolean);
    const result = await api(`/api/projects/${projectId}/code-paths`, { method: "PUT", json: { paths } });
    container.querySelector("#code-paths-state").textContent = paths.length ? `${result.files} kaynak dosya indekslendi` : "Kod analizi kapalı";
    accept(await api(`/api/projects/${projectId}`));
  });
  on("save-sql-log", async () => { accept(await api(`/api/projects/${projectId}/sql-log`, { method: "PUT", json: { path: value("sql-log") } })); toast("SQL log yolu kaydedildi"); });
  container.querySelector("#documents-optional").addEventListener("change", (event) => withBusy(event.target, async () => {
    accept(await api(`/api/projects/${projectId}/documents-optional`, { method: "PUT", json: { optional: event.target.checked } }));
  }));
  on("upload-docs", async () => {
    const files = container.querySelector("#doc-files").files;
    if (!files.length) throw new Error("Önce dosya seçin");
    const form = new FormData(); [...files].forEach((file) => form.append("files", file));
    await api(`/api/projects/${projectId}/documents`, { method: "POST", body: form });
    await loadDocuments();
  });
  on("save-model", async () => {
    const profile = { ...project.model, base_url: value("m-url"), model: value("m-model"), embedding_model: value("m-embed"), temperature: Number(value("m-temp")), num_ctx: Number(value("m-ctx")), max_steps_per_agent: Number(value("m-steps")) };
    accept(await api(`/api/projects/${projectId}/model`, { method: "PUT", json: profile })); toast("Model ayarları kaydedildi");
  });
  on("probe-model", async () => { accept(await api(`/api/projects/${projectId}/model/probe`, { method: "POST" })); toast(project.readiness.ollama.message, project.readiness.ollama.state !== "ready"); });
  container.querySelector("#mappings").addEventListener("click", async (event) => {
    if (!event.target.dataset.delete) return;
    await api(`/api/projects/${projectId}/mappings/${encodeURIComponent(event.target.dataset.delete)}`, { method: "DELETE" });
    loadMappings();
  });
  const overridePayload = () => ({ items_path: value("r-items"), total_path: value("r-total"), id_path: value("r-id"), pagination: value("r-pagination"), page_param: value("r-page"), size_param: value("r-size"), first_page: Number(value("r-first")), page_size: Number(value("r-page-size")) });
  on("save-override", async () => { accept(await api(`/api/projects/${projectId}/response-profiles/${encodeURIComponent(value("r-operation"))}`, { method: "PUT", json: overridePayload() })); await loadCatalog(); toast("Yanıt profili kaydedildi"); });
  on("clear-override", async () => { accept(await api(`/api/projects/${projectId}/response-profiles/${encodeURIComponent(value("r-operation"))}`, { method: "DELETE" })); await loadCatalog(); toast("Otomatik algılamaya dönüldü"); });
  on("load-tables", async () => {
    const tables = await api(`/api/projects/${projectId}/oracle/tables`);
    container.querySelector("#tables").innerHTML = `<div style="max-height:320px;overflow:auto;margin-top:10px"><table><thead><tr><th>Tablo</th><th>Tip</th><th>Açıklama</th></tr></thead><tbody>${tables.map((table) => `<tr><td><code>${escapeHtml(table.table)}</code></td><td>${escapeHtml(table.type)}</td><td class="muted">${escapeHtml(table.comment)}</td></tr>`).join("")}</tbody></table></div>`;
  });

  fill();
  showStep();
  await Promise.all([loadCatalog(), loadDocuments(), loadMappings()]);
}

function stageFor(node) {
  return STAGES.find((stage) => stage.node === node);
}

function createRunTracker(estimates = {}) {
  return { stages: Object.fromEntries(STAGES.map((stage) => [stage.node, { state: "idle", tools: [], detail: "", live: "", tokens: 0, startedAt: null, finishedAt: null }])), current: null, estimates };
}

function remainingEstimate(tracker) {
  const now = Date.now();
  let seconds = 0;
  for (const definition of STAGES) {
    const stage = tracker.stages[definition.node];
    const estimate = tracker.estimates[definition.node];
    if (!estimate || ["done", "failed"].includes(stage.state)) continue;
    seconds += stage.state === "running" && stage.startedAt ? Math.max(0, estimate - (now - stage.startedAt) / 1000) : estimate;
  }
  return seconds;
}

function trackEvent(tracker, event) {
  const at = event.at ? new Date(event.at).getTime() : Date.now();
  if (event.type === "node_started" && tracker.stages[event.node]) {
    Object.entries(tracker.stages).forEach(([node, stage]) => { if (node !== event.node && stage.state === "waiting") stage.state = "done"; });
    tracker.current = event.node;
    Object.assign(tracker.stages[event.node], { state: "running", startedAt: tracker.stages[event.node].startedAt || at, finishedAt: null });
    return;
  }
  if (event.type === "node_finished" && tracker.stages[event.node]) {
    const stage = tracker.stages[event.node];
    stage.finishedAt = at;
    stage.state = event.status === "ERROR" ? "failed" : WAITING_STATUSES.has(event.status) ? "waiting" : "done";
    return;
  }
  const stage = tracker.stages[tracker.current];
  if (!stage) return;
  if (event.type === "tool_call") stage.tools.push({ name: event.tool, ok: true });
  if (event.type === "tool_result" && !event.ok && stage.tools.length) stage.tools[stage.tools.length - 1].ok = false;
  if (event.type === "model_progress") { stage.tokens = Math.max(stage.tokens, event.tokens); if (event.reason) stage.live = event.reason; }
  if (event.type === "model_started") stage.live = "";
  if (event.type === "model_finished") stage.tokens = Math.max(stage.tokens, event.output_tokens || 0);
  if (["agent_final", "deterministic", "memory", "code_sql", "runtime_sql", "independence", "gate_rejected", "comparison", "off_topic"].includes(event.type)) { stage.detail = event.reason || event.message || event.interpretation || ""; stage.live = ""; }
  if (event.type === "agent_clarification") stage.detail = event.question;
}

function renderPipeline(tracker) {
  const now = Date.now();
  return STAGES.map((definition) => {
    const stage = tracker.stages[definition.node];
    const elapsed = stage.startedAt ? ((stage.finishedAt || now) - stage.startedAt) / 1000 : null;
    const tools = stage.tools.slice(-4).map((tool) => `<span class="chip ${tool.ok ? "" : "fail"}">${escapeHtml(tool.name.replace("oracle_mcp.", "mcp."))}</span>`).join("");
    const estimate = tracker.estimates[definition.node];
    const meta = [elapsed != null ? `⏱ ${duration(elapsed)}` : "", stage.state !== "done" && estimate ? `≈ ${duration(estimate)}` : "", stage.tokens ? `✍️ ${stage.tokens} token` : "", stage.tools.length ? `🔧 ${stage.tools.length}` : ""].filter(Boolean).join(" ");
    const icon = stage.state === "done" ? "✓" : stage.state === "failed" ? "!" : definition.icon;
    const text = stage.state === "running" && stage.live ? `<span class="typing">${escapeHtml(stage.live)}</span>` : escapeHtml(stage.detail || definition.hint);
    return `<div class="stage ${stage.state}"><header><span class="icon">${icon}</span>${definition.title}</header>
      <div class="meta">${meta || escapeHtml(stage.state === "idle" ? "bekliyor" : "")}</div>
      <div class="detail">${text}</div>
      <div class="chips">${tools}</div></div>`;
  }).join("");
}

function describeEvent(event) {
  const who = event.agent || event.node || "";
  switch (event.type) {
    case "tool_call": return [who, `🔧 <code>${escapeHtml(event.tool)}</code> <span class="muted">${escapeHtml(JSON.stringify(event.arguments || {})).slice(0, 220)}</span>${event.reason ? `<br><span class="muted">${escapeHtml(event.reason)}</span>` : ""}`];
    case "tool_result": return [who, `<span class="${event.ok ? "ok-text" : "fail-text"}">${event.ok ? "✓" : "✗"}</span> <code>${escapeHtml(event.tool)}</code> ${escapeHtml(event.summary || "")}`];
    case "agent_final": return [who, `✅ ${escapeHtml(event.reason || "")}`];
    case "agent_clarification": return [who, `❓ ${escapeHtml(event.question)}`];
    case "model_output_invalid": return [who, `<span class="fail-text">Model çıktısı doğrulanamadı (deneme ${event.attempt})</span>: ${escapeHtml(event.error)}`];
    case "gate_rejected": return [who, `<span class="fail-text">🛡️ Plan kapısı reddetti</span>: ${escapeHtml(event.message)}`];
    case "memory": return [who, `🧠 ${escapeHtml(event.message)}`];
    case "replay": return [who, `↺ ${escapeHtml(event.message)}`];
    case "deterministic": return [who, `⚡ ${escapeHtml(event.message)}`];
    case "code_sql": return [who, `🧩 ${escapeHtml(event.message)}`];
    case "runtime_sql": return [who, `🎯 ${escapeHtml(event.message)}`];
    case "independence": return [who, `🧱 ${escapeHtml(event.message)}`];
    case "code_warning": return ["kod", `<span class="muted">⚠ ${escapeHtml(event.message)}</span>`];
    case "off_topic": return [who, `ℹ️ ${escapeHtml(event.message)}`];
    case "options_rejected": return [who, `<span class="fail-text">⚠ ${escapeHtml(event.message)}</span>`];
    case "comparison": return ["karşılaştırma", `${badge(event.status)} ${escapeHtml(event.interpretation)}`];
    case "mcp_connected": return ["mcp", `oracle_mcp stdio bağlandı · ${(event.tools || []).length} araç`];
    case "mcp_error": return ["mcp", `<span class="fail-text">${escapeHtml(event.message)}</span>`];
    case "run_waiting": return ["akış", `${badge(event.status)} ${escapeHtml(event.message)}`];
    case "run_finished": return ["akış", `Tamamlandı ${badge(event.status)}`];
    case "run_error": return ["akış", `<span class="fail-text">${escapeHtml(event.message)}</span>`];
    default: return null;
  }
}

function renderRules(target, rules) {
  if (!rules || !rules.rules) return;
  const items = rules.rules.map((rule) => {
    const code = rule.sources.some((source) => source.document_id === "code");
    return `<li class="${code ? "code" : ""}">${escapeHtml(rule.text)}${rule.exceptions.length ? `<br><small>İstisna: ${escapeHtml(rule.exceptions.join("; "))}</small>` : ""}<div class="source">${code ? "💻" : "📄"} ${rule.sources.map((source) => escapeHtml(sourceLabel(source))).join(" · ")}</div></li>`;
  }).join("");
  target.innerHTML = `<ul class="rule-list">${items || "<li class='muted'>Kaynaklı kural yok</li>"}</ul>${rules.unresolved.length ? `<div class="callout warn">Çözülemeyenler: ${rules.unresolved.map(escapeHtml).join("; ")}</div>` : ""}`;
}

function sqlCompare(specSql, specBinds, implementation) {
  const impl = implementation && implementation.sql
    ? `<pre>${escapeHtml(implementation.sql)}</pre><div class="source">💻 ${escapeHtml(implementation.reference || "")} · bind <code>${escapeHtml(JSON.stringify(implementation.binds || {}))}</code></div>`
    : `<p class="muted">Kodda henüz bulunmadı. Yürütme sırasında V$SQL, SQL logu veya OpenTelemetry ile yakalanmaya çalışılacak.</p>`;
  return `<div class="grid two"><div><h3>(A) Bağımsız iş kuralı sorgusu</h3><pre>${escapeHtml(specSql)}</pre><div class="source">bind <code>${escapeHtml(JSON.stringify(specBinds || {}))}</code></div></div><div><h3>(B) Servisin kendi sorgusu</h3>${impl}</div></div>`;
}

function evidenceChain(chain) {
  if (!chain || !chain.length) return "";
  return `<div class="chain">${chain.map((item, index) => `<span class="link">${index ? "→ " : ""}<b>${escapeHtml((item.symbol || item).split(" ").pop())}</b><small>${escapeHtml(item.file ? `${item.file.split("/").pop()}:${item.line}` : String(item).split(" ")[0].split("/").pop())}</small></span>`).join("")}</div>`;
}

function mappingPanel(waiting) {
  const details = waiting.details || {};
  const mapping = details.mapping;
  const code = mapping.source === "code"
    ? `<div class="callout ok"><b>💻 Servisin kendi sorgusu kullanılıyor</b> — <code>${mapping.code_references.map(escapeHtml).join(", ")}</code>${mapping.notes.length ? `<ul>${mapping.notes.map((note) => `<li>${escapeHtml(note)}</li>`).join("")}</ul>` : ""}</div>`
    : "";
  const filters = mapping.scope_filters.map((item) => `<code>${escapeHtml(item.column)} ${escapeHtml(item.operator)} ${escapeHtml(JSON.stringify(item.values))}</code>${item.or_group ? ` <small>(OR ${item.or_group})</small>` : ""}<div class="source">${escapeHtml(item.rule_text)} — ${escapeHtml(item.verified_by)}</div>`).join("") || "<span class='muted'>yok</span>";
  const unaddressed = mapping.unaddressed_rules && mapping.unaddressed_rules.length ? `<div class="callout warn"><b>SQL'e yansıtılmayan kurallar</b><ul>${mapping.unaddressed_rules.map((rule) => `<li>${escapeHtml(rule)}</li>`).join("")}</ul></div>` : "";
  return `${code}<table><tr><th>Operasyon</th><td>${escapeHtml(mapping.operation_id)}</td></tr><tr><th>Tablo</th><td>${escapeHtml(mapping.base_table)}${mapping.primary_key.length ? ` (kimlik: ${escapeHtml(mapping.primary_key.join(", "))})` : ""}</td></tr>
    ${mapping.source === "code" ? "" : `<tr><th>Parametre → kolon</th><td>${Object.entries(mapping.parameter_columns).map(([parameter, column]) => `${escapeHtml(parameter)} → ${escapeHtml(column)}`).join("<br>")}</td></tr><tr><th>Kapsam filtreleri</th><td>${filters}</td></tr>`}</table>
    ${unaddressed}${sqlCompare(details.sql, details.binds, details.implementation)}
    <label for="answer">Reddederseniz not (opsiyonel)</label><input id="answer" placeholder="ör. silinmiş kayıtlar hariç tutulmalı">
    <div class="row"><button data-action="approve">Onayla ve kaydet</button><button class="secondary" data-action="reject">Reddet, yeniden öner</button></div>`;
}

function waitingPanel(record, projectId) {
  const waiting = record.payload && record.payload.waiting;
  if (!waiting || !WAITING_STATUSES.has(record.status)) return "";
  let body = `<h2>${badge(record.status)} ${escapeHtml(waiting.message)}</h2>${waiting.question ? `<p><b>${escapeHtml(waiting.question)}</b></p>` : ""}`;
  if (waiting.details && waiting.details.commands) body += `<pre>${waiting.details.commands.map(escapeHtml).join("\n")}</pre>`;
  if (waiting.kind === "mapping") body += mappingPanel(waiting);
  else if (waiting.kind === "clarification") body += `${waiting.options.length ? `<div class="examples">${waiting.options.map((option) => `<button class="secondary" data-option="${escapeHtml(option)}">${escapeHtml(option)}</button>`).join("")}</div>` : ""}<label for="answer">Yanıtınız</label><input id="answer"><div class="row"><button data-action="answer">Yanıtla ve devam et</button></div>`;
  else if (waiting.kind === "documents") body += `<div class="row"><a class="button secondary" href="#/projects/${projectId}/setup">Belge yükle</a><button data-action="retry">Tekrar kontrol et</button><button class="secondary" data-action="continue_without_documents">Belgesiz devam et</button></div>`;
  else body += `<div class="row"><button data-action="retry">Tekrar kontrol et</button><a class="button secondary" href="#/projects/${projectId}/setup">Kurulum</a></div>`;
  return `<section class="card waiting-card">${body}</section>`;
}

function resultPanel(report, runId) {
  const byName = Object.fromEntries(report.assertions.map((assertion) => [assertion.name, assertion]));
  const apiValue = byName.api_vs_database ? byName.api_vs_database.left_value : null;
  const dbValue = byName.api_vs_database ? byName.api_vs_database.right_value : null;
  const expected = report.intent ? report.intent.expected_value : null;
  const implValue = byName.api_vs_implementation ? byName.api_vs_implementation.right_value : null;
  const source = report.implementation_source && report.implementation_source.source;
  const sourceLabel = { otel: "OpenTelemetry", log: "SQL logu", vsql: "V$SQL", code: "kaynak kod" }[source] || "";
  return `<div class="hero ${report.status}"><div><div class="big">${escapeHtml(STATUS_TEXT[report.status] || report.status)}</div><p>${escapeHtml(report.summary)}</p></div></div>
    <div class="grid four" style="margin-top:12px">${[["Beklenti", expected, ""], ["API (C)", apiValue, "servis çıktısı"], ["Servis sorgusu (B)", implValue, sourceLabel], ["İş kuralı sorgusu (A)", dbValue, "bağımsız"]].map(([caption, value, hint]) => `<div class="card metric"><div class="value">${escapeHtml(value ?? "–")}</div><div class="caption">${caption}</div>${hint ? `<small class="muted">${escapeHtml(hint)}</small>` : ""}</div>`).join("")}</div>
    <div class="row"><a class="button" href="#/runs/${runId}">Ayrıntılı rapor</a><a class="button secondary" href="/api/runs/${runId}/report.md">Markdown</a></div>`;
}

async function exampleRequests(projectId) {
  return api(`/api/projects/${projectId}/examples`).catch(() => []);
}

async function renderVerify(container, projectId, runId) {
  const project = await api(`/api/projects/${projectId}`);
  const estimates = await api(`/api/projects/${projectId}/stage-stats`).catch(() => ({}));
  const tracker = createRunTracker(estimates);
  let lastEventId = 0;
  let source = null;
  container.innerHTML = `
    <div class="page-head"><div><h1>Doğrulama · ${escapeHtml(project.name)}</h1><p>Beklentinizi doğal dille yazın; ajanlar kuralları, kodu, API'yi ve Oracle'ı inceler, sonucu kanıtlarıyla raporlar.</p></div><div id="run-badge"></div></div>
    <section class="card">
      <label for="request" style="margin-top:0">Talep</label>
      <textarea id="request" placeholder="Doğrulamak istediğiniz servisi, filtreyi ve beklentinizi yazın"></textarea>
      <div class="examples" id="examples"></div>
      <div class="row"><button id="start">▶ Doğrulamayı başlat</button><button id="cancel" class="danger hidden">İptal et</button></div>
      <div id="overall" style="margin-top:14px"></div>
    </section>
    <section style="margin-top:16px"><div class="pipeline" id="pipeline">${renderPipeline(tracker)}</div></section>
    <section class="card hidden" id="chain-card" style="margin-top:16px"><h2>Kod kanıt zinciri</h2><div id="chain"></div></section>
    <div id="waiting" style="margin-top:16px"></div>
    <div class="split" style="margin-top:16px">
      <section class="card"><h2>Canlı akış</h2><ul class="feed" id="feed"><li class="empty" style="display:block">Henüz çalışma yok</li></ul></section>
      <div class="stack"><section class="card"><h2>Kurallar ve kod kanıtları</h2><div id="rules" class="muted">Henüz yok</div></section><section id="result"></section></div>
    </div>`;
  const feed = container.querySelector("#feed");
  exampleRequests(projectId).then((examples) => {
    container.querySelector("#examples").innerHTML = examples.length ? `<span class="muted" style="font-size:12px;align-self:center">Son talepler:</span>` + examples.map((example) => `<button class="secondary" type="button">${escapeHtml(example)}</button>`).join("") : "";
    container.querySelectorAll("#examples button").forEach((button) => button.addEventListener("click", () => { container.querySelector("#request").value = button.textContent; }));
  });

  function renderOverall(status) {
    const done = Object.values(tracker.stages).filter((stage) => stage.state === "done").length;
    const running = status === "RUNNING";
    const remaining = running ? remainingEstimate(tracker) : 0;
    container.querySelector("#overall").innerHTML = runId ? `<div class="progress-label"><span>${running ? `Ajanlar çalışıyor…${remaining ? ` · tahmini kalan ${duration(remaining)}` : ""}` : escapeHtml(STATUS_TEXT[status] || status)}</span><span>${done}/${STAGES.length} aşama</span></div><div class="progress ${running && done === 0 ? "indeterminate" : ""}"><span style="width:${Math.round((done / STAGES.length) * 100)}%"></span></div>` : "";
  }

  async function refreshRecord() {
    const record = await api(`/api/runs/${runId}`);
    container.querySelector("#run-badge").innerHTML = `${badge(record.status)} <code class="muted">${escapeHtml(runId)}</code>`;
    const state = (record.payload && record.payload.state) || {};
    if (state.rules) renderRules(container.querySelector("#rules"), state.rules);
    const chain = (state.code_evidence && state.code_evidence.chain) || (record.payload.report && record.payload.report.code_chain) || [];
    container.querySelector("#chain-card").classList.toggle("hidden", !chain.length);
    container.querySelector("#chain").innerHTML = evidenceChain(chain);
    container.querySelector("#waiting").innerHTML = waitingPanel(record, projectId);
    notifyWaiting(record);
    const report = record.payload && record.payload.report;
    if (report) {
      renderRules(container.querySelector("#rules"), report.rules);
      container.querySelector("#result").innerHTML = `<div class="card">${resultPanel(report, runId)}</div>`;
    }
    const finished = TERMINAL.has(record.status) && !record.active;
    container.querySelector("#cancel").classList.toggle("hidden", finished);
    container.querySelector("#start").disabled = !finished && !WAITING_STATUSES.has(record.status);
    renderOverall(record.status);
    return record;
  }

  function addFeed(event) {
    const described = describeEvent(event);
    if (!described) return;
    const empty = feed.querySelector(".empty");
    if (empty) empty.remove();
    feed.insertAdjacentHTML("beforeend", `<li><div class="who">${escapeHtml(described[0])}<small>${time(event.at)}</small></div><div>${described[1]}</div></li>`);
    feed.scrollTop = feed.scrollHeight;
  }

  async function loadSnapshot() {
    const events = await api(`/api/runs/${runId}/events?after=0`);
    events.forEach((event) => { lastEventId = event.id; trackEvent(tracker, event); if (event.type !== "model_progress") addFeed(event); });
    container.querySelector("#pipeline").innerHTML = renderPipeline(tracker);
    await refreshRecord();
  }

  function connect() {
    if (SNAPSHOT) return loadSnapshot();
    if (source) source.close();
    source = new EventSource(`/api/runs/${runId}/stream?after=${lastEventId}`);
    source.addEventListener("run_event", (message) => {
      const event = JSON.parse(message.data);
      lastEventId = event.id;
      trackEvent(tracker, event);
      if (event.type !== "model_progress") addFeed(event);
      container.querySelector("#pipeline").innerHTML = renderPipeline(tracker);
    });
    source.addEventListener("run_status", async () => {
      const record = await refreshRecord();
      if (TERMINAL.has(record.status) && !record.active) source.close();
    });
    source.onerror = () => { source.close(); setTimeout(() => { if (runId) connect(); }, 2000); };
  }

  const ticker = setInterval(() => { if (tracker.current) container.querySelector("#pipeline").innerHTML = renderPipeline(tracker); }, 1000);
  app.cleanup.push(() => { clearInterval(ticker); if (source) source.close(); });

  container.querySelector("#waiting").addEventListener("click", (event) => {
    const answer = container.querySelector("#answer");
    if (event.target.dataset.option) { answer.value = event.target.dataset.option; return; }
    const action = event.target.dataset.action;
    if (!action) return;
    const text = answer ? answer.value : "";
    if (action === "answer" && !text.trim()) { toast("Bir yanıt yazın", true); return; }
    withBusy(event.target, async () => {
      await api(`/api/runs/${runId}/resume`, { method: "POST", json: { action, text } });
      container.querySelector("#waiting").innerHTML = "";
      connect();
    });
  });

  container.querySelector("#start").addEventListener("click", (event) => withBusy(event.target, async () => {
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
    const response = await api(`/api/projects/${projectId}/runs`, { method: "POST", json: { request: container.querySelector("#request").value } });
    history.replaceState(null, "", `#/projects/${projectId}/verify/${response.run_id}`);
    runId = response.run_id;
    lastEventId = 0;
    Object.assign(tracker, createRunTracker());
    feed.innerHTML = "";
    container.querySelector("#result").innerHTML = "";
    container.querySelector("#rules").innerHTML = "Henüz yok";
    await refreshRecord();
    connect();
  }));

  container.querySelector("#cancel").addEventListener("click", (event) => withBusy(event.target, async () => {
    await api(`/api/runs/${runId}/cancel`, { method: "POST" });
    connect();
  }));

  if (runId) {
    const record = await refreshRecord();
    container.querySelector("#request").value = record.user_request;
    connect();
  }
}

let lastNotified = "";
function notifyWaiting(record) {
  const waiting = WAITING_STATUSES.has(record.status);
  document.title = waiting ? "(!) Onay bekleniyor · ContractFlow" : "ContractFlow";
  const key = `${record.id}:${record.status}:${record.updated_at}`;
  if (!waiting || key === lastNotified || !("Notification" in window) || Notification.permission !== "granted") return;
  lastNotified = key;
  new Notification("ContractFlow", { body: (record.payload.waiting && record.payload.waiting.message) || "Onayınız bekleniyor", icon: "/static/logo.svg" });
}

async function renderHistory(container, projectId) {
  const project = await api(`/api/projects/${projectId}`);
  const runs = await api(`/api/projects/${projectId}/runs`);
  container.innerHTML = `<div class="page-head"><div><h1>Geçmiş · ${escapeHtml(project.name)}</h1><p>Önceki doğrulamalar; raporu açabilir veya aynı kontrolü tekrar çalıştırabilirsiniz.</p></div></div>
    <section>${runs.map((run) => {
      const finished = TERMINAL.has(run.status);
      const seconds = (new Date(run.updated_at) - new Date(run.created_at)) / 1000;
      return `<div class="history-item" data-href="${finished ? `#/runs/${run.id}` : `#/projects/${projectId}/verify/${run.id}`}">${badge(run.status)}<div><b>${escapeHtml(run.user_request)}</b><div class="muted" style="font-size:12.5px">${new Date(run.created_at).toLocaleString("tr-TR")} · ${duration(seconds)}</div></div><span class="muted">${finished ? "Rapor →" : "Devam et →"}</span></div>`;
    }).join("") || `<div class="card empty">Henüz çalışma yok</div>`}</section>`;
  container.querySelectorAll(".history-item").forEach((item) => item.addEventListener("click", () => { location.hash = item.dataset.href; }));
}

async function renderReport(container, runId) {
  const record = await api(`/api/runs/${runId}`);
  selectProject(record.project_id);
  const report = record.payload.report;
  if (!report) {
    container.innerHTML = `<div class="card">${badge(record.status)} Rapor henüz hazır değil. <a href="#/projects/${record.project_id}/verify/${runId}">Çalışmayı izle</a></div>`;
    return;
  }
  const api_ = report.api || {};
  const database = report.database || {};
  const section = (title, body) => `<section class="card"><h2>${title}</h2>${body}</section>`;
  const mapping = report.mapping;
  container.innerHTML = `<div class="page-head"><div><h1>Doğrulama raporu</h1><p>${escapeHtml(report.project_name)} · ${escapeHtml(report.environment)} · <code>${escapeHtml(report.run_id)}</code></p></div>
    <div class="row" style="margin-top:0"><a class="button secondary" href="/api/runs/${runId}/report.json">JSON</a><a class="button secondary" href="/api/runs/${runId}/report.md">Markdown</a><button id="rerun">Tekrar çalıştır</button></div></div>
    <div class="stack">
      <div class="card">${resultPanel(report, runId).replace(/<div class="row">[\s\S]*<\/div>$/, "")}<p class="muted" style="margin-bottom:0">Talep: ${escapeHtml(report.user_request)}</p></div>
      ${section("Bağımsız assertion'lar", `<table><thead><tr><th>Kontrol</th><th>Durum</th><th>Sol</th><th>Sağ</th><th>Açıklama</th></tr></thead><tbody>${report.assertions.map((assertion) => `<tr><td>${escapeHtml(assertion.title)}</td><td>${badge(assertion.status)}</td><td class="num">${escapeHtml(assertion.left_label)}: ${escapeHtml(assertion.left_value ?? "-")}</td><td class="num">${escapeHtml(assertion.right_label)}: ${escapeHtml(assertion.right_value ?? "-")}</td><td>${escapeHtml(assertion.message)}</td></tr>`).join("")}</tbody></table>`)}
      ${report.hypotheses.length ? section("Hipotezler (kesin kök neden değildir)", `<ul>${report.hypotheses.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`) : ""}
      ${section("Kurallar ve kod kanıtları", `<div id="report-rules"></div><p class="muted">Belge snapshot'ı: ${escapeHtml(report.document_snapshot.join(", ") || "yok")}</p>`)}
      ${report.code_chain && report.code_chain.length ? section("Kod kanıt zinciri", evidenceChain(report.code_chain)) : ""}
      ${section("Üç kaynaklı karşılaştırma: iş kuralı (A) ↔ servis sorgusu (B) ↔ API (C)", sqlCompare(database.sql || "", database.binds, report.implementation_source))}
      ${(report.implementation_source && report.implementation_source.raw_sql) ? section("Yakalanan ham servis SQL'i", `<pre>${escapeHtml(report.implementation_source.raw_sql)}</pre>${(report.implementation_source.notes || []).map((note) => `<div class="source">• ${escapeHtml(note)}</div>`).join("")}`) : ""}
      ${mapping ? section("Endpoint – veritabanı eşleştirmesi", `<table><tr><th>Kaynak</th><td>${mapping.source === "code" ? `💻 Kod: <code>${escapeHtml(mapping.code_references.join(", "))}</code>` : "Ajan önerisi"}</td></tr><tr><th>Operasyon</th><td>${escapeHtml(mapping.operation_id)}</td></tr><tr><th>Tablo</th><td>${escapeHtml(mapping.base_table)}</td></tr><tr><th>Onay</th><td>${mapping.confirmed ? "Onaylı (proje hafızası)" : "Onaysız"}</td></tr></table>`) : ""}
      ${report.api ? section("API gözlemi", `<pre>${escapeHtml(api_.curl)}</pre><p>HTTP ${escapeHtml(api_.status_code ?? "-")} · ${api_.pages_fetched} sayfa · ${api_.fetched_count} kayıt · ${escapeHtml(api_.unique_id_count ?? "-")} benzersiz ID · bildirilen toplam ${escapeHtml(api_.reported_total ?? "-")} · ${api_.elapsed_ms} ms</p>${api_.error_code ? `<div class="callout fail">${escapeHtml(api_.error_code)}: ${escapeHtml(api_.error_message)}</div>` : ""}`) : ""}
      ${report.database ? section("Oracle gözlemi (SELECT-only MCP)", `<pre>${escapeHtml(database.sql)}</pre><p>Bind: <code>${escapeHtml(JSON.stringify(database.binds))}</code> · ${database.row_count} satır · ${database.elapsed_ms} ms</p>${database.error_code ? `<div class="callout fail">${escapeHtml(database.error_code)}: ${escapeHtml(database.error_message)}</div>` : ""}`) : ""}
      ${report.coverage_notes.length ? section("Kapsam ve eksikler", `<ul>${report.coverage_notes.map((note) => `<li>${escapeHtml(note)}</li>`).join("")}</ul>`) : ""}
      ${section("Araç izi", `<ul class="feed">${report.tool_events.filter((event) => event.tool).map((event) => `<li><div class="who">${escapeHtml(event.agent)}</div><div><code>${escapeHtml(event.tool)}</code> <span class="muted">${escapeHtml(JSON.stringify(event.arguments)).slice(0, 200)}</span> ${escapeHtml(event.summary)}</div></li>`).join("")}</ul>`)}
      <p class="muted">Model: ${escapeHtml(report.model)} · ${escapeHtml(report.prompt_version)} · ${time(report.started_at)} → ${time(report.finished_at)}</p>
    </div>`;
  renderRules(container.querySelector("#report-rules"), report.rules);
  container.querySelector("#rerun").addEventListener("click", (event) => withBusy(event.target, async () => {
    const response = await api(`/api/runs/${runId}/rerun`, { method: "POST" });
    location.hash = `#/projects/${record.project_id}/verify/${response.run_id}`;
  }));
}

(async () => {
  await loadProjects();
  if (!location.hash) location.hash = app.projectId ? `#/projects/${app.projectId}/verify` : "#/projects";
  else route();
})();
