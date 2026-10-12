(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const s = {
    user: null,
    csrf: "",
    cameras: [],
    selected: "",
    system: null,
    media: [],
    schedules: [],
    events: [],
    view: "overview",
    kind: "",
    offset: 0,
    total: 0,
    lang: localStorage.getItem("campix-language") || "de",
    socket: null,
    peer: null,
    whep: null,
    toastTimer: null,
    refreshTimer: null,
    streamGeneration: 0,
    preview: null,
    dirtyControls: false,
    cameraDirty: false,
    storageDirty: false,
    refreshGeneration: 0,
  };
  const words = {
    signin: ["Anmelden", "Sign in"],
    username: ["Benutzername", "Username"],
    password: ["Passwort", "Password"],
    accountHint: [
      "Das erste Administratorkonto wird beim Einrichten des Dienstes angelegt.",
      "Create the first administrator account when setting up the service.",
    ],
    overview: ["Übersicht", "Overview"],
    media: ["Medien", "Media"],
    scheduler: ["Zeitpläne", "Schedules"],
    devices: ["Kameras", "Cameras"],
    settings: ["Einstellungen", "Settings"],
    logout: ["Abmelden", "Sign out"],
    refresh: ["Aktualisieren", "Refresh"],
    controlCenter: ["Kamera-Kontrollzentrum", "Camera control center"],
    addCamera: ["＋ Kamera hinzufügen", "＋ Add camera"],
    onlineCameras: ["KAMERAS ONLINE", "CAMERAS ONLINE"],
    freeStorage: ["SPEICHER FREI", "FREE STORAGE"],
    cpuLoad: ["CPU-LAST", "CPU LOAD"],
    uptime: ["DIENSTLAUFZEIT", "SERVICE UPTIME"],
    camera: ["Kamera", "Camera"],
    noCamera: [
      "Füge eine Kamera hinzu, um das Livebild zu starten.",
      "Add a camera to start the live view.",
    ],
    liveConnecting: ["Livebild wird verbunden …", "Connecting live view …"],
    liveUnavailable: [
      "Kein aktuelles Livebild. Verbindung wird erneut versucht …",
      "No current live frame. Retrying connection …",
    ],
    jpegLive: ["JPEG · Live", "JPEG · Live"],
    autoLive: ["WebRTC → JPEG · Live", "WebRTC → JPEG · Live"],
    capture: ["◎ Foto", "◎ Photo"],
    startRecording: ["Aufnehmen", "Record"],
    stopRecording: ["Aufnahme beenden", "Stop recording"],
    controls: ["Kamerasteuerung", "Camera controls"],
    exposure: ["Belichtung", "Exposure"],
    brightness: ["Helligkeit", "Brightness"],
    focus: ["Linsenposition", "Lens position"],
    motion: ["Bewegungserkennung", "Motion detection"],
    noControls: [
      "Diese Quelle bietet keine Sensor-Controls.",
      "This source exposes no sensor controls.",
    ],
    apply: ["✓ Anwenden", "✓ Apply"],
    recentMedia: ["Letzte Aufnahmen", "Recent captures"],
    showAll: ["Alle anzeigen →", "Show all →"],
    activity: ["Aktivität", "Activity"],
    mediaHint: [
      "Gespeicherte Fotos und abgeschlossene Aufnahmen.",
      "Saved photos and finalized recordings.",
    ],
    all: ["Alle", "All"],
    photos: ["Fotos", "Photos"],
    videos: ["Videos", "Videos"],
    scheduleHint: [
      "Fotos und zeitbegrenzte Aufnahmen mit gespeicherten Regeln.",
      "Photos and timed recordings with persistent rules.",
    ],
    newSchedule: ["＋ Neuer Zeitplan", "＋ New schedule"],
    devicesHint: [
      "Erkannte Fähigkeiten und tatsächlicher Verbindungsstatus.",
      "Detected capabilities and actual connection status.",
    ],
    scan: ["Geräte suchen", "Discover devices"],
    settingsHint: [
      "Kameraprofile, Speicher und Zugriffsrechte.",
      "Camera profiles, storage and permissions.",
    ],
    export: ["Konfiguration exportieren", "Export configuration"],
    adminRequired: [
      "Administratorrechte erforderlich.",
      "Administrator permissions required.",
    ],
    cameraProfile: ["Aktives Kameraprofil", "Active camera profile"],
    save: ["Speichern", "Save"],
    name: ["Name", "Name"],
    resolution: ["Auflösung", "Resolution"],
    storage: ["Speicher", "Storage"],
    retention: ["Aufbewahrung (Tage)", "Retention (days)"],
    reserve: ["Freier Mindestplatz (MB)", "Minimum free space (MB)"],
    retentionHint: [
      "Stündliche Bereinigung entfernt abgelaufene, ungesperrte Medien. Gesperrte Medien bleiben erhalten.",
      "Hourly cleanup removes expired, unlocked media. Locked media is retained.",
    ],
    users: ["Benutzer", "Users"],
    newUser: ["＋ Benutzer", "＋ User"],
    source: [
      "Quelle (Index, /dev/videoN, Gerätename oder URL)",
      "Source (index, /dev/videoN, device name or URL)",
    ],
    cameraHint: [
      "Treiber müssen auf dem Host installiert sein. Ein Gerät ist erst nach erfolgreichem Öffnen online.",
      "Drivers must be installed on the host. A device is online only after it opens successfully.",
    ],
    add: ["Hinzufügen", "Add"],
    action: ["Aktion", "Action"],
    interval: ["Intervall (Sekunden)", "Interval (seconds)"],
    duration: ["Videodauer (Sekunden)", "Video duration (seconds)"],
    startHour: ["Startstunde", "Start hour"],
    endHour: ["Endstunde", "End hour"],
    timezone: ["Zeitzone", "Timezone"],
    role: ["Rolle", "Role"],
    empty: ["Noch keine Einträge.", "No entries yet."],
    saved: ["Gespeichert", "Saved"],
    captured: ["Foto gespeichert", "Photo saved"],
    recorded: ["Aufnahme abgeschlossen", "Recording finalized"],
    started: ["Aufnahme gestartet", "Recording started"],
    enabled: ["Aktiv", "Enabled"],
    disabled: ["Aus", "Off"],
    delete: ["Löschen", "Delete"],
    download: ["Herunterladen", "Download"],
    lock: ["Sperren", "Lock"],
    unlock: ["Entsperren", "Unlock"],
    confirmDelete: [
      "Dieses Element dauerhaft löschen?",
      "Permanently delete this item?",
    ],
    noDevices: [
      "Keine Geräte erkannt. Netzwerkquellen kannst du manuell hinzufügen.",
      "No devices detected. Network sources can be added manually.",
    ],
    removeCamera: ["Kamera entfernen", "Remove camera"],
    noHls: [
      "HLS wird nicht nativ unterstützt; JPEG-Livebild wird verwendet.",
      "HLS is not supported natively; using live JPEG.",
    ],
    timelapseHint: [
      "Alle Fotos der aktuellen Medienseite in zeitlicher Reihenfolge als Video exportieren?",
      "Export all photos on the current media page, ordered by capture time, as a video?",
    ],
  };
  const t = (key) => words[key]?.[s.lang === "de" ? 0 : 1] || key;
  const make = (tag, className, text) => {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined) e.textContent = text;
    return e;
  };
  const active = () => s.cameras.find((c) => c.id === s.selected);
  const admin = () => s.user?.role === "admin";
  const operate = () => ["operator", "admin"].includes(s.user?.role);
  const bytes = (n) =>
    n === null || !Number.isFinite(n)
      ? "—"
      : `${(n / (n >= 1024 ** 3 ? 1024 ** 3 : 1024 ** 2)).toFixed(1)} ${n >= 1024 ** 3 ? "GB" : "MB"}`;
  const date = (n) =>
    new Date(n * 1000).toLocaleString(s.lang === "de" ? "de-DE" : "en-GB");

  function toast(message, error = false) {
    $("#toast-message").textContent = message;
    $("#toast").classList.toggle("error-toast", error);
    $("#toast").classList.add("show");
    clearTimeout(s.toastTimer);
    s.toastTimer = setTimeout(
      () => $("#toast").classList.remove("show"),
      error ? 6000 : 3000,
    );
  }
  async function api(path, method = "GET", body) {
    const r = await fetch("/api/v1" + path, {
      method,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...(method !== "GET" ? { "X-CSRF-Token": s.csrf } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(45000),
    });
    const result = await r.json().catch(() => ({ detail: r.statusText }));
    if (!r.ok) {
      if (r.status === 401 && path !== "/auth/login") showLogin();
      throw new Error(
        typeof result.detail === "string"
          ? result.detail
          : JSON.stringify(result.detail),
      );
    }
    return result;
  }
  async function act(handler, button) {
    if (button) button.disabled = true;
    try {
      await handler();
    } catch (err) {
      toast(err.message, true);
    } finally {
      if (button) button.disabled = false;
      permissions();
    }
  }
  function permissions() {
    $$("[data-admin]").forEach((e) => {
      e.hidden = !admin();
    });
    $$("[data-operator]").forEach((e) => {
      e.disabled = !operate() || active()?.status !== "online";
    });
    $("#timelapse-button").disabled = !operate();
    $("#settings-forbidden").hidden = admin();
    $("#apply-controls").disabled =
      !operate() || !active()?.capabilities.controls.length;
  }
  function translate() {
    document.documentElement.lang = s.lang;
    $$("[data-i18n]").forEach((e) => {
      e.textContent = t(e.dataset.i18n);
    });
    $("#language-button").textContent = s.lang === "de" ? "EN" : "DE";
    $("#breadcrumb-current").textContent = t(s.view);
    if (s.user) render();
  }
  function switchView(view) {
    if (
      !["overview", "media", "scheduler", "devices", "settings"].includes(view)
    )
      return;
    s.view = view;
    $$(".nav-item").forEach((e) =>
      e.classList.toggle("active", e.dataset.view === view),
    );
    $$("[data-view-panel]").forEach((e) =>
      e.classList.toggle("active", e.dataset.viewPanel === view),
    );
    $("#breadcrumb-current").textContent = t(view);
    $("#sidebar").classList.remove("open");
    history.replaceState(
      null,
      "",
      view === "overview" ? location.pathname : "#" + view,
    );
    if (view === "overview" && !document.hidden) startStream();
    else stopStream();
  }
  function showLogin() {
    s.user = null;
    s.refreshGeneration++;
    s.cameraDirty = s.storageDirty = s.dirtyControls = false;
    $("#user-form").reset();
    $("#camera-form").reset();
    $("#workspace").hidden = true;
    $("#login-screen").hidden = false;
    if (s.socket) s.socket.close();
    stopStream();
    s.csrf = "";
    $$("dialog[open]").forEach((d) => d.close());
  }
  async function connect() {
    const me = await api("/auth/me");
    s.user = me.user;
    s.csrf = me.csrf;
    $("#login-screen").hidden = true;
    $("#workspace").hidden = false;
    $("#login-form [name=password]").value = "";
    await refresh();
    connectEvents();
    startStream();
  }
  async function refresh() {
    if (!s.user) return;
    const generation = ++s.refreshGeneration;
    const [cameras, system, media, schedules, events] = await Promise.all([
      api("/cameras"),
      api("/system"),
      api(
        `/media?limit=24&offset=${s.offset}${s.kind ? "&kind=" + s.kind : ""}`,
      ),
      api("/schedules"),
      api("/events?limit=10"),
    ]);
    if (!s.user || generation !== s.refreshGeneration) return;
    s.cameras = cameras;
    s.system = system;
    s.media = media.items;
    s.total = media.total;
    s.schedules = schedules;
    s.events = events;
    if (!cameras.some((c) => c.id === s.selected)) {
      s.selected = cameras[0]?.id || "";
      s.cameraDirty = s.dirtyControls = false;
    }
    render();
    if (active()?.status !== "online") {
      stopStream();
      $("#live-empty").hidden = false;
    }
    if (admin()) {
      const [settings, users] = await Promise.all([
        api("/settings"),
        api("/users"),
      ]);
      if (!s.user || generation !== s.refreshGeneration) return;
      if (
        !s.storageDirty &&
        !$("#storage-settings-form").contains(document.activeElement)
      ) {
        $("#setting-retention").value = settings.retention_days;
        $("#setting-reserve").value = settings.min_free_mb;
      }
      renderUsers(users);
    }
  }
  function options(select) {
    const prev = select.value;
    select.replaceChildren(
      ...s.cameras.map((c) => {
        const o = make("option", "", c.name);
        o.value = c.id;
        return o;
      }),
    );
    select.value =
      select.id === "camera-select"
        ? s.selected
        : s.cameras.some((c) => c.id === prev)
          ? prev
          : s.selected;
  }
  function render() {
    if (!s.system || !s.user) return;
    const sys = s.system;
    $("#version-label").textContent = sys.version;
    $("#mode-label").textContent = sys.demo
      ? "SYNTHETIC TEST MODE"
      : "CAMERA SERVICE";
    $("#connection-label").textContent = sys.platform.model;
    $("#connection-detail").textContent =
      `${sys.platform.system} · ${sys.platform.arch}`;
    $("#user-label").textContent = s.user.username;
    $("#role-label").textContent = s.user.role;
    $("#camera-count").textContent =
      `${s.cameras.filter((c) => c.status === "online").length} / ${s.cameras.length}`;
    $("#storage-free").textContent = bytes(sys.disk_free_bytes);
    $("#cpu-load").textContent = `${sys.cpu_percent.toFixed(1)} %`;
    $("#uptime").textContent =
      `${Math.floor(sys.uptime_seconds / 3600)}h ${Math.floor((sys.uptime_seconds % 3600) / 60)}m`;
    $("#system-summary").textContent =
      `${sys.platform.model} · ${bytes(sys.process_memory_bytes)} RAM${sys.temperature_c === null ? "" : " · " + sys.temperature_c.toFixed(1) + " °C"}`;
    $("#nav-media-count").textContent = s.total;
    options($("#camera-select"));
    options($("#schedule-camera"));
    $("#synthetic-option").hidden = !sys.demo;
    renderCamera();
    renderMedia();
    renderEvents();
    renderSchedules();
    renderDevices();
    permissions();
    const details = [
      ["Version", sys.version],
      ["Platform", `${sys.platform.system} / ${sys.platform.arch}`],
      ["Memory", bytes(sys.process_memory_bytes)],
      [
        "MediaMTX",
        sys.media_plane.configured
          ? sys.media_plane.version || "external"
          : t("disabled"),
      ],
      ["Scheduler", sys.scheduler_alive ? "running" : "stopped"],
    ];
    $("#system-details").replaceChildren(
      ...details.flatMap(([k, v]) => [make("dt", "", k), make("dd", "", v)]),
    );
  }
  const defaultResolutions = [...$("#setting-resolution").options].map(
    (o) => o.value,
  );
  function resolutionOptions(c, size) {
    // Cameras that report native modes offer only those sizes.
    const sizes = c.capabilities.modes?.length
      ? c.capabilities.modes
      : defaultResolutions;
    const values = sizes.includes(size) ? sizes : [...sizes, size];
    const select = $("#setting-resolution");
    if (values.join() !== [...select.options].map((o) => o.value).join())
      select.replaceChildren(
        ...values.map((value) => {
          const option = make("option", "", value.replace("x", " × "));
          option.value = value;
          return option;
        }),
      );
    select.value = size;
  }
  function renderCamera() {
    const c = active();
    $("#active-camera-name").textContent = c?.name || t("camera");
    $("#camera-state").textContent = c
      ? `${c.status}${c.error ? " · " + c.error : ""}`
      : "—";
    $("#actual-fps").textContent = c ? `${c.fps} / ${c.fps_target} FPS` : "—";
    $("#actual-resolution").textContent = c ? `${c.width} × ${c.height}` : "—";
    $("#backend-label").textContent = c?.backend || "—";
    $("#sensor-label").textContent = c?.capabilities.model || "—";
    $("#record-label").textContent = t(
      c?.recording ? "stopRecording" : "startRecording",
    );
    $("#record-button").classList.toggle("recording", !!c?.recording);
    $("#motion-state").textContent = t(
      c?.motion_enabled ? "enabled" : "disabled",
    );
    $$("[data-control]").forEach((e) => {
      e.hidden = !c?.capabilities.controls.includes(e.dataset.control);
    });
    $("#controls-empty").hidden = !!c?.capabilities.controls.length;
    if (!s.dirtyControls) {
      for (const [key, id] of [
        ["exposure", "exposure-range"],
        ["brightness", "brightness-range"],
        ["lens_position", "focus-range"],
      ])
        $("#" + id).value = c?.controls[key] ?? 0;
      $("#autofocus-checkbox").checked = !!c?.controls.autofocus;
    }
    outputs();
    if (
      c &&
      !s.cameraDirty &&
      !$("#camera-settings-form").contains(document.activeElement)
    ) {
      $("#setting-camera-name").value = c.name;
      resolutionOptions(c, `${c.width}x${c.height}`);
      $("#setting-fps").value = c.fps_target;
      $("#setting-preroll").value = c.preroll_seconds;
      $("#setting-motion").checked = c.motion_enabled;
      $("#setting-motion-threshold").value = c.motion_threshold;
    }
  }
  function outputs() {
    $("#exposure-output").textContent =
      `${Number($("#exposure-range").value).toFixed(1)} EV`;
    $("#brightness-output").textContent = Number(
      $("#brightness-range").value,
    ).toFixed(2);
    $("#focus-output").textContent = Number($("#focus-range").value).toFixed(1);
  }
  function stopPreview() {
    if (!s.preview) return;
    s.preview.request?.abort();
    clearTimeout(s.preview.timer);
    if (s.preview.url) URL.revokeObjectURL(s.preview.url);
    if (s.preview.pendingUrl) URL.revokeObjectURL(s.preview.pendingUrl);
    s.preview = null;
  }
  function closePeer() {
    if (s.peer) s.peer.close();
    s.peer = null;
    if (s.whep && s.csrf)
      fetch(s.whep, {
        method: "DELETE",
        headers: { "X-CSRF-Token": s.csrf },
      }).catch(() => {});
    s.whep = null;
  }
  function liveMessage(key) {
    const message = $("#live-empty p");
    message.dataset.i18n = key;
    message.textContent = t(key);
    $("#live-empty").hidden = false;
  }
  function decodePreview(image, signal) {
    return new Promise((resolve, reject) => {
      const finish = (handler, value) => {
        signal.removeEventListener("abort", abort);
        handler(value);
      };
      const abort = () => {
        image.removeAttribute("src");
        finish(reject, new DOMException("Live frame cancelled", "AbortError"));
      };
      if (signal.aborted) return abort();
      signal.addEventListener("abort", abort, { once: true });
      image.decode().then(
        () => finish(resolve),
        (error) => finish(reject, error),
      );
    });
  }
  function stopStream() {
    s.streamGeneration++;
    stopPreview();
    $("#live-image").removeAttribute("src");
    $("#live-image").hidden = true;
    $("#live-video").pause();
    $("#live-video").removeAttribute("src");
    $("#live-video").srcObject = null;
    $("#live-video").hidden = true;
    closePeer();
    $("#live-badge").hidden = true;
  }
  function jpegPreview(gen) {
    if (
      gen !== s.streamGeneration ||
      !s.user ||
      s.view !== "overview" ||
      document.hidden ||
      active()?.status !== "online" ||
      s.preview?.generation === gen
    )
      return;
    closePeer();
    $("#live-badge").hidden = true;
    $("#live-image").removeAttribute("src");
    $("#live-image").hidden = true;
    $("#live-video").pause();
    $("#live-video").removeAttribute("src");
    $("#live-video").srcObject = null;
    $("#live-video").hidden = true;
    $("#actual-transport").textContent = "JPEG";
    liveMessage("liveConnecting");
    const camera = s.selected;
    const preview = {
      generation: gen,
      request: null,
      timer: null,
      url: null,
      pendingUrl: null,
    };
    s.preview = preview;
    const current = () => gen === s.streamGeneration && s.preview === preview;
    async function next() {
      if (!current()) return;
      const started = performance.now();
      const controller = new AbortController();
      preview.request = controller;
      const timeout = setTimeout(() => controller.abort(), 2500);
      let url;
      let ready = false;
      try {
        // Request the current frame only after the preceding frame is decoded.
        // Multipart image buffering cannot be bounded by the browser UI.
        const response = await fetch(
          `/api/v1/cameras/${encodeURIComponent(camera)}/preview.jpg`,
          {
            credentials: "same-origin",
            cache: "no-store",
            signal: controller.signal,
          },
        );
        if (!current()) return;
        if (response.status === 401) {
          showLogin();
          return;
        }
        if (!response.ok) throw new Error("Live frame unavailable");
        url = URL.createObjectURL(await response.blob());
        preview.pendingUrl = url;
        const image = new Image();
        image.src = url;
        await decodePreview(image, controller.signal);
        if (!current()) return;
        if (controller.signal.aborted) throw new Error("Live frame timed out");
        const displayed = $("#live-image");
        displayed.src = url;
        await decodePreview(displayed, controller.signal);
        if (!current()) return;
        if (controller.signal.aborted) throw new Error("Live frame timed out");
        const previous = preview.url;
        preview.url = url;
        url = null;
        if (previous) URL.revokeObjectURL(previous);
        displayed.hidden = false;
        $("#live-empty").hidden = true;
        $("#live-badge").hidden = false;
        ready = true;
      } catch (_) {
        if (!current()) return;
        $("#live-image").removeAttribute("src");
        $("#live-image").hidden = true;
        if (preview.url) URL.revokeObjectURL(preview.url);
        preview.url = null;
        $("#live-badge").hidden = true;
        liveMessage("liveUnavailable");
      } finally {
        clearTimeout(timeout);
        if (url) URL.revokeObjectURL(url);
        preview.pendingUrl = null;
        if (current()) {
          preview.request = null;
          const interval =
            1000 / Math.max(1, Math.min(30, active().fps_target));
          preview.timer = setTimeout(
            next,
            ready ? Math.max(0, interval - (performance.now() - started)) : 500,
          );
        }
      }
    }
    next();
  }
  async function startStream() {
    stopStream();
    const gen = s.streamGeneration;
    if (
      !s.user ||
      s.view !== "overview" ||
      document.hidden ||
      active()?.status !== "online"
    ) {
      liveMessage("noCamera");
      return;
    }
    const mode = $("#transport-select").value;
    if (mode === "hls") {
      if ($("#live-video").canPlayType("application/vnd.apple.mpegurl")) {
        $("#live-empty").hidden = true;
        $("#live-video").hidden = false;
        $("#live-video").src = `/api/v1/streams/${s.selected}/hls/index.m3u8`;
        $("#actual-transport").textContent = "HLS";
        $("#live-video")
          .play()
          .catch(() => jpegPreview(gen));
        return;
      }
      toast(t("noHls"));
    }
    if (
      mode !== "auto" ||
      !s.system.media_plane.configured ||
      !window.RTCPeerConnection
    ) {
      jpegPreview(gen);
      return;
    }
    let peer;
    try {
      peer = new RTCPeerConnection();
      s.peer = peer;
      peer.addTransceiver("video", { direction: "recvonly" });
      let timer;
      const track = new Promise((resolve, reject) => {
        timer = setTimeout(() => reject(new Error("WebRTC timeout")), 8000);
        peer.ontrack = (e) => {
          clearTimeout(timer);
          resolve(e.streams[0] || new MediaStream([e.track]));
        };
      });
      track.catch(() => {});
      await peer.setLocalDescription(await peer.createOffer());
      await new Promise((resolve) => {
        if (peer.iceGatheringState === "complete") return resolve();
        const timeout = setTimeout(resolve, 1500);
        peer.addEventListener("icegatheringstatechange", () => {
          if (peer.iceGatheringState === "complete") {
            clearTimeout(timeout);
            resolve();
          }
        });
      });
      const r = await fetch(`/api/v1/streams/${s.selected}/webrtc/whep`, {
        method: "POST",
        headers: { "Content-Type": "application/sdp", "X-CSRF-Token": s.csrf },
        body: peer.localDescription.sdp,
        signal: AbortSignal.timeout(10000),
      });
      if (!r.ok) throw new Error("WHEP unavailable");
      const session = r.headers.get("Location");
      if (gen !== s.streamGeneration) {
        if (session)
          fetch(session, {
            method: "DELETE",
            headers: { "X-CSRF-Token": s.csrf },
          }).catch(() => {});
        clearTimeout(timer);
        peer.close();
        return;
      }
      s.whep = session;
      await peer.setRemoteDescription({ type: "answer", sdp: await r.text() });
      const stream = await track;
      if (gen !== s.streamGeneration) {
        peer.close();
        return;
      }
      $("#live-empty").hidden = true;
      $("#live-video").hidden = false;
      $("#live-video").srcObject = stream;
      $("#live-video")
        .play()
        .catch(() => {});
      $("#actual-transport").textContent = "WebRTC";
      $("#live-badge").hidden = false;
      peer.onconnectionstatechange = () => {
        if (["failed", "disconnected"].includes(peer.connectionState)) {
          peer.close();
          jpegPreview(gen);
        }
      };
    } catch (_) {
      if (peer) peer.close();
      jpegPreview(gen);
    }
  }
  function mediaCard(item, compact = false) {
    const card = make(
      "button",
      compact ? "media-card real-media-card" : "gallery-card real-gallery-card",
    );
    card.setAttribute("aria-label", item.filename);
    if (item.kind === "snapshot") {
      const img = make("img", "media-thumbnail");
      img.src = `/api/v1/media/${item.id}/file`;
      img.alt = "";
      img.loading = "lazy";
      card.append(img);
    } else card.append(make("span", "video-symbol", "▶"));
    card.append(
      make(
        "span",
        compact ? "media-type" : "gallery-type",
        `${item.kind === "snapshot" ? "JPG" : "MP4"} ${item.locked ? "▣" : ""}`,
      ),
    );
    const info = make("div", "gallery-info");
    info.append(
      make("strong", "", item.filename),
      make("small", "", `${date(item.created)} · ${bytes(item.size)}`),
    );
    card.append(info);
    card.addEventListener("click", () => openMedia(item));
    return card;
  }
  function renderMedia() {
    const search = $("#media-search").value.trim().toLowerCase();
    const items = s.media.filter((i) =>
      i.filename.toLowerCase().includes(search),
    );
    $("#media-grid").replaceChildren(...items.map((i) => mediaCard(i)));
    if (!items.length)
      $("#media-grid").append(make("p", "empty-message", t("empty")));
    $("#recent-media").replaceChildren(
      ...s.media.slice(0, 3).map((i) => mediaCard(i, true)),
    );
    if (!s.media.length)
      $("#recent-media").append(make("p", "empty-message", t("empty")));
    $("#media-page").textContent =
      `${Math.floor(s.offset / 24) + 1} · ${s.total}`;
    $("#media-prev").disabled = s.offset === 0;
    $("#media-next").disabled = s.offset + 24 >= s.total;
  }
  function openMedia(item) {
    $("#media-dialog-title").textContent = item.filename;
    const file = `/api/v1/media/${item.id}/file`;
    const content = make(
      item.kind === "snapshot" ? "img" : "video",
      "media-preview",
    );
    content.src = file;
    if (item.kind !== "snapshot") {
      content.controls = true;
      content.playsInline = true;
    }
    $("#media-viewer").replaceChildren(content);
    const download = make("a", "button ghost", t("download"));
    download.href = file + "?download=true";
    download.download = item.filename;
    const actions = [download];
    if (operate()) {
      const lock = make(
        "button",
        "button soft",
        t(item.locked ? "unlock" : "lock"),
      );
      lock.addEventListener("click", () =>
        act(async () => {
          await api(`/media/${item.id}`, "PATCH", { locked: !item.locked });
          $("#media-dialog").close();
          await refresh();
        }, lock),
      );
      const remove = make("button", "button danger", t("delete"));
      remove.disabled = !!item.locked;
      remove.addEventListener("click", () =>
        act(async () => {
          if (!confirm(t("confirmDelete"))) return;
          await api(`/media/${item.id}`, "DELETE");
          $("#media-dialog").close();
          await refresh();
        }, remove),
      );
      actions.push(lock, remove);
    }
    $("#media-dialog-actions").replaceChildren(...actions);
    $("#media-dialog").showModal();
  }
  function renderEvents() {
    $("#recent-events").replaceChildren(
      ...s.events.slice(0, 5).map((e) => {
        const row = make("div", "event-item");
        const text = make("div");
        text.append(
          make("strong", "", e.message),
          make("small", "", `${e.camera_id || "CamPiX"} · ${date(e.created)}`),
        );
        row.append(
          make(
            "span",
            "event-marker cyan-marker",
            e.kind.includes("error") ? "!" : "⌁",
          ),
          text,
        );
        return row;
      }),
    );
  }
  function renderSchedules() {
    $("#schedule-list").replaceChildren(
      ...s.schedules.map((job) => {
        const card = make("article", "panel schedule-rule");
        const text = make("div");
        text.append(
          make("h3", "", job.name),
          make(
            "p",
            "subhead",
            `${job.camera_id} · ${job.action} · ${job.interval_seconds}s · ${job.start_hour}:00–${job.end_hour}:00 · ${job.timezone}`,
          ),
          make("small", "", job.enabled ? date(job.next_run) : t("disabled")),
        );
        card.append(text);
        if (admin()) {
          const toggle = make(
            "button",
            "button soft",
            t(job.enabled ? "disabled" : "enabled"),
          );
          toggle.addEventListener("click", () =>
            act(async () => {
              const { next_run, ...payload } = job;
              await api("/schedules", "POST", {
                ...payload,
                enabled: !job.enabled,
              });
              await refresh();
            }, toggle),
          );
          const remove = make("button", "button ghost", t("delete"));
          remove.addEventListener("click", () =>
            act(async () => {
              if (!confirm(t("confirmDelete"))) return;
              await api(`/schedules/${job.id}`, "DELETE");
              await refresh();
            }, remove),
          );
          card.append(toggle, remove);
        }
        return card;
      }),
    );
    if (!s.schedules.length)
      $("#schedule-list").append(make("p", "empty-message", t("empty")));
  }
  function renderDevices() {
    $("#device-list").replaceChildren(
      ...s.cameras.map((c) => {
        const card = make("article", "panel device-runtime");
        const head = make("div", "panel-header");
        const title = make("div");
        title.append(
          make("h3", "", c.name),
          make(
            "p",
            "subhead",
            `${c.backend} · ${c.capabilities.model || "—"} · ${c.status}`,
          ),
        );
        head.append(title);
        const select = make("button", "button soft", "Live");
        select.addEventListener("click", () => {
          s.selected = c.id;
          s.cameraDirty = s.dirtyControls = false;
          renderCamera();
          permissions();
          switchView("overview");
        });
        head.append(select);
        card.append(head);
        const details = make("dl", "capability-list");
        for (const [k, v] of [
          ["Profile", `${c.width} × ${c.height} @ ${c.fps_target}`],
          ["Observed FPS", c.fps],
          ["Controls", c.capabilities.controls.join(", ") || "—"],
          ["Hardware verified", c.encoding.hardware_verified ? "yes" : "no"],
          ["Encoders", c.encoding.encoders.join(", ") || "—"],
          ["Test status", c.capabilities.test_status || "unverified"],
          ["Motion", t(c.motion_enabled ? "enabled" : "disabled")],
          ["Dropped recording frames", c.dropped_frames],
        ])
          details.append(make("dt", "", k), make("dd", "", String(v)));
        card.append(details);
        if (c.error) card.append(make("p", "form-error control-note", c.error));
        if (admin()) {
          const remove = make(
            "button",
            "button ghost device-remove",
            t("removeCamera"),
          );
          remove.addEventListener("click", () =>
            act(async () => {
              if (!confirm(t("confirmDelete"))) return;
              await api(`/cameras/${c.id}`, "DELETE");
              await refresh();
              startStream();
            }, remove),
          );
          card.append(remove);
        }
        return card;
      }),
    );
    if (!s.cameras.length)
      $("#device-list").append(make("p", "empty-message", t("empty")));
  }
  function renderUsers(users) {
    $("#users-list").replaceChildren(
      ...users.map((u) => {
        const row = make("div", "user-row");
        row.append(make("strong", "", u.username));
        const roles = make("select", "user-role-select");
        ["viewer", "operator", "admin"].forEach((role) => {
          const option = make("option", "", role);
          option.value = role;
          roles.append(option);
        });
        roles.value = u.role;
        roles.addEventListener("change", () =>
          act(async () => {
            await api(`/users/${u.id}`, "PATCH", { role: roles.value });
            await refresh();
          }, roles),
        );
        const toggle = make(
          "button",
          "button ghost",
          t(u.disabled ? "enabled" : "disabled"),
        );
        toggle.addEventListener("click", () =>
          act(async () => {
            await api(`/users/${u.id}`, "PATCH", { disabled: !u.disabled });
            await refresh();
          }, toggle),
        );
        row.append(roles, toggle);
        return row;
      }),
    );
  }
  function connectEvents() {
    if (s.socket) s.socket.close();
    const ws = new WebSocket(
      `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/v1/events/ws`,
    );
    s.socket = ws;
    ws.onopen = () => {
      $("#event-connection").textContent = "WebSocket";
    };
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.kind === "heartbeat") return;
      clearTimeout(s.refreshTimer);
      s.refreshTimer = setTimeout(
        () =>
          act(async () => {
            await refresh();
            if (
              (message.kind === "camera.online" ||
                (message.kind === "stream.ready" &&
                  $("#transport-select").value === "auto")) &&
              message.camera_id === s.selected
            )
              startStream();
          }),
        300,
      );
    };
    ws.onclose = () => {
      $("#event-connection").textContent = "Polling";
      if (s.user && s.socket === ws)
        setTimeout(() => {
          if (s.user) connectEvents();
        }, 5000);
    };
  }

  $("#login-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.currentTarget,
      button = e.submitter;
    button.disabled = true;
    $("#login-error").textContent = "";
    api("/auth/login", "POST", Object.fromEntries(new FormData(form)))
      .then(() => connect())
      .catch((err) => {
        $("#login-error").textContent = err.message;
      })
      .finally(() => {
        button.disabled = false;
      });
  });
  $("#logout-button").addEventListener("click", () =>
    act(async () => {
      await api("/auth/logout", "POST");
      showLogin();
    }),
  );
  $$(".nav-item").forEach((e) =>
    e.addEventListener("click", () => switchView(e.dataset.view)),
  );
  $$("[data-view-target]").forEach((e) =>
    e.addEventListener("click", () => switchView(e.dataset.viewTarget)),
  );
  $("#mobile-menu").addEventListener("click", () =>
    $("#sidebar").classList.toggle("open"),
  );
  $("#refresh-button").addEventListener("click", (e) =>
    act(async () => {
      await refresh();
      if (s.view === "overview") startStream();
    }, e.currentTarget),
  );
  $("#theme-toggle").addEventListener("click", () => {
    const theme =
      document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("campix-theme", theme);
  });
  $("#language-button").addEventListener("click", () => {
    s.lang = s.lang === "de" ? "en" : "de";
    localStorage.setItem("campix-language", s.lang);
    translate();
  });
  $("#camera-select").addEventListener("change", (e) => {
    s.selected = e.target.value;
    s.cameraDirty = s.dirtyControls = false;
    renderCamera();
    permissions();
    startStream();
  });
  $("#transport-select").addEventListener("change", startStream);
  $("#live-image").addEventListener("error", () => {
    if (!s.preview) return;
    $("#live-badge").hidden = true;
    liveMessage("liveUnavailable");
  });
  $("#live-video").addEventListener("error", () =>
    jpegPreview(s.streamGeneration),
  );
  $("#fullscreen-button").addEventListener("click", () =>
    act(async () => {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await $("#live-stage").requestFullscreen();
    }),
  );
  for (const id of ["exposure-range", "brightness-range", "focus-range"])
    $("#" + id).addEventListener("input", () => {
      s.dirtyControls = true;
      outputs();
    });
  $("#autofocus-checkbox").addEventListener("change", () => {
    s.dirtyControls = true;
  });
  $("#apply-controls").addEventListener("click", (e) =>
    act(async () => {
      const payload = {};
      for (const [k, v] of [
        ["exposure", Number($("#exposure-range").value)],
        ["brightness", Number($("#brightness-range").value)],
        ["lens_position", Number($("#focus-range").value)],
        ["autofocus", $("#autofocus-checkbox").checked],
      ])
        if (active().capabilities.controls.includes(k)) payload[k] = v;
      await api(`/cameras/${s.selected}/controls`, "PATCH", payload);
      s.dirtyControls = false;
      toast(t("saved"));
      await refresh();
    }, e.currentTarget),
  );
  $$("[data-capture]").forEach((button) =>
    button.addEventListener("click", () =>
      act(async () => {
        const item = await api(`/cameras/${s.selected}/snapshot`, "POST");
        toast(`${t("captured")} · ${item.filename}`);
        s.offset = 0;
        await refresh();
      }, button),
    ),
  );
  $("#record-button").addEventListener("click", (e) =>
    act(async () => {
      const recording = active().recording;
      await api(
        `/cameras/${s.selected}/recording`,
        recording ? "DELETE" : "POST",
        recording ? undefined : {},
      );
      toast(t(recording ? "recorded" : "started"));
      await refresh();
    }, e.currentTarget),
  );
  $$("[data-close]").forEach((b) =>
    b.addEventListener("click", () => b.closest("dialog").close()),
  );
  $("#media-dialog").addEventListener("close", () => {
    const video = $("#media-viewer video");
    if (video) video.pause();
    $("#media-viewer").replaceChildren();
  });
  function addCamera(backend = "picamera2", source = "0", name = "") {
    $("#new-camera-backend").value = backend;
    $("#new-camera-source").value = source;
    $("#camera-form [name=name]").value = name;
    $("#camera-dialog").showModal();
  }
  $$("[data-add-camera]").forEach((b) =>
    b.addEventListener("click", () => addCamera()),
  );
  $("#new-camera-backend").addEventListener("change", (e) => {
    $("#new-camera-source").value = {
      picamera2: "0",
      v4l2: "/dev/video0",
      dshow: "",
      network: "rtsp://",
      legacy: "/dev/shm/mjpeg/cam.jpg",
      synthetic: "0",
    }[e.target.value];
  });
  $("#camera-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.currentTarget));
    act(async () => {
      const item = await api("/cameras", "POST", {
        ...payload,
        id: "cam-" + crypto.randomUUID().slice(0, 8),
      });
      $("#camera-dialog").close();
      s.selected = item.id;
      toast(t("saved"));
      await refresh();
      startStream();
    }, e.submitter);
  });
  $("#scan-button").addEventListener("click", (e) =>
    act(async () => {
      const result = await api("/discovery");
      $("#discovery-list").hidden = false;
      $("#discovery-list").replaceChildren(
        ...result.devices.map((d) => {
          const b = make("button", "button soft", `${d.model} · ${d.source}`);
          b.addEventListener("click", () =>
            addCamera(d.backend, d.source, d.model),
          );
          return b;
        }),
      );
      if (!result.devices.length)
        $("#discovery-list").append(make("p", "empty-message", t("noDevices")));
    }, e.currentTarget),
  );
  $("#new-schedule").addEventListener("click", () => {
    if (!s.cameras.length) return toast(t("noCamera"), true);
    options($("#schedule-camera"));
    $("#schedule-dialog").showModal();
  });
  $("#schedule-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.currentTarget));
    for (const key of [
      "interval_seconds",
      "duration_seconds",
      "start_hour",
      "end_hour",
    ])
      payload[key] = Number(payload[key]);
    act(async () => {
      await api("/schedules", "POST", {
        ...payload,
        id: "job-" + crypto.randomUUID().slice(0, 8),
      });
      $("#schedule-dialog").close();
      toast(t("saved"));
      await refresh();
    }, e.submitter);
  });
  $("#new-user").addEventListener("click", () => $("#user-dialog").showModal());
  $("#user-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.currentTarget,
      payload = Object.fromEntries(new FormData(form));
    act(async () => {
      await api("/users", "POST", payload);
      form.reset();
      $("#user-dialog").close();
      toast(t("saved"));
      await refresh();
    }, e.submitter);
  });
  $("#camera-settings-form").addEventListener("input", () => {
    s.cameraDirty = true;
  });
  $("#storage-settings-form").addEventListener("input", () => {
    s.storageDirty = true;
  });
  $("#camera-settings-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const [width, height] = $("#setting-resolution")
      .value.split("x")
      .map(Number);
    const payload = {
      name: $("#setting-camera-name").value,
      width,
      height,
      fps: Number($("#setting-fps").value),
      preroll_seconds: Number($("#setting-preroll").value),
      motion_enabled: $("#setting-motion").checked,
      motion_threshold: Number($("#setting-motion-threshold").value),
    };
    act(async () => {
      if (!active()) throw new Error(t("noCamera"));
      await api(`/cameras/${s.selected}`, "PATCH", payload);
      s.cameraDirty = false;
      toast(t("saved"));
      await refresh();
    }, e.submitter);
  });
  $("#storage-settings-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const payload = {
      retention_days: Number($("#setting-retention").value),
      min_free_mb: Number($("#setting-reserve").value),
    };
    act(async () => {
      await api("/settings", "PATCH", payload);
      s.storageDirty = false;
      toast(t("saved"));
      await refresh();
    }, e.submitter);
  });
  $("#export-button").addEventListener("click", (e) =>
    act(async () => {
      const config = await api("/export");
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(config, null, 2)], {
          type: "application/json",
        }),
      );
      const link = make("a");
      link.href = url;
      link.download = "campix-config.json";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }, e.currentTarget),
  );
  $$("#media-filters button").forEach((b) =>
    b.addEventListener("click", () =>
      act(async () => {
        s.kind = b.dataset.kind;
        s.offset = 0;
        $$("#media-filters button").forEach((e) =>
          e.classList.toggle("active", e === b),
        );
        await refresh();
      }),
    ),
  );
  $("#media-search").addEventListener("input", renderMedia);
  $("#media-prev").addEventListener("click", () =>
    act(async () => {
      s.offset = Math.max(0, s.offset - 24);
      await refresh();
    }),
  );
  $("#media-next").addEventListener("click", () =>
    act(async () => {
      s.offset += 24;
      await refresh();
    }),
  );
  $("#timelapse-button").addEventListener("click", (e) =>
    act(async () => {
      const ids = s.media
        .filter((i) => i.kind === "snapshot")
        .sort((a, b) => a.created - b.created)
        .map((i) => i.id);
      if (ids.length < 2)
        throw new Error(
          s.lang === "de"
            ? "Mindestens zwei Fotos erforderlich."
            : "At least two photos required.",
        );
      if (!confirm(t("timelapseHint"))) return;
      await api("/timelapse", "POST", { media_ids: ids, fps: 10 });
      toast(
        s.lang === "de"
          ? "Timelapse-Export eingereiht. Der Fortschritt erscheint unter Aktivität."
          : "Timelapse export queued. Activity shows completion or failure.",
      );
      await refresh();
    }, e.currentTarget),
  );
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopStream();
    else if (s.user && s.view === "overview") startStream();
  });
  setInterval(() => {
    const now = new Date();
    $("#clock").textContent = now.toLocaleTimeString(
      s.lang === "de" ? "de-DE" : "en-GB",
    );
    $("#current-date").textContent = now
      .toLocaleDateString(s.lang === "de" ? "de-DE" : "en-GB", {
        weekday: "long",
        day: "numeric",
        month: "long",
        year: "numeric",
      })
      .toUpperCase();
  }, 1000);
  setInterval(() => {
    if (s.user && !document.hidden) act(refresh);
  }, 10000);
  document.documentElement.dataset.theme =
    localStorage.getItem("campix-theme") || "dark";
  s.view = ["overview", "media", "scheduler", "devices", "settings"].includes(
    location.hash.slice(1),
  )
    ? location.hash.slice(1)
    : "overview";
  translate();
  switchView(s.view);
  connect().catch(() => showLogin());
})();
