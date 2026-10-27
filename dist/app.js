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
    recent: [],
    schedules: [],
    events: [],
    view: "overview",
    kind: "",
    offset: 0,
    total: 0,
    libraryTotal: 0,
    query: "",
    searchTimer: null,
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
    liveFullscreen: false,
    fullscreenGeneration: 0,
    liveMode: "single",
    wallGeneration: 0,
    wallIds: "",
    audioGeneration: 0,
    mediaCamera: "",
    mediaFrom: "",
    mediaTo: "",
    picked: new Set(),
    regions: [],
    editingSchedule: "",
    passwordUser: "",
    clearPtzPassword: false,
    desktopNotes: localStorage.getItem("campix-notes") === "1",
    audioPlaying: false,
    audioContext: null,
    notifyDirty: false,
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
    encoder: ["Video-Encoder", "Video encoder"],
    autoEncoder: [
      "Automatisch · Hardware bevorzugen",
      "Automatic · prefer hardware",
    ],
    encoderChecking: ["Encoder wird geprüft …", "Checking encoder …"],
    captureFps: ["Kamera", "Camera"],
    frameAge: ["Alter", "Age"],
    drops: ["Drops", "Drops"],
    liveEncoder: ["Live-Encoder", "Live encoder"],
    preparedEncoder: ["Geprüfter Encoder", "Prepared encoder"],
    fullscreen: ["Vollbild", "Fullscreen"],
    exitFullscreen: ["Vollbild beenden", "Exit fullscreen"],
    fullscreenFallback: [
      "Browser-Vollbild nicht verfügbar. Das Livebild wird im Fenster vergrößert.",
      "Browser fullscreen is unavailable. The live view is expanded in this window.",
    ],
    capture: ["◎ Foto", "◎ Photo"],
    startRecording: ["Aufnehmen", "Record"],
    stopRecording: ["Aufnahme beenden", "Stop recording"],
    controls: ["Kamerasteuerung", "Camera controls"],
    exposure: ["Belichtung", "Exposure"],
    brightness: ["Helligkeit", "Brightness"],
    focus: ["Linsenposition", "Lens position"],
    motion: ["Bewegungserkennung", "Motion detection"],
    singleView: ["Einzelansicht", "Single view"],
    wallView: ["Alle Kameras", "All cameras"],
    wallHint: [
      "Jede Kachel zeigt das aktuelle JPEG. WebRTC bleibt der Einzelansicht vorbehalten.",
      "Each tile shows the current JPEG. WebRTC stays on the single view.",
    ],
    audio: ["Ton", "Audio"],
    audioSource: ["Audioquelle", "Audio source"],
    audioHint: [
      "Ton ist standardmäßig aus und ein eigener Stream. Die Aufnahme wird erst gespeichert, wenn Bild und Ton abgeschlossen sind.",
      "Audio is off by default and uses its own stream. A recording is saved only after picture and audio both finish.",
    ],
    playAudio: ["Ton abspielen", "Play audio"],
    stopAudio: ["Ton aus", "Mute audio"],
    motionMask: ["Ignorierte Zellen", "Ignored cells"],
    motionMaskHint: [
      "Helle Zellen werden ausgewertet. Dunkle Zellen ignoriert die Erkennung, zum Beispiel Bäume oder eine Uhr. Ein langsames Hintergrundmodell gleicht Lichtwechsel aus.",
      "Bright cells are analyzed. Dark cells are ignored, for example trees or a clock. A slow background model absorbs lighting changes.",
    ],
    notifications: ["Benachrichtigungen", "Notifications"],
    webhook: ["Signierter Webhook", "Signed webhook"],
    webhookUrl: ["Webhook-URL", "Webhook URL"],
    localMail: ["Lokale Mail", "Local mail"],
    mailRecipient: ["Empfänger", "Recipient"],
    mailPort: ["SMTP-Port auf 127.0.0.1", "SMTP port on 127.0.0.1"],
    signingSecret: ["Signaturgeheimnis", "Signing secret"],
    revealSecret: ["Geheimnis anzeigen", "Show secret"],
    hideSecret: ["Geheimnis verbergen", "Hide secret"],
    rotateSecret: ["Geheimnis erneuern", "Rotate secret"],
    notificationHint: [
      "Nur Bewegung, ein blockierter Kamerastream, die Speicherreserve und ein fehlgeschlagener Export werden zugestellt. Der Webhook ist HTTPS, außer auf dem Loopback. Mail geht nur an den lokalen SMTP-Dienst. Es gibt keine Befehle und keine Makros.",
      "Only motion, a stalled camera, the storage reserve, and a failed export are delivered. The webhook is HTTPS except on loopback. Mail uses only the local SMTP service. There are no commands and no macros.",
    ],
    noDeliveries: ["Noch keine Zustellungen.", "No deliveries yet."],
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
    weekdays: ["Wochentage", "Weekdays"],
    everyDay: ["täglich", "every day"],
    mon: ["Mo", "Mon"],
    tue: ["Di", "Tue"],
    wed: ["Mi", "Wed"],
    thu: ["Do", "Thu"],
    fri: ["Fr", "Fri"],
    sat: ["Sa", "Sat"],
    sun: ["So", "Sun"],
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
    emptySearch: ["Keine passenden Medien.", "No matching media."],
    search: ["Medien suchen", "Search media"],
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
      "{count} passende Fotos in zeitlicher Reihenfolge als Video exportieren?",
      "Export {count} matching photos, ordered by capture time, as a video?",
    ],
    timelapseTooMany: [
      "Mehr als 500 Fotos passen. Grenze die Suche ein, bevor du exportierst.",
      "More than 500 photos match. Narrow the search before exporting.",
    ],
    needPhotos: [
      "Mindestens zwei Fotos erforderlich.",
      "At least two photos required.",
    ],
    contrast: ["Kontrast", "Contrast"],
    saturation: ["Sättigung", "Saturation"],
    autofocus: ["Autofokus", "Autofocus"],
    fps: ["Bilder pro Sekunde", "Frames per second"],
    preroll: ["Pre-Roll (Sekunden)", "Pre-roll (seconds)"],
    motionThreshold: ["Bewegungsschwelle (0–1)", "Motion threshold (0–1)"],
    timestamp: ["Zeitstempel auf JPEG", "UTC timestamp on JPEG"],
    timestampHint: [
      "Der Zeitstempel liegt auf der JPEG-Vorschau und auf Fotos. Native H.264-Aufnahmen bleiben ohne Einblendung.",
      "The timestamp is burned into the JPEG preview and snapshots. Native H.264 recordings stay unstamped.",
    ],
    viewers: ["Sichtbare Benutzer", "Visible accounts"],
    viewersHint: [
      "Leer lässt jede Rolle sehen.",
      "Empty keeps the library shared.",
    ],
    regions: ["Bewegungszonen", "Motion regions"],
    clearRegions: ["Zonen leeren", "Clear regions"],
    regionsHint: [
      "Ziehe bis zu acht Rechtecke. Ohne Zone gilt das ganze Bild, abzüglich der dunklen Zellen.",
      "Drag up to eight rectangles. With none, the whole frame is scored except ignored cells.",
    ],
    ptz: ["PTZ über ONVIF", "ONVIF PTZ"],
    ptzPort: ["ONVIF-Port", "ONVIF port"],
    ptzUser: ["ONVIF-Benutzer", "ONVIF username"],
    ptzPassword: ["ONVIF-Passwort", "ONVIF password"],
    clearPtzPassword: ["PTZ-Passwort entfernen", "Remove PTZ password"],
    ptzHint: [
      "Der Dienst spricht nur den Host der Kamera-URL an. Ein leeres Passwortfeld lässt das gespeicherte Passwort unverändert.",
      "The service contacts only the host in the camera URL. An empty password leaves the saved one unchanged.",
    ],
    pause: ["Pausieren", "Pause"],
    resume: ["Fortsetzen", "Resume"],
    edit: ["Bearbeiten", "Edit"],
    enableUser: ["Aktivieren", "Enable"],
    disableUser: ["Deaktivieren", "Disable"],
    setPassword: ["Passwort setzen", "Set password"],
    changePassword: ["Passwort ändern", "Change password"],
    currentPassword: ["Aktuelles Passwort", "Current password"],
    newPassword: ["Neues Passwort (12+)", "New password (12+)"],
    allCameras: ["Alle Kameras", "All cameras"],
    fromDate: ["Von", "From"],
    toDate: ["Bis", "Until"],
    eyebrowOverview: ["ÜBERSICHT", "OVERVIEW"],
    eyebrowCamera: ["KAMERA", "CAMERA"],
    eyebrowMedia: ["MEDIENBIBLIOTHEK", "MEDIA LIBRARY"],
    eyebrowAutomation: ["AUTOMATISIERUNG", "AUTOMATION"],
    eyebrowDevices: ["FÄHIGKEITEN", "CAPABILITIES"],
    eyebrowSettings: ["KONFIGURATION", "CONFIGURATION"],
    system: ["System", "System"],
    share: ["Link erzeugen", "Create link"],
    shareReady: [
      "Der Link wird nur einmal angezeigt.",
      "The link is shown only once.",
    ],
    recover: ["Unterbrochene Aufnahmen prüfen", "Check interrupted recordings"],
    integrations: ["Integrationen", "Integrations"],
    onvif: ["ONVIF-Gerät", "ONVIF device"],
    onvifUser: ["ONVIF-Benutzer", "ONVIF username"],
    onvifPassword: ["ONVIF-Passwort (12+)", "ONVIF password (12+)"],
    onvifHint: [
      "ONVIF ist aus, bis Benutzer und Passwort gesetzt sind. PasswordDigest wird abgelehnt. Der RTSP-Pfad ist der verwaltete MediaMTX-Pfad.",
      "ONVIF stays off until a username and password are set. Password digest is rejected. The RTSP path is the managed MediaMTX path.",
    ],
    pair: ["Home Assistant koppeln", "Pair Home Assistant"],
    desktopNotes: ["Browser-Hinweise", "Browser notifications"],
    transfer: ["Sicherung und Import", "Backup and import"],
    backup: ["Datenbank sichern", "Download database backup"],
    importConfig: ["Konfiguration importieren", "Import configuration"],
    readLegacy: ["Raspimjpeg lesen", "Read raspimjpeg"],
    backupHint: [
      "Die Sicherung enthält Passwort-Hashes und keine Sitzungen. Medien bleiben ein eigener Ordner. Ein Raspimjpeg-Import übernimmt nur Größe, Rate und Bewegung und speichert noch nichts.",
      "The backup contains password hashes and no sessions. Media stays a separate folder. A raspimjpeg import reads size, rate, and motion only, and does not save yet.",
    ],
    notificationKinds: ["Ereignisse", "Events"],
    kindMotion: ["Bewegung", "Motion"],
    kindStalled: ["Stillstand", "Stall"],
    kindStorage: ["Speicher", "Storage"],
    kindExport: ["Export", "Export"],
    notificationCameras: ["Kameras", "Cameras"],
    startMinute: ["Startminute", "Start minute"],
    endMinute: ["Endminute", "End minute"],
    nativeSizes: ["Native Größen", "Native sizes"],
    encoderFallback: ["Encoder-Rückfall", "Encoder fallback"],
    liveEncoderFallback: ["Live-Encoder-Rückfall", "Live encoder fallback"],
    captureProcessing: ["Aufnahmeverarbeitung (ms)", "Capture processing (ms)"],
    skippedFrames: ["Übersprungene Bilder", "Skipped capture frames"],
    testStatus: ["Teststatus", "Test status"],
    motionMaskLabel: ["Bewegungsmaske", "Motion mask"],
    ignoredCells: ["ignorierte Zellen", "ignored cells"],
    droppedRecording: ["Verworfene Aufnahmeframes", "Dropped recording frames"],
    controlsLabel: ["Regler", "Controls"],
    revoke: ["Widerrufen", "Revoke"],
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
    const camera = active();
    $("#record-button").disabled =
      !operate() ||
      camera?.status !== "online" ||
      (!camera.recording &&
        (camera.encoding.preparing ||
          !camera.encoding.selected ||
          !camera.encoding.packet_ready));
  }
  function translate() {
    document.documentElement.lang = s.lang;
    $$("[data-i18n]").forEach((e) => {
      e.textContent = t(e.dataset.i18n);
    });
    $("#language-button").textContent = s.lang === "de" ? "EN" : "DE";
    $("#media-search").placeholder = t("search");
    $("#media-search").setAttribute("aria-label", t("search"));
    $("#breadcrumb-current").textContent = t(s.view);
    syncFullscreen();
    if (s.user) render();
  }
  function syncFullscreen() {
    const fullscreen =
      document.fullscreenElement === $("#live-stage") ||
      document.documentElement.classList.contains("live-expanded");
    const entry = $("#fullscreen-button");
    const exit = $("#fullscreen-exit");
    entry.setAttribute("aria-expanded", String(fullscreen));
    entry.title = t("fullscreen");
    exit.title = t("exitFullscreen");
    exit.hidden = !fullscreen;
    if (fullscreen === s.liveFullscreen) return;
    s.liveFullscreen = fullscreen;
    if (fullscreen) exit.focus({ preventScroll: true });
    else if (s.user && s.view === "overview")
      entry.focus({ preventScroll: true });
  }
  async function exitLiveFullscreen() {
    s.fullscreenGeneration++;
    document.documentElement.classList.remove("live-expanded");
    if (document.fullscreenElement === $("#live-stage"))
      await document.exitFullscreen();
    syncFullscreen();
  }
  async function enterLiveFullscreen() {
    const generation = ++s.fullscreenGeneration;
    const stage = $("#live-stage");
    let entered = false;
    if (stage.requestFullscreen && document.fullscreenEnabled !== false) {
      try {
        await stage.requestFullscreen();
        entered = true;
      } catch {
        // A denied request still permits an expanded view within the application.
      }
    }
    if (generation !== s.fullscreenGeneration) {
      if (entered) await exitLiveFullscreen();
      return;
    }
    if (entered) return;
    document.documentElement.classList.add("live-expanded");
    syncFullscreen();
    toast(t("fullscreenFallback"));
  }
  function switchView(view) {
    if (
      !["overview", "media", "scheduler", "devices", "settings"].includes(view)
    )
      return;
    s.view = view;
    if (view !== "overview") act(exitLiveFullscreen);
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
    if (view === "overview" && !document.hidden) resumeLive();
    else {
      stopWall();
      stopStream();
    }
    if (s.user) act(refresh);
  }
  function showLogin() {
    s.user = null;
    act(exitLiveFullscreen);
    s.refreshGeneration++;
    s.cameraDirty = s.storageDirty = s.dirtyControls = false;
    $("#user-form").reset();
    $("#camera-form").reset();
    $("#workspace").hidden = true;
    $("#login-screen").hidden = false;
    if (s.socket) s.socket.close();
    stopStream();
    stopWall();
    showSingleChrome();
    s.liveMode = "single";
    s.csrf = "";
    clearTimeout(s.searchTimer);
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
    resumeLive();
  }
  async function refresh() {
    if (!s.user) return;
    const generation = ++s.refreshGeneration;
    const view = s.view;
    const jobs = {
      cameras: api("/cameras"),
      system: api("/system"),
    };
    if (view === "overview" || view === "media")
      jobs.recent = api(mediaPath({ limit: 3, offset: 0 }));
    if (view === "media")
      jobs.media = api(
        mediaPath({
          limit: 24,
          offset: s.offset,
          kind: s.kind,
          query: s.query,
        }),
      );
    if (view === "scheduler") jobs.schedules = api("/schedules");
    if (view === "overview") jobs.events = api("/events?limit=10");
    const names = Object.keys(jobs);
    const results = await Promise.all(names.map((name) => jobs[name]));
    if (!s.user || generation !== s.refreshGeneration) return;
    const data = Object.fromEntries(
      names.map((name, index) => [name, results[index]]),
    );
    s.cameras = data.cameras;
    s.system = data.system;
    if (data.media) {
      s.media = data.media.items;
      s.total = data.media.total;
    }
    if (data.recent) {
      s.recent = data.recent.items;
      s.libraryTotal = data.recent.total;
    }
    if (data.schedules) s.schedules = data.schedules;
    if (data.events) s.events = data.events;
    if (!data.cameras.some((c) => c.id === s.selected)) {
      s.selected = data.cameras[0]?.id || "";
      s.cameraDirty = s.dirtyControls = false;
    }
    render();
    if (s.liveMode !== "wall" && active()?.status !== "online") {
      stopStream();
      $("#live-empty").hidden = false;
    }
    syncWall();
    if (admin() && s.view === "settings") {
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
      const notifications = await api("/notifications");
      if (!s.user || generation !== s.refreshGeneration) return;
      if (
        !s.notifyDirty &&
        !$("#notification-settings-form").contains(document.activeElement)
      ) {
        $("#setting-webhook").checked = !!notifications.webhook?.enabled;
        $("#setting-webhook-url").value = notifications.webhook?.url || "";
        $("#setting-mail").checked = !!notifications.mail?.enabled;
        $("#setting-mail-recipient").value =
          notifications.mail?.recipient || "";
        $("#setting-mail-port").value = notifications.mail?.port || 25;
        $("#setting-webhook-secret").value = notifications.secret || "";
        const selectedKinds = notifications.kinds;
        $$("#notification-kinds input").forEach((input) => {
          input.checked = Array.isArray(selectedKinds)
            ? selectedKinds.includes(input.value)
            : true;
        });
        $("#setting-notification-cameras").value = (
          notifications.cameras || []
        ).join(", ");
        renderDeliveries(notifications.deliveries);
        try {
          await loadIntegrations();
        } catch (error) {
          toast(error.message, true);
        }
        if (!s.audioListed) {
          s.audioListed = true;
          try {
            const audio = await api("/discovery/audio");
            $("#audio-devices").replaceChildren(
              ...(audio.devices || []).map((device) => {
                const option = make("option");
                option.value = device.source;
                option.label = device.name;
                return option;
              }),
            );
          } catch (error) {
            toast(error.message, true);
          }
        }
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
    $("#nav-media-count").textContent = s.libraryTotal;
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
    const publisher = s.system?.media_plane.publishers?.[c?.id];
    $("#active-camera-name").textContent = c?.name || t("camera");
    $("#camera-state").textContent = c
      ? `${c.status}${c.error ? " · " + c.error : ""}`
      : "—";
    $("#actual-fps").textContent = c
      ? `${t("captureFps")} ${c.fps} / ${c.fps_target} FPS`
      : "—";
    $("#actual-age").textContent = c
      ? `${t("frameAge")} ${c.frame_age_seconds ?? "—"} s`
      : "";
    $("#actual-drops").textContent = c
      ? `${t("drops")} ${c.capture_dropped_frames} / ${c.dropped_frames}`
      : "";
    $("#actual-fps").title = publisher?.encoder
      ? `${t("liveEncoder")}: ${publisher.encoder}${publisher.fallback_reason ? " · " + publisher.fallback_reason : ""}`
      : c?.encoding.selected
        ? `${t("preparedEncoder")}: ${c.encoding.selected}${c.encoding.fallback_reason ? " · " + c.encoding.fallback_reason : ""}`
        : c?.encoding.error || t("encoderChecking");
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
        ["contrast", "contrast-range"],
        ["saturation", "saturation-range"],
        ["lens_position", "focus-range"],
      ])
        $("#" + id).value =
          c?.controls[key] ??
          (key === "contrast" || key === "saturation" ? 1 : 0);
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
      const encoders = [
        ...new Set([
          "auto",
          "libx264",
          ...(c.encoding.encoders || []),
          c.encoding.requested || "auto",
        ]),
      ];
      $("#setting-encoder").replaceChildren(
        ...encoders.map((codec) => {
          const option = make(
            "option",
            "",
            codec === "auto" ? t("autoEncoder") : codec,
          );
          option.value = codec;
          return option;
        }),
      );
      $("#setting-encoder").value = c.encoding.requested || "auto";
      $("#setting-preroll").value = c.preroll_seconds;
      $("#setting-motion").checked = c.motion_enabled;
      $("#setting-motion-threshold").value = c.motion_threshold;
      $("#setting-audio").checked = !!c.audio_enabled;
      $("#setting-audio-source").value = c.audio_source || "";
      $("#setting-timestamp").checked = !!c.timestamp_overlay;
      $("#setting-viewers").value = (c.viewer_users || []).join(", ");
      $("#setting-ptz").checked = !!c.onvif_ptz;
      $("#setting-ptz-port").value = c.onvif_port || 80;
      $("#setting-ptz-user").value = c.onvif_username || "";
      $("#setting-ptz-password").value = "";
      s.clearPtzPassword = false;
      $("#ptz-settings").hidden = c.backend !== "network";
      s.regions = (c.motion_regions || []).map((region) => ({ ...region }));
      renderRegions();
      renderMask(c.motion_mask || "");
      const needsSource = [
        "dshow",
        "v4l2",
        "picamera2",
        "avfoundation",
      ].includes(c.backend);
      $("#audio-source-field").hidden = !needsSource;
      $("#setting-audio").disabled = ["legacy", "gstreamer"].includes(
        c.backend,
      );
    }
    updateAudioButton();
  }
  function outputs() {
    $("#exposure-output").textContent =
      `${Number($("#exposure-range").value).toFixed(1)} EV`;
    $("#brightness-output").textContent = Number(
      $("#brightness-range").value,
    ).toFixed(2);
    $("#contrast-output").textContent = Number(
      $("#contrast-range").value,
    ).toFixed(2);
    $("#saturation-output").textContent = Number(
      $("#saturation-range").value,
    ).toFixed(2);
    $("#focus-output").textContent = Number($("#focus-range").value).toFixed(1);
    drawMotion();
    const pad = $("#ptz-pad");
    const camera = active();
    if (pad)
      pad.hidden = !(camera && camera.onvif_ptz && s.liveMode === "single");
  }
  let previewQueue = Promise.resolve();

  function enqueuePreview(task) {
    const run = previewQueue.then(task);
    // Keep the chain alive after a cancelled frame so the next fetch can start,
    // but only after this request has left the network.
    previewQueue = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
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
    stopAudio();
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
    const camera = s.selected;
    const preview = {
      generation: gen,
      camera,
      request: null,
      timer: null,
      url: null,
      pendingUrl: null,
    };
    // Claim this generation before clearing the video. That clear fires an
    // error event in some browsers, and the handler must not start a second fetch.
    s.preview = preview;
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
        // One preview response is consumed before another request is allowed
        // onto the network, including after a cancelled frame.
        const loaded = await enqueuePreview(async () => {
          if (!current() || controller.signal.aborted) return null;
          const response = await fetch(
            `/api/v1/cameras/${encodeURIComponent(camera)}/preview.jpg?view=live`,
            {
              credentials: "same-origin",
              cache: "no-store",
              signal: controller.signal,
            },
          );
          const blob = await response.blob();
          return { response, blob };
        });
        if (!current() || !loaded) return;
        if (loaded.response.status === 401) {
          showLogin();
          return;
        }
        if (!loaded.response.ok) throw new Error("Live frame unavailable");
        url = URL.createObjectURL(loaded.blob);
        preview.pendingUrl = url;
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
            1000 / Math.max(1, Math.min(60, active().fps_target));
          preview.timer = setTimeout(
            next,
            ready ? Math.max(0, interval - (performance.now() - started)) : 500,
          );
        }
      }
    }
    next();
  }
  function showSingleChrome() {
    $("#camera-wall").hidden = true;
    $("#wall-note").hidden = true;
    $("#live-stage").hidden = false;
    $("#transport-select").hidden = false;
    $("#fullscreen-button").hidden = false;
    $("#live-single").setAttribute("aria-pressed", "true");
    $("#live-wall").setAttribute("aria-pressed", "false");
  }
  function showWallChrome() {
    $("#camera-wall").hidden = false;
    $("#wall-note").hidden = false;
    $("#live-stage").hidden = true;
    $("#transport-select").hidden = true;
    $("#fullscreen-button").hidden = true;
    $("#live-badge").hidden = true;
    $("#live-single").setAttribute("aria-pressed", "false");
    $("#live-wall").setAttribute("aria-pressed", "true");
  }
  function resumeLive() {
    if (s.liveMode === "wall") startWall();
    else startStream();
  }
  function updateAudioButton() {
    const button = $("#audio-toggle");
    const enabled = !!active()?.audio_enabled && s.liveMode === "single";
    button.hidden = !enabled;
    button.textContent = t(s.audioPlaying ? "stopAudio" : "playAudio");
    button.setAttribute("aria-pressed", s.audioPlaying ? "true" : "false");
  }
  function stopAudio() {
    s.audioGeneration++;
    s.audioPlaying = false;
    clearTimeout(s.audioTimer);
    if (s.audioController) s.audioController.abort();
    s.audioController = null;
    if (s.audioContext) {
      s.audioContext.close().catch(() => {});
      s.audioContext = null;
    }
    if ($("#audio-toggle")) updateAudioButton();
  }
  function startAudio() {
    const camera = active();
    if (!camera?.audio_enabled || s.liveMode !== "single" || !s.user) return;
    stopAudio();
    const gen = s.audioGeneration;
    const context = new AudioContext();
    s.audioContext = context;
    s.audioPlaying = true;
    updateAudioButton();
    let after = 0;
    let nextTime = 0;
    const pump = async () => {
      if (gen !== s.audioGeneration || !s.user) return;
      const controller = new AbortController();
      s.audioController = controller;
      const timeout = setTimeout(() => controller.abort(), 2500);
      try {
        const response = await fetch(
          `/api/v1/cameras/${encodeURIComponent(s.selected)}/audio.wav?after=${after}`,
          {
            credentials: "same-origin",
            cache: "no-store",
            signal: controller.signal,
          },
        );
        if (response.status === 401) {
          showLogin();
          return;
        }
        if (!response.ok) throw new Error("audio");
        const sequence = Number(response.headers.get("X-Audio-Sequence"));
        const payload = await response.arrayBuffer();
        if (gen !== s.audioGeneration) return;
        const decoded = await context.decodeAudioData(payload.slice(0));
        const node = context.createBufferSource();
        node.buffer = decoded;
        node.connect(context.destination);
        const startAt = Math.max(context.currentTime + 0.05, nextTime);
        node.start(startAt);
        nextTime = startAt + decoded.duration;
        after = Number.isFinite(sequence) ? sequence : after;
      } catch (error) {
        if (gen !== s.audioGeneration) return;
        if (error?.name === "AbortError") return;
        nextTime = 0;
      } finally {
        clearTimeout(timeout);
        if (gen === s.audioGeneration && s.audioPlaying) {
          const delay = Math.max(
            40,
            (nextTime - context.currentTime) * 1000 - 80,
          );
          s.audioTimer = setTimeout(pump, Number.isFinite(delay) ? delay : 200);
        }
      }
    };
    pump();
  }
  function stopWall() {
    s.wallGeneration++;
    for (const tile of s.wallTiles || []) {
      tile.controller?.abort();
      if (tile.url) URL.revokeObjectURL(tile.url);
    }
    s.wallTiles = [];
    s.wallIds = "";
  }
  function startWall() {
    stopStream();
    stopWall();
    showWallChrome();
    const gen = s.wallGeneration;
    const wall = $("#camera-wall");
    s.wallIds = s.cameras.map((camera) => camera.id).join("\n");
    s.wallTiles = s.cameras.map((camera) => {
      const button = make("button", "wall-tile");
      button.type = "button";
      button.dataset.camera = camera.id;
      const image = make("img");
      image.alt = `${camera.name} live preview`;
      const caption = make("span", "wall-caption");
      caption.append(
        make("strong", "", camera.name),
        make("small", "", camera.status),
      );
      button.append(image, caption);
      button.addEventListener("click", () => {
        s.selected = camera.id;
        s.liveMode = "single";
        s.cameraDirty = s.dirtyControls = false;
        $("#camera-select").value = camera.id;
        stopWall();
        showSingleChrome();
        renderCamera();
        startStream();
      });
      return { id: camera.id, button, image, controller: null, url: "" };
    });
    wall.replaceChildren(
      ...(s.wallTiles.length
        ? s.wallTiles.map((tile) => tile.button)
        : [make("p", "empty-message", t("noCamera"))]),
    );
    for (const tile of s.wallTiles) pumpWall(gen, tile);
    updateAudioButton();
  }
  async function pumpWall(gen, tile) {
    while (
      gen === s.wallGeneration &&
      s.user &&
      s.view === "overview" &&
      s.liveMode === "wall" &&
      !document.hidden
    ) {
      const started = performance.now();
      const controller = new AbortController();
      tile.controller = controller;
      const timeout = setTimeout(() => controller.abort(), 2500);
      let nextUrl = "";
      try {
        const response = await fetch(
          `/api/v1/cameras/${encodeURIComponent(tile.id)}/preview.jpg?view=live`,
          {
            credentials: "same-origin",
            cache: "no-store",
            signal: controller.signal,
          },
        );
        if (gen !== s.wallGeneration) return;
        if (response.status === 401) {
          showLogin();
          return;
        }
        if (!response.ok) throw new Error("preview");
        const blob = await response.blob();
        if (gen !== s.wallGeneration) return;
        nextUrl = URL.createObjectURL(blob);
        tile.image.src = nextUrl;
        await tile.image.decode();
        if (gen !== s.wallGeneration) return;
        if (tile.url) URL.revokeObjectURL(tile.url);
        tile.url = nextUrl;
        nextUrl = "";
      } catch (error) {
        if (gen !== s.wallGeneration || error?.name === "AbortError") return;
      } finally {
        clearTimeout(timeout);
        if (nextUrl) URL.revokeObjectURL(nextUrl);
        tile.controller = null;
      }
      if (gen !== s.wallGeneration) return;
      const camera = s.cameras.find((item) => item.id === tile.id);
      const interval =
        1000 / Math.max(1, Math.min(15, camera?.fps_target || 5));
      await new Promise((resolve) =>
        setTimeout(
          resolve,
          Math.max(0, interval - (performance.now() - started)),
        ),
      );
    }
  }
  function syncWall() {
    if (s.liveMode !== "wall" || s.view !== "overview") return;
    const ids = s.cameras.map((camera) => camera.id).join("\n");
    if (ids !== s.wallIds) {
      startWall();
      return;
    }
    for (const tile of s.wallTiles || []) {
      const camera = s.cameras.find((item) => item.id === tile.id);
      const status = tile.button.querySelector("small");
      if (camera && status) status.textContent = camera.status;
    }
  }
  function renderMask(value) {
    const mask = $("#motion-mask");
    mask.setAttribute("aria-label", t("motionMask"));
    const cells = value && value.length === 144 ? value : "0".repeat(144);
    mask.replaceChildren(
      ...[...cells].map((cell, index) => {
        const cellButton = make("div", "mask-cell");
        cellButton.role = "button";
        cellButton.tabIndex = 0;
        cellButton.setAttribute(
          "aria-pressed",
          cell === "1" ? "true" : "false",
        );
        const toggle = () => {
          const ignored = cellButton.getAttribute("aria-pressed") === "true";
          cellButton.setAttribute("aria-pressed", ignored ? "false" : "true");
          s.cameraDirty = true;
        };
        cellButton.addEventListener("click", toggle);
        cellButton.addEventListener("keydown", (event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          toggle();
        });
        cellButton.dataset.index = String(index);
        return cellButton;
      }),
    );
  }
  function maskValue() {
    const cells = [...$("#motion-mask").children].map((button) =>
      button.getAttribute("aria-pressed") === "true" ? "1" : "0",
    );
    return cells.includes("1") ? cells.join("") : "";
  }
  function renderRegions() {
    const stage = $("#region-stage");
    if (!stage) return;
    stage.replaceChildren(
      ...s.regions.map((region) => {
        const box = make("div", "region-box");
        box.style.left = `${region.x * 100}%`;
        box.style.top = `${region.y * 100}%`;
        box.style.width = `${region.w * 100}%`;
        box.style.height = `${region.h * 100}%`;
        return box;
      }),
    );
  }
  function drawMotion() {
    const canvas = $("#motion-overlay");
    const camera = active();
    const image = $("#live-image");
    if (
      !canvas ||
      !camera?.motion_enabled ||
      s.liveMode !== "single" ||
      image.hidden
    ) {
      if (canvas) canvas.hidden = true;
      return;
    }
    const stage = $("#live-stage");
    const width = stage.clientWidth;
    const height = stage.clientHeight;
    canvas.width = width;
    canvas.height = height;
    canvas.hidden = false;
    const context = canvas.getContext("2d");
    context.clearRect(0, 0, width, height);
    const naturalWidth = image.naturalWidth || camera.width || 16;
    const naturalHeight = image.naturalHeight || camera.height || 9;
    const scale = Math.min(width / naturalWidth, height / naturalHeight);
    const boxWidth = naturalWidth * scale;
    const boxHeight = naturalHeight * scale;
    const left = (width - boxWidth) / 2;
    const top = (height - boxHeight) / 2;
    const cells = camera.motion_cells || "";
    if (cells.length === 144) {
      context.fillStyle = "rgba(251, 23, 143, 0.28)";
      for (let index = 0; index < 144; index += 1) {
        if (cells[index] !== "1") continue;
        const column = index % 16;
        const row = Math.floor(index / 16);
        context.fillRect(
          left + (column * boxWidth) / 16,
          top + (row * boxHeight) / 9,
          boxWidth / 16,
          boxHeight / 9,
        );
      }
    }
    context.strokeStyle = "#07dff1";
    context.lineWidth = 2;
    for (const region of camera.motion_regions || []) {
      context.strokeRect(
        left + region.x * boxWidth,
        top + region.y * boxHeight,
        region.w * boxWidth,
        region.h * boxHeight,
      );
    }
  }
  function renderTokens(tokens) {
    $("#token-list").replaceChildren(
      ...(tokens || []).map((token) => {
        const item = make("li", "");
        item.append(make("span", "", `${token.name} · ${token.id}`));
        const revoke = make("button", "button ghost", t("revoke"));
        revoke.type = "button";
        revoke.addEventListener("click", () =>
          act(async () => {
            await api(`/integrations/tokens/${token.id}`, "DELETE");
            await refresh();
          }, revoke),
        );
        item.append(revoke);
        return item;
      }),
    );
  }
  async function loadIntegrations() {
    const [onvif, tokens] = await Promise.all([
      api("/integrations/onvif"),
      api("/integrations/tokens"),
    ]);
    if (!$("#integration-form").contains(document.activeElement)) {
      $("#setting-onvif").checked = !!onvif.enabled;
      $("#setting-onvif-user").value = onvif.username || "";
    }
    renderTokens(tokens);
  }
  function renderDeliveries(items) {
    const list = $("#notification-deliveries");
    if (!items?.length) {
      list.replaceChildren(make("li", "", t("noDeliveries")));
      return;
    }
    list.replaceChildren(
      ...items.map((item) =>
        make(
          "li",
          "",
          `${item.kind} · ${item.channel} · ${item.status}${item.last_error ? " · " + item.last_error : ""}`,
        ),
      ),
    );
  }
  async function startStream() {
    const transport = $("#transport-select").value;
    if (
      s.user &&
      s.view === "overview" &&
      s.liveMode === "single" &&
      !document.hidden &&
      active()?.status === "online" &&
      transport === "mjpeg" &&
      s.preview?.camera === s.selected &&
      s.preview.generation === s.streamGeneration
    )
      return;
    stopWall();
    showSingleChrome();
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
    if (!compact && operate()) {
      const box = make("input", "gallery-check");
      box.type = "checkbox";
      box.checked = s.picked.has(item.id);
      box.addEventListener("click", (event) => event.stopPropagation());
      box.addEventListener("change", () => {
        if (box.checked) s.picked.add(item.id);
        else s.picked.delete(item.id);
        $("#media-bulk").hidden = s.picked.size === 0;
      });
      card.append(box);
    }
    card.addEventListener("click", () => openMedia(item));
    return card;
  }
  function mediaPath({ limit, offset, kind = "", query = "" }) {
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    });
    if (kind) params.set("kind", kind);
    if (s.mediaCamera) params.set("camera_id", s.mediaCamera);
    if (s.mediaFrom)
      params.set("created_after", String(Date.parse(s.mediaFrom) / 1000));
    if (s.mediaTo) {
      const end = Date.parse(s.mediaTo);
      if (Number.isFinite(end))
        params.set("created_before", String(end / 1000 + 86399));
    }
    const text = query.trim();
    if (text) params.set("q", text);
    return "/media?" + params;
  }
  function renderMedia() {
    $("#media-grid").replaceChildren(...s.media.map((i) => mediaCard(i)));
    if (!s.media.length)
      $("#media-grid").append(
        make("p", "empty-message", s.query ? t("emptySearch") : t("empty")),
      );
    $("#recent-media").replaceChildren(
      ...s.recent.map((i) => mediaCard(i, true)),
    );
    if (!s.recent.length)
      $("#recent-media").append(make("p", "empty-message", t("empty")));
    $("#media-bulk").hidden = s.picked.size === 0;
    const cameraFilter = $("#media-camera");
    const current = cameraFilter.value;
    cameraFilter.replaceChildren(new Option(t("allCameras"), ""));
    for (const camera of s.cameras)
      cameraFilter.append(new Option(camera.name, camera.id));
    cameraFilter.value = s.cameras.some((camera) => camera.id === current)
      ? current
      : "";
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
      const share = make("button", "button ghost", t("share"));
      share.addEventListener("click", () =>
        act(async () => {
          const link = await api(`/media/${item.id}/share`, "POST", {
            hours: 24,
          });
          toast(`${t("shareReady")} ${location.origin}${link.path}`);
        }, share),
      );
      actions.push(lock, remove, share);
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
        const camera = s.cameras.find((item) => item.id === job.camera_id);
        const text = make("div");
        text.append(
          make("h3", "", job.name),
          make(
            "p",
            "subhead",
            `${camera?.name || job.camera_id} · ${job.action} · ${job.interval_seconds}s · ${scheduleDays(job)} · ${clock(job.start_hour, job.start_minute)}–${clock(job.end_hour, job.end_minute)} · ${job.timezone}`,
          ),
          make("small", "", job.enabled ? date(job.next_run) : t("disabled")),
        );
        card.append(text);
        if (admin()) {
          const edit = make("button", "button ghost", t("edit"));
          edit.addEventListener("click", () => openSchedule(job));
          const toggle = make(
            "button",
            "button soft",
            t(job.enabled ? "pause" : "resume"),
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
          card.append(edit, toggle, remove);
        }
        return card;
      }),
    );
    if (!s.schedules.length)
      $("#schedule-list").append(make("p", "empty-message", t("empty")));
  }
  function scheduleDays(job) {
    const names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
    const days =
      Array.isArray(job.days) && job.days.length
        ? [...new Set(job.days)].sort((a, b) => a - b)
        : [0, 1, 2, 3, 4, 5, 6];
    if (days.length === 7) return t("everyDay");
    return days
      .filter((day) => names[day])
      .map((day) => t(names[day]))
      .join(" ");
  }
  function clock(hour, minute) {
    return `${String(hour).padStart(2, "0")}:${String(minute || 0).padStart(2, "0")}`;
  }
  function openSchedule(job) {
    if (!s.cameras.length) return toast(t("noCamera"), true);
    options($("#schedule-camera"));
    const form = $("#schedule-form");
    s.editingSchedule = job?.id || "";
    form.elements.name.value = job?.name || "";
    form.elements.camera_id.value = job?.camera_id || s.selected;
    form.elements.action.value = job?.action || "snapshot";
    form.elements.interval_seconds.value = job?.interval_seconds || 3600;
    form.elements.duration_seconds.value = job?.duration_seconds || 30;
    form.elements.start_hour.value = job?.start_hour ?? 0;
    form.elements.start_minute.value = job?.start_minute ?? 0;
    form.elements.end_hour.value = job?.end_hour ?? 24;
    form.elements.end_minute.value = job?.end_minute ?? 0;
    form.elements.timezone.value = job?.timezone || "Europe/Berlin";
    const days = new Set(job?.days || [0, 1, 2, 3, 4, 5, 6]);
    $$("#schedule-form input[name=days]").forEach((input) => {
      input.checked = days.has(Number(input.value));
    });
    $("#schedule-dialog").showModal();
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
          [t("controlsLabel"), c.capabilities.controls.join(", ") || "—"],
          ["Hardware verified", c.encoding.hardware_verified ? "yes" : "no"],
          ["Encoders", c.encoding.encoders.join(", ") || "—"],
          [
            "Selected encoder",
            c.encoding.selected || c.encoding.error || t("encoderChecking"),
          ],
          ["Hardware encoding", c.encoding.selected_hardware ? "yes" : "no"],
          ["Profile probe passed", c.encoding.profile_tested ? "yes" : "no"],
          [
            "Shared stream / recording encoder",
            c.encoding.shared ? "yes" : "no",
          ],
          ["H.264 packets ready", c.encoding.packet_ready ? "yes" : "no"],
          ["Encoded source", c.encoding.source || "—"],
          [
            "Raw-frame zero-copy",
            c.encoding.zero_copy_raw_frames ? "yes" : "no",
          ],
          ["Zero-copy scope", c.encoding.zero_copy_scope || "—"],
          ["Zero-copy fallback", c.encoding.zero_copy_reason || "—"],
          [t("nativeSizes"), (c.capabilities.modes || []).join(", ") || "—"],
          [t("encoderFallback"), c.encoding.native_fallback_reason || "—"],
          [
            "Encoder sessions started",
            c.encoding.encoder_sessions_started ?? "—",
          ],
          ["Preview size", c.preview_size?.join("×") || "—"],
          ["Preview FPS", c.preview_fps ?? "—"],
          [t("encoderFallback"), c.encoding.fallback_reason || "—"],
          [
            t("liveEncoder"),
            s.system?.media_plane.publishers?.[c.id]?.encoder || "—",
          ],
          [
            t("liveEncoderFallback"),
            s.system?.media_plane.publishers?.[c.id]?.fallback_reason || "—",
          ],
          [t("captureProcessing"), c.capture_processing_ms ?? "—"],
          [t("skippedFrames"), c.capture_dropped_frames],
          [t("testStatus"), c.capabilities.test_status || "unverified"],
          ["Motion", t(c.motion_enabled ? "enabled" : "disabled")],
          [t("motionMaskLabel"), c.motion_mask ? t("ignoredCells") : "—"],
          [
            "Audio",
            c.audio_enabled ? c.audio_status || t("enabled") : t("disabled"),
          ],
          [t("droppedRecording"), c.dropped_frames],
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
              resumeLive();
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
          t(u.disabled ? "enableUser" : "disableUser"),
        );
        toggle.addEventListener("click", () =>
          act(async () => {
            await api(`/users/${u.id}`, "PATCH", { disabled: !u.disabled });
            await refresh();
          }, toggle),
        );
        const password = make("button", "button ghost", t("setPassword"));
        password.addEventListener("click", () => {
          s.passwordUser = u.id;
          $("#user-password-form").reset();
          $("#user-password-dialog").showModal();
        });
        row.append(roles, toggle, password);
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
      if (
        s.desktopNotes &&
        typeof Notification === "function" &&
        Notification.permission === "granted" &&
        [
          "motion.detected",
          "camera.stalled",
          "storage.reserve",
          "export.failed",
        ].includes(message.kind)
      )
        new Notification("CamPiX", { body: message.message || message.kind });
      clearTimeout(s.refreshTimer);
      s.refreshTimer = setTimeout(
        () =>
          act(async () => {
            await refresh();
            if (
              s.liveMode === "single" &&
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
      if (s.view === "overview") resumeLive();
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
    updateAudioButton();
    $("#motion-mask").setAttribute("aria-label", t("motionMask"));
    $("#live-layout-toggle")?.setAttribute("aria-label", t("singleView"));
  });
  $("#camera-select").addEventListener("change", (e) => {
    s.selected = e.target.value;
    s.cameraDirty = s.dirtyControls = false;
    renderCamera();
    permissions();
    if (s.liveMode === "single") startStream();
  });
  $("#live-single").addEventListener("click", () => {
    if (s.liveMode === "single") return;
    s.liveMode = "single";
    startStream();
  });
  $("#live-wall").addEventListener("click", () =>
    act(async () => {
      if (s.liveFullscreen) await exitLiveFullscreen();
      s.liveMode = "wall";
      startWall();
    }),
  );
  $("#audio-toggle").addEventListener("click", () => {
    if (s.audioPlaying) stopAudio();
    else startAudio();
  });
  $("#transport-select").addEventListener("change", startStream);
  $("#live-image").addEventListener("error", () => {
    if (!s.preview) return;
    $("#live-badge").hidden = true;
    liveMessage("liveUnavailable");
  });
  $("#live-video").addEventListener("error", () => {
    if (s.preview || $("#transport-select").value === "mjpeg") return;
    jpegPreview(s.streamGeneration);
  });
  $("#fullscreen-button").addEventListener("click", (event) =>
    act(
      s.liveFullscreen ? exitLiveFullscreen : enterLiveFullscreen,
      event.currentTarget,
    ),
  );
  $("#fullscreen-exit").addEventListener("click", () =>
    act(exitLiveFullscreen),
  );
  document.addEventListener("fullscreenchange", syncFullscreen);
  document.addEventListener("keydown", (event) => {
    if (!s.liveFullscreen) return;
    if (event.key === "Escape") {
      event.preventDefault();
      act(exitLiveFullscreen);
    } else if (
      event.key === "Tab" &&
      document.documentElement.classList.contains("live-expanded")
    ) {
      event.preventDefault();
      $("#fullscreen-exit").focus({ preventScroll: true });
    }
  });
  for (const id of [
    "exposure-range",
    "brightness-range",
    "contrast-range",
    "saturation-range",
    "focus-range",
  ])
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
        ["contrast", Number($("#contrast-range").value)],
        ["saturation", Number($("#saturation-range").value)],
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
      resumeLive();
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
  $("#new-schedule").addEventListener("click", () => openSchedule(null));
  $("#schedule-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const payload = Object.fromEntries(new FormData(form));
    delete payload.days;
    payload.days = [...form.querySelectorAll("input[name=days]:checked")].map(
      (input) => Number(input.value),
    );
    for (const key of [
      "interval_seconds",
      "duration_seconds",
      "start_hour",
      "start_minute",
      "end_hour",
      "end_minute",
    ])
      payload[key] = Number(payload[key]);
    act(async () => {
      await api("/schedules", "POST", {
        ...payload,
        id: s.editingSchedule || "job-" + crypto.randomUUID().slice(0, 8),
      });
      s.editingSchedule = "";
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
  $("#notification-settings-form").addEventListener("input", () => {
    s.notifyDirty = true;
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
      encoder: $("#setting-encoder").value,
      preroll_seconds: Number($("#setting-preroll").value),
      motion_enabled: $("#setting-motion").checked,
      motion_threshold: Number($("#setting-motion-threshold").value),
      motion_mask: maskValue(),
      motion_regions: s.regions,
      timestamp_overlay: $("#setting-timestamp").checked,
      viewer_users: $("#setting-viewers")
        .value.split(",")
        .map((name) => name.trim())
        .filter(Boolean),
      audio_enabled: $("#setting-audio").checked,
      audio_source: ["dshow", "v4l2", "picamera2", "avfoundation"].includes(
        active()?.backend,
      )
        ? $("#setting-audio-source").value
        : "",
    };
    if (active()?.backend === "network") {
      payload.onvif_ptz = $("#setting-ptz").checked;
      payload.onvif_port = Number($("#setting-ptz-port").value);
      payload.onvif_username = $("#setting-ptz-user").value.trim();
      if (s.clearPtzPassword) payload.onvif_password = "";
      else if ($("#setting-ptz-password").value)
        payload.onvif_password = $("#setting-ptz-password").value;
    }
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
  $("#notification-settings-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const payload = {
      webhook: {
        enabled: $("#setting-webhook").checked,
        url: $("#setting-webhook-url").value.trim(),
      },
      mail: {
        enabled: $("#setting-mail").checked,
        recipient: $("#setting-mail-recipient").value.trim(),
        port: Number($("#setting-mail-port").value),
      },
      kinds: [
        ...$("#notification-kinds").querySelectorAll("input:checked"),
      ].map((input) => input.value),
      cameras: $("#setting-notification-cameras")
        .value.split(",")
        .map((name) => name.trim())
        .filter(Boolean),
    };
    act(async () => {
      const saved = await api("/notifications", "PATCH", payload);
      s.notifyDirty = false;
      $("#setting-webhook-secret").value = saved.secret || "";
      renderDeliveries(saved.deliveries);
      toast(t("saved"));
    }, e.submitter);
  });
  $("#reveal-secret").addEventListener("click", () => {
    const input = $("#setting-webhook-secret");
    const reveal = input.type === "password";
    input.type = reveal ? "text" : "password";
    $("#reveal-secret").textContent = t(reveal ? "hideSecret" : "revealSecret");
  });
  $("#rotate-secret").addEventListener("click", (e) =>
    act(async () => {
      const saved = await api("/notifications/secret", "POST");
      $("#setting-webhook-secret").value = saved.secret;
      toast(t("saved"));
    }, e.currentTarget),
  );
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
  $("#media-search").addEventListener("input", () => {
    clearTimeout(s.searchTimer);
    s.searchTimer = setTimeout(() => {
      const next = $("#media-search").value.trim();
      if (next === s.query) return;
      s.query = next;
      s.offset = 0;
      act(refresh);
    }, 250);
  });
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
      const library = await api(
        mediaPath({ limit: 500, offset: 0, kind: "snapshot", query: s.query }),
      );
      if (library.total > 500) throw new Error(t("timelapseTooMany"));
      const ids = library.items
        .filter((i) => i.kind === "snapshot")
        .sort((a, b) => a.created - b.created)
        .map((i) => i.id);
      if (ids.length < 2) throw new Error(t("needPhotos"));
      if (!confirm(t("timelapseHint").replace("{count}", String(ids.length))))
        return;
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
    else if (s.user && s.view === "overview") resumeLive();
  });
  function bindRegionStage() {
    const stage = $("#region-stage");
    let origin = null;
    stage.addEventListener("pointerdown", (event) => {
      if (s.regions.length >= 8) return;
      const bounds = stage.getBoundingClientRect();
      origin = {
        x: (event.clientX - bounds.left) / bounds.width,
        y: (event.clientY - bounds.top) / bounds.height,
      };
      stage.setPointerCapture(event.pointerId);
    });
    stage.addEventListener("pointerup", (event) => {
      if (!origin) return;
      const bounds = stage.getBoundingClientRect();
      const x = (event.clientX - bounds.left) / bounds.width;
      const y = (event.clientY - bounds.top) / bounds.height;
      const region = {
        x: Math.min(origin.x, x),
        y: Math.min(origin.y, y),
        w: Math.abs(x - origin.x),
        h: Math.abs(y - origin.y),
      };
      origin = null;
      if (region.w < 0.02 || region.h < 0.02) return;
      region.x = Math.max(0, Math.min(1, region.x));
      region.y = Math.max(0, Math.min(1, region.y));
      region.w = Math.min(region.w, 1 - region.x);
      region.h = Math.min(region.h, 1 - region.y);
      s.regions.push(region);
      s.cameraDirty = true;
      renderRegions();
    });
  }
  bindRegionStage();
  $("#region-clear").addEventListener("click", () => {
    s.regions = [];
    s.cameraDirty = true;
    renderRegions();
  });
  $("#clear-ptz-password").addEventListener("click", () => {
    s.clearPtzPassword = true;
    s.cameraDirty = true;
    $("#setting-ptz-password").value = "";
    toast(t("clearPtzPassword"));
  });
  $("#password-form").addEventListener("submit", (event) => {
    event.preventDefault();
    act(async () => {
      await api("/auth/password", "POST", {
        current_password: $("#current-password").value,
        new_password: $("#new-password").value,
      });
      showLogin();
    }, event.submitter);
  });
  $("#user-password-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    act(async () => {
      await api(`/users/${s.passwordUser}`, "PATCH", {
        password: new FormData(form).get("password"),
      });
      $("#user-password-dialog").close();
      toast(t("saved"));
    }, event.submitter);
  });
  $("#integration-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const password = $("#setting-onvif-password").value;
    const payload = {
      enabled: $("#setting-onvif").checked,
      username: $("#setting-onvif-user").value.trim(),
    };
    if (password) payload.password = password;
    act(async () => {
      await api("/integrations/onvif", "PUT", payload);
      $("#setting-onvif-password").value = "";
      toast(t("saved"));
      await refresh();
    }, event.submitter);
  });
  $("#pair-homeassistant").addEventListener("click", () =>
    act(async () => {
      const paired = await api("/integrations/homeassistant/pair", "POST", {
        name: "Home Assistant",
      });
      const note = $("#pair-token");
      note.hidden = false;
      note.textContent = `${t("shareReady")} ${paired.token}`;
      await refresh();
    }, $("#pair-homeassistant")),
  );
  $("#notify-desktop").addEventListener("click", async () => {
    if (typeof Notification !== "function") return toast(t("noHls"), true);
    const permission = await Notification.requestPermission();
    s.desktopNotes = permission === "granted";
    localStorage.setItem("campix-notes", s.desktopNotes ? "1" : "0");
    toast(s.desktopNotes ? t("enabled") : t("disabled"), !s.desktopNotes);
  });
  $("#backup-button").addEventListener("click", () =>
    act(async () => {
      const response = await fetch("/api/v1/backup", {
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(t("backup"));
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = make("a");
      link.href = url;
      link.download = "campix-backup.sqlite3";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }, $("#backup-button")),
  );
  $("#import-file").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () =>
      act(async () => {
        await api("/import", "POST", JSON.parse(String(reader.result)));
        toast(t("saved"));
        await refresh();
      });
    reader.readAsText(file);
    event.target.value = "";
  });
  $("#raspimjpeg-file").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () =>
      act(async () => {
        const proposal = await api("/import/raspimjpeg", "POST", {
          text: String(reader.result),
        });
        if (proposal.width && proposal.height)
          $("#setting-resolution").value =
            `${proposal.width}x${proposal.height}`;
        if (proposal.fps) $("#setting-fps").value = proposal.fps;
        if ("motion_enabled" in proposal)
          $("#setting-motion").checked = !!proposal.motion_enabled;
        s.cameraDirty = true;
        toast(t("saved"));
      });
    reader.readAsText(file);
    event.target.value = "";
  });
  for (const [id, action] of [
    ["bulk-lock", "lock"],
    ["bulk-unlock", "unlock"],
    ["bulk-delete", "delete"],
  ])
    $("#" + id).addEventListener("click", () =>
      act(
        async () => {
          if (!s.picked.size) return;
          if (action === "delete" && !confirm(t("confirmDelete"))) return;
          await api("/media/actions", "POST", { ids: [...s.picked], action });
          s.picked.clear();
          await refresh();
        },
        $("#" + id),
      ),
    );
  $("#recover-button").addEventListener("click", () =>
    act(async () => {
      await api("/media/recovery", "POST");
      toast(t("recover"));
    }, $("#recover-button")),
  );
  $("#media-camera").addEventListener("change", () =>
    act(async () => {
      s.mediaCamera = $("#media-camera").value;
      s.offset = 0;
      await refresh();
    }),
  );
  for (const id of ["media-from", "media-to"])
    $("#" + id).addEventListener("change", () =>
      act(async () => {
        s.mediaFrom = $("#media-from").value;
        s.mediaTo = $("#media-to").value;
        s.offset = 0;
        await refresh();
      }),
    );
  document.addEventListener("keydown", (event) => {
    const dialog = $("#media-dialog");
    if (!dialog.open) return;
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const items = s.view === "media" ? s.media : s.recent;
    const index = items.findIndex(
      (item) => item.filename === $("#media-dialog-title").textContent,
    );
    const next = items[index + (event.key === "ArrowRight" ? 1 : -1)];
    if (!next) return;
    event.preventDefault();
    openMedia(next);
  });
  $$("[data-ptz]").forEach((button) => {
    button.addEventListener("pointerdown", () => {
      const value = button.dataset.ptz;
      const action = value === "stop" ? "stop" : "move";
      const [pan, tilt] =
        value === "stop" ? [0, 0] : value.split(",").map(Number);
      act(async () => {
        await api(`/cameras/${s.selected}/ptz`, "POST", {
          action,
          pan,
          tilt,
          zoom: 0,
        });
      }, button);
    });
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
