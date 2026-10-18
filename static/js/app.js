/* Görev Merkezi - arayüz etkileşimleri
   Sunucu uç noktaları: POST /tasks, /tasks/<id>/toggle, /tasks/<id>/delete
   Liste araması: GET /?q=...&fragment=1 (yalnızca liste parçası döner) */
(() => {
  "use strict";

  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const listEl = document.getElementById("task-list");
  const searchInput = document.getElementById("search-input");
  const searchBox = document.querySelector(".search-box");
  const addForm = document.getElementById("add-form");
  const toastStack = document.getElementById("toast-stack");

  const AYLAR = window.I18N_MONTHS || [];

  // Sunucunun base.html üzerinden enjekte ettiği çeviri metinleri
  const STR = window.I18N || {};
  const text = (key, fallback = "") => STR[key] || fallback;

  let searchTimer = null;
  let deleteTarget = null;
  const inFlight = new Set(); // aynı göreve çift istek göndermeyi engeller

  const currentQuery = () => (searchInput?.value || "").trim();

  /* ------------------------------------------------------------------ UI */

  const TOAST_META = {
    success: { icon: "check-lg", key: "toast.success" },
    danger: { icon: "x-lg", key: "toast.danger" },
    info: { icon: "info-lg", key: "toast.info" },
    warning: { icon: "exclamation-triangle", key: "toast.warning" },
  };

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  /** Kullanıcıya kısa geri bildirim bildirimi (toast) gösterir. */
  function notify(message, type = "success") {
    if (!toastStack || typeof bootstrap === "undefined") return;
    const meta = TOAST_META[type] || TOAST_META.info;
    const el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");
    el.innerHTML = `
      <div class="toast-header">
        <span class="toast-icon t-${type}"><i class="bi bi-${meta.icon}"></i></span>
        <strong class="ms-2 me-auto">${escapeHtml(text(meta.key))}</strong>
        <small>${escapeHtml(text("js.now"))}</small>
        <button type="button" class="btn-close ms-2 mb-1" data-bs-dismiss="toast" aria-label="Close"></button>
      </div>
      <div class="toast-body">${escapeHtml(message)}</div>`;
    toastStack.appendChild(el);
    el.addEventListener("hidden.bs.toast", () => el.remove());
    new bootstrap.Toast(el, { delay: 3200 }).show();
  }

  /** Sol paneldeki sayıları ve ilerleme çubuğunu günceller. */
  function updateStats(stats) {
    if (!stats) return;
    [["stat-total", stats.total], ["stat-active", stats.active], ["stat-done", stats.done]]
      .forEach(([id, value]) => {
        const el = document.getElementById(id);
        if (!el || el.textContent === String(value)) return;
        el.textContent = value;
        el.classList.remove("bump");
        void el.offsetWidth; // animasyonun yeniden tetiklenmesi için
        el.classList.add("bump");
      });
    const fill = document.getElementById("progress-fill");
    const label = document.getElementById("progress-label");
    if (fill) fill.style.width = `${stats.percent}%`;
    if (label) label.textContent = `${stats.percent}%`;
  }

  /** Liste başlığındaki sayaç ve notu günceller (ekleme/silme/togle sonrası). */
  function updateCounts(stats) {
    const badge = document.getElementById("result-count");
    if (badge && listEl) badge.textContent = listEl.querySelectorAll(".task-card").length;
    const note = listEl?.querySelector(".list-note");
    if (note && stats && !currentQuery()) {
      note.textContent = `${stats.active} bekleyen · ${stats.done} tamamlanan`;
    }
  }

  /** Listebaşındaki çöp kutusu sayacını günceller. */
  function updateTrashBadge(count) {
    const el = document.getElementById("trash-count");
    if (!el || count == null) return;
    el.hidden = !count;
    el.textContent = count;
  }

  function setLoading(on) {
    if (listEl) listEl.classList.toggle("is-loading", Boolean(on));
  }

  /** Sunucunun tarih biçimini arayüzdeki biçime çevirir. */
  function formatDate(value) {
    if (!value) return "";
    const match = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(value);
    if (!match) return value;
    const [, y, m, d, hh, mm] = match;
    return `${Number(d)} ${AYLAR[Number(m) - 1]} ${y} · ${hh}:${mm}`;
  }

  /* ------------------------------------------------------------ sunucu IO */

  /** POST isteği gönderir; başarısızsa alan hatalarıyla birlikte hata fırlatır. */
  async function post(url, payload) {
    const body = new FormData();
    Object.entries(payload || {}).forEach(([key, value]) => body.append(key, value));
    const res = await fetch(url, {
      method: "POST",
      body,
      headers: { "X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": csrfToken },
    });
    let data = {};
    try {
      data = await res.json();
    } catch {
      /* gövdesiz yanıtlar için boş kalır */
    }
    if (!res.ok) {
      const err = new Error(data.error || (data.errors && data.errors.title) || text("error.generic"));
      err.fields = data.errors || {};
      throw err;
    }
    return data;
  }

  /** Listeyi sunucudan yeniden çeker; highlightId verilirse o kart animasyon alır. */
  async function fetchList(highlightId) {
    const url = `/?q=${encodeURIComponent(currentQuery())}&fragment=1`;
    setLoading(true);
    try {
      const res = await fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } });
      if (!res.ok) throw new Error(text("js.list_failed"));
      listEl.innerHTML = await res.text();
      syncSearchUi();
      if (highlightId) {
        listEl.querySelector(`.task-card[data-id="${highlightId}"]`)?.classList.add("is-new");
      }
    } finally {
      setLoading(false);
    }
  }

  /** Aramayı uygular ve adres çubuğunu günceller. */
  async function runSearch() {
    try {
      await fetchList();
      const q = currentQuery();
      history.replaceState(null, "", q ? `/?q=${encodeURIComponent(q)}` : "/");
    } catch (err) {
      notify(err.message, "danger");
    }
  }

  /* ------------------------------------------------------- alan doğrulama */

  function clearFieldErrors(form) {
    form?.querySelectorAll(".is-invalid").forEach((el) => el.classList.remove("is-invalid"));
    form?.querySelectorAll("[data-error-for]").forEach((el) => (el.textContent = ""));
  }

  function showFieldErrors(form, fields) {
    Object.entries(fields).forEach(([name, message]) => {
      const input = form?.elements.namedItem(name);
      const feedback = form?.querySelector(`[data-error-for="${name}"]`);
      input?.classList.add("is-invalid");
      if (feedback) feedback.textContent = message;
    });
    const first = form?.querySelector(".is-invalid");
    if (first) first.focus();
  }

  /* ------------------------------------------------------------ görev işlem */

  /** Kart üzerinde tamamlanan/bekleyen durumunu görsel olarak yansıtır. */
  function applyToggle(card, task) {
    const done = Boolean(task.done);
    card.classList.toggle("is-done", done);

    const check = card.querySelector(".task-check");
    if (check) {
      check.setAttribute("aria-pressed", String(done));
      check.title = done ? "Tamamlandı işaretini kaldır" : "Tamamlandı olarak işaretle";
    }

    const toggleBtn = card.querySelector(".act-toggle");
    if (toggleBtn) {
      const icon = toggleBtn.querySelector("i");
      const label = toggleBtn.querySelector(".act-label");
      if (icon) icon.className = done ? "bi bi-arrow-counterclockwise" : "bi bi-check-circle";
      if (label) label.textContent = done ? "Geri al" : "Tamamla";
    }

    const doneMeta = card.querySelector(".done-meta");
    if (doneMeta) {
      doneMeta.hidden = !done;
      const text = doneMeta.querySelector(".done-meta-text");
      if (text && done) text.textContent = `Tamamlandı: ${formatDate(task.completed_at)}`;
    }

    card.classList.remove("just-toggled");
    void card.offsetWidth;
    card.classList.add("just-toggled");
  }

  async function handleToggle(id) {
    if (inFlight.has(id)) return;
    const card = listEl?.querySelector(`.task-card[data-id="${id}"]`);
    if (!card) return;

    inFlight.add(id);
    try {
      const data = await post(`/tasks/${id}/toggle`);
      applyToggle(card, data.task);
      updateStats(data.stats);
      updateCounts(data.stats);
      notify(
        data.task.done ? text("js.completed") : text("js.undone"),
        data.task.done ? "success" : "info"
      );
    } catch (err) {
      notify(err.message, "danger");
    } finally {
      inFlight.delete(id);
    }
  }

  /* ------------------------------------------------ çöp kutusu / silme akışı
     Varsayılan: görev çöp kutusuna taşınır (geri alınabilir).
     "Kalıcı olarak sil" onay kutusu işaretlenirse uyarı görünür ve görevin
     adının birebir yazıldığı anda kalıcı silme butonu açılır. */

  const purgeToggle = document.getElementById("purge-toggle");
  const purgeArea = document.getElementById("purge-area");
  const purgeName = document.getElementById("purge-name");
  const purgeBtn = document.getElementById("purge-confirm-btn");
  const softBtn = document.getElementById("soft-delete-btn");
  const softHint = document.getElementById("soft-hint");

  /** Silme penceresini hedef görevle açar ve kalıcı silme alanlarını sıfırlar. */
  function openDeleteModal(button) {
    const id = button.dataset.id;
    deleteTarget = {
      id,
      title: (button.dataset.title || "").trim(),
      card: listEl?.querySelector(`.task-card[data-id="${id}"]`) || null,
      row: document.querySelector(`.trash-row[data-id="${id}"]`),
    };
    const titleEl = document.getElementById("delete-task-title");
    if (titleEl) titleEl.textContent = deleteTarget.title;

    if (purgeToggle) purgeToggle.checked = false;
    if (purgeArea) purgeArea.hidden = true;
    if (purgeName) {
      purgeName.value = "";
      purgeName.classList.remove("is-valid", "is-invalid");
    }
    if (purgeBtn) { purgeBtn.hidden = true; purgeBtn.disabled = true; }
    if (softBtn) softBtn.hidden = false;
    if (softHint) softHint.hidden = false;

    bootstrap.Modal.getOrCreateInstance(document.getElementById("delete-modal")).show();
  }

  /** Yazılan ad hedefle birebir eşleşirse kalıcı silme butonunu açar. */
  function syncPurgeConfirm() {
    if (!purgeBtn || !purgeName || !deleteTarget) return;
    const value = purgeName.value.trim();
    const match = value === deleteTarget.title;
    purgeBtn.disabled = !match;
    purgeName.classList.toggle("is-invalid", value.length > 0 && !match);
    purgeName.classList.toggle("is-valid", match);
  }

  purgeToggle?.addEventListener("change", () => {
    const on = purgeToggle.checked;
    if (purgeArea) purgeArea.hidden = !on;
    if (softBtn) softBtn.hidden = on;
    if (softHint) softHint.hidden = on;
    if (purgeBtn) purgeBtn.hidden = !on;
    if (on) {
      syncPurgeConfirm();
      purgeName?.focus();
    } else {
      purgeName?.classList.remove("is-valid", "is-invalid");
      if (purgeBtn) purgeBtn.disabled = true;
    }
  });

  purgeName?.addEventListener("input", syncPurgeConfirm);

  /** Görevi çöp kutusuna taşır (varsayılan silme). */
  async function confirmDelete() {
    if (!deleteTarget || inFlight.has(deleteTarget.id)) return;
    const { id, card } = deleteTarget;
    deleteTarget = null;
    bootstrap.Modal.getOrCreateInstance(document.getElementById("delete-modal")).hide();

    inFlight.add(id);
    try {
      const data = await post(`/tasks/${id}/delete`);
      if (card) {
        card.classList.add("is-leaving");
        card.addEventListener("animationend", () => card.remove(), { once: true });
        setTimeout(() => card.remove(), 400); // animasyon desteklenmiyorsa da kaldır
      }
      updateStats(data.stats);
      updateCounts(data.stats);
      updateTrashBadge(data.trash_count);
      notify(text("js.trashed"), "warning");
    } catch (err) {
      notify(err.message, "danger");
    } finally {
      inFlight.delete(id);
    }
  }

  /** Görevi, adı birebir yazılarak kalıcı olarak siler (geri alınamaz). */
  async function confirmPurge() {
    if (!deleteTarget || inFlight.has(deleteTarget.id)) return;
    const { id, card, row } = deleteTarget;
    const written = purgeName ? purgeName.value.trim() : "";
    deleteTarget = null;
    bootstrap.Modal.getOrCreateInstance(document.getElementById("delete-modal")).hide();

    inFlight.add(id);
    try {
      const data = await post(`/tasks/${id}/purge`, { confirm_title: written });
      if (card) {
        card.classList.add("is-leaving");
        card.addEventListener("animationend", () => card.remove(), { once: true });
        setTimeout(() => card.remove(), 400);
      }
      if (row) row.classList.add("is-leaving");
      updateStats(data.stats);
      updateCounts(data.stats);
      updateTrashBadge(data.trash_count);
      notify(text("js.purged"), "danger");
      // Çöp kutusu sayfasındaysak sayaç ve boş durum tazelenir
      if (row) setTimeout(() => window.location.reload(), 700);
    } catch (err) {
      notify(err.message, "danger");
    } finally {
      inFlight.delete(id);
    }
  }

  softBtn?.addEventListener("click", confirmDelete);
  purgeBtn?.addEventListener("click", confirmPurge);

  /* ---------------------------------------------------------------- arama */

  function syncSearchUi() {
    searchBox?.classList.toggle("has-query", currentQuery().length > 0);
  }

  function resetSearch() {
    if (!searchInput) return;
    searchInput.value = "";
    syncSearchUi();
    clearTimeout(searchTimer);
    runSearch();
    searchInput.focus();
  }

  /* ------------------------------------------------------------- olaylar */

  // Görev listesindeki tüm tıklamalar tek dinleyici ile yakalanır (delegasyon)
  document.addEventListener("click", (event) => {
    const actionEl = event.target.closest("[data-action]");
    if (actionEl) {
      const action = actionEl.dataset.action;
      if (action === "toggle") {
        event.stopPropagation();
        handleToggle(actionEl.dataset.id);
        return;
      }
      if (action === "delete" || action === "trash") {
        event.stopPropagation();
        openDeleteModal(actionEl);
        return;
      }
      if (action === "edit") {
        event.stopPropagation();
        openEditModal(actionEl);
        return;
      }
      if (action === "reset-search") {
        resetSearch();
        return;
      }
      if (action === "focus-add") {
        document.getElementById("title")?.focus();
        return;
      }
    }

    // Takvim hücresinin boş alanına tıklayınca o gün seçilir (bağlantı gibi)
    const navEl = event.target.closest("[data-href]");
    if (navEl) {
      if (event.target.closest("a, button")) return; // çip/bağlantı kendi işini yapsın
      if (String(window.getSelection() || "").trim()) return;
      window.location.href = navEl.dataset.href;
      return;
    }

    // Kartın boş bir yerine tıklayarak da tamamlama
    const card = event.target.closest(".task-card");
    if (!card || card.classList.contains("is-leaving")) return;
    if (event.target.closest("a, button")) return;
    if (String(window.getSelection() || "").trim()) return; // metin seçiliyorsa dokunma
    handleToggle(card.dataset.id);
  });

  // Yeni görev ekleme
  addForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearFieldErrors(addForm);

    const title = addForm.elements.namedItem("title");
    if (!title.value.trim()) {
      showFieldErrors(addForm, { title: text("error.title_required") });
      title.focus();
      return;
    }

    const submitBtn = document.getElementById("add-submit");
    submitBtn.disabled = true;
    try {
      const payload = Object.fromEntries(new FormData(addForm).entries());
      const data = await post("/tasks", payload);
      addForm.reset();
      clearFieldErrors(addForm);
      await fetchList(data.task.id);
      updateStats(data.stats);
      notify(text("js.added"), "success");
      title.focus();
    } catch (err) {
      if (err.fields && Object.keys(err.fields).length) showFieldErrors(addForm, err.fields);
      notify(err.message, "danger");
    } finally {
      submitBtn.disabled = false;
    }
  });

  /* ------------------------------------------------------- görev düzenleme */

  const editForm = document.getElementById("edit-form");
  const editModalEl = document.getElementById("edit-modal");

  /** Düzenleme penceresini görev verileriyle doldurur ve açar.
      Kaynak önce düğmenin kendi verileridir (takvim çipleri), yoksa liste kartıdır. */
  function openEditModal(button) {
    if (!editForm || !editModalEl) return;
    const id = button.dataset.id;
    const card = listEl?.querySelector(`.task-card[data-id="${id}"]`);
    const src = button.dataset.title !== undefined ? button.dataset : card?.dataset;
    if (!src || id === undefined) return;
    editForm.action = `/tasks/${id}/edit`;
    editForm.elements.namedItem("title").value = src.title || "";
    editForm.elements.namedItem("description").value = src.desc || "";
    editForm.elements.namedItem("due_at").value = src.due || "";
    editForm.elements.namedItem("priority").value = src.priority || "2";
    clearFieldErrors(editForm);
    bootstrap.Modal.getOrCreateInstance(editModalEl).show();
  }

  // Kaydet: sunucuya gönderir, liste ve sayımlar tazelenir
  editForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearFieldErrors(editForm);

    const title = editForm.elements.namedItem("title");
    if (!title.value.trim()) {
      showFieldErrors(editForm, { title: text("error.title_required") });
      title.focus();
      return;
    }

    const submitBtn = document.getElementById("edit-submit");
    submitBtn.disabled = true;
    try {
      const payload = Object.fromEntries(new FormData(editForm).entries());
      const data = await post(editForm.action, payload);
      bootstrap.Modal.getOrCreateInstance(editModalEl).hide();
      // Listede ise parça tazelenir; takvimde ise sunucudan yeniden çizilir
      if (listEl) {
        await fetchList();
      } else {
        window.location.reload();
        return;
      }
      updateStats(data.stats);
      notify(text("js.saved"), "success");
    } catch (err) {
      if (err.fields && Object.keys(err.fields).length) showFieldErrors(editForm, err.fields);
      notify(err.message, "danger");
    } finally {
      submitBtn.disabled = false;
    }
  });

  // Arama: yazarken 260 ms bekleme ile liste tazelenir
  if (searchInput) {
    searchInput.addEventListener("input", () => {
      syncSearchUi();
      clearTimeout(searchTimer);
      searchTimer = setTimeout(runSearch, 260);
    });
    document.getElementById("search-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      clearTimeout(searchTimer);
      runSearch();
    });
  }

  document.getElementById("search-clear")?.addEventListener("click", resetSearch);

  // Kısayollar: "/" aramaya odaklanır, Escape aramayı temizler
  document.addEventListener("keydown", (event) => {
    const tag = (document.activeElement?.tagName || "").toLowerCase();
    const modalOpen = document.querySelector(".modal.show");
    if (event.key === "/" && !modalOpen && tag !== "input" && tag !== "textarea") {
      event.preventDefault();
      searchInput?.focus();
      searchInput?.select();
    } else if (event.key === "Escape" && document.activeElement === searchInput && searchInput.value) {
      resetSearch();
    }
  });

  /* --------------------------------------------------- tema (aydınlık/karanlık)
     Tercih localStorage'da tutulur; hiçbir sunucu isteği yapılmaz. Tema
     etiketi base.html'deki satır içi betikle (boyanmadan önce) konur. */
  const THEME_KEY = "theme";
  const rootEl = document.documentElement;
  const themeBtn = document.getElementById("theme-toggle");

  /** Temayı uygular; persist=true ise tarayıcıya hatırlatır. */
  function applyTheme(theme, persist) {
    const dark = theme === "dark";
    rootEl.setAttribute("data-bs-theme", dark ? "dark" : "light");
    if (persist) {
      try {
        localStorage.setItem(THEME_KEY, dark ? "dark" : "light");
      } catch { /* depolama kapalıysa sessizce geç */ }
    }
    if (!themeBtn) return;
    const icon = themeBtn.querySelector("i");
    if (icon) icon.className = dark ? "bi bi-sun" : "bi bi-moon-stars";
    const label = text(dark ? "theme.to_light" : "theme.to_dark");
    themeBtn.title = label;
    themeBtn.setAttribute("aria-label", label);
    themeBtn.setAttribute("aria-pressed", String(dark));
  }

  themeBtn?.addEventListener("click", () => {
    const dark = rootEl.getAttribute("data-bs-theme") === "dark";
    applyTheme(dark ? "light" : "dark", true);
  });

  // Kullanıcı seçim yapmadıysa sistem tercihi değiştiğinde tema da değişir
  const themeMedia = window.matchMedia("(prefers-color-scheme: dark)");
  const themeSaved = () => {
    try {
      return localStorage.getItem(THEME_KEY);
    } catch {
      return null;
    }
  };
  themeMedia.addEventListener?.("change", (event) => {
    if (!themeSaved()) applyTheme(event.matches ? "dark" : "light", false);
  });

  applyTheme(rootEl.getAttribute("data-bs-theme"), false); // ikon/başlığı hizala

  /* --------------------------------------------------- şifre gücü göstergesi
     Yalnızca bilgilendiricidir: gönderimi engellemez, dayatma yapmaz.
     Puan uzunluk + karakter çeşitliliğiyle hesaplanır, zayıf desenler
     (tekrarlar, bilinen başlangıçlar) düşülür. */
  const pwInput = document.getElementById("password");
  const strengthMeter = document.getElementById("strength-meter");

  if (pwInput && strengthMeter) {
    const strengthFill = document.getElementById("strength-fill");
    const strengthLabel = document.getElementById("strength-label");

    /** Şifreyi 0 (çok zayıf) - 4 (çok güçlü) arası sınıfa çevirir. */
    function passwordLevel(value) {
      if (!value) return -1;
      if (/^(.)\1+$/.test(value)) return 0;                 // "aaaaaa", "111111"
      let score = 0;
      if (value.length >= 8) score++;
      if (value.length >= 12) score++;
      if (/[a-zçğıöşü]/.test(value) && /[A-ZÇĞİÖŞÜ]/.test(value)) score++;
      if (/\d/.test(value)) score++;
      if (/[^A-Za-z0-9]/.test(value)) score++;
      // bilinen zayıf başlangıçlar tavanı kısıtlar
      if (/^(1234|1111|password|sifre|qwerty|asdf)/i.test(value)) {
        score = Math.min(score, 1);
      }
      return score <= 1 ? 0 : score === 2 ? 1 : score === 3 ? 2 : score === 4 ? 3 : 4;
    }

    pwInput.addEventListener("input", () => {
      const level = passwordLevel(pwInput.value.trim() ? pwInput.value : "");
      strengthMeter.hidden = level < 0;
      if (level < 0) return;
      strengthFill.style.width = `${(level + 1) * 20}%`;
      strengthFill.className = `strength-fill lvl-${level}`;
      strengthLabel.textContent = text(`auth.strength_${level}`);
      strengthLabel.className = `strength-label lvl-${level}`;
    });
  }

  // Sunucudan gelen yönlendirme bildirimlerini 5 sn sonra kapat
  document.querySelectorAll("#flash-area .alert-dismissible").forEach((alert) => {
    setTimeout(() => bootstrap.Alert.getOrCreateInstance(alert).close(), 5000);
  });

  syncSearchUi();
})();
