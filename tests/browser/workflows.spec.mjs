import { test, expect } from "@playwright/test";

// Reuse test-only sessions across fresh browser contexts without exhausting login limits.
const sessionCookies = new Map();

function previewRoute(camera) {
  return new RegExp(`/api/v1/cameras/${camera}/preview\\.jpg(?:\\?|#|$)`);
}

function isPreviewRequest(url, camera = "") {
  const suffix = camera ? `/${camera}/preview.jpg` : "/preview.jpg";
  return new URL(url).pathname.endsWith(suffix);
}

async function signin(page, role = "admin") {
  let cookies = sessionCookies.get(role);
  if (cookies) {
    await page.context().addCookies(cookies);
    const session = await page.request.get("/api/v1/auth/me");
    if (session.ok()) expect((await session.json()).user.role).toBe(role);
    else {
      sessionCookies.delete(role);
      await page.context().clearCookies();
      cookies = null;
    }
  }
  await page.goto("/");
  if (!cookies) {
    await expect(page.locator("#login-screen")).toBeVisible();
    await page.locator("#login-form [name=username]").fill(role);
    await page
      .locator("#login-form [name=password]")
      .fill(`test-only-${role}-password`);
    await page.locator("#login-form button").click();
  }
  await expect(page.locator("#workspace")).toBeVisible();
  if (!cookies) {
    cookies = (await page.context().cookies()).filter(
      (cookie) => cookie.name === "campix_session",
    );
    expect(cookies).toHaveLength(1);
    sessionCookies.set(role, cookies);
  }
  await expect(page.locator("#camera-count")).toHaveText("1 / 1");
  await page.locator("#transport-select").selectOption("mjpeg");
}

test("real capture, recording, library, schedule and persistent profile", async ({
  page,
}) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await signin(page);
  await expect(page.locator("#live-image")).toBeVisible();
  await expect
    .poll(() =>
      page.locator("#live-image").evaluate((image) => image.naturalWidth),
    )
    .toBe(640);
  await page.locator("[data-view-panel=overview] [data-capture]").click();
  await expect(page.locator("#recent-media .media-card")).toHaveCount(1);
  await page.locator("#record-button").click();
  await expect(page.locator("#record-label")).toHaveText("Aufnahme beenden");
  await page.waitForTimeout(700);
  await page.locator("#record-button").click();
  await expect(page.locator("#record-label")).toHaveText("Aufnehmen");
  await page.locator(".nav-item[data-view=media]").click();
  await expect(page.locator("#media-grid .gallery-card")).toHaveCount(2);
  await page.locator("#media-grid .gallery-card").first().click();
  await expect(page.locator("#media-dialog video")).toBeVisible();
  await expect
    .poll(() =>
      page.locator("#media-dialog video").evaluate((video) => video.readyState),
    )
    .toBeGreaterThan(0);
  await page.locator("#media-dialog [data-close]").click();
  await page.locator(".nav-item[data-view=scheduler]").click();
  await page.locator("#new-schedule").click();
  await page.locator("#schedule-form [name=name]").fill("Browser schedule");
  await page.locator("#schedule-form .full-button").click();
  await expect(page.locator("#schedule-list h3")).toHaveText(
    "Browser schedule",
  );
  await page.locator(".nav-item[data-view=settings]").click();
  await page.locator("#setting-camera-name").fill("Browser validated camera");
  await page.locator("#camera-settings-form button").click();
  await expect(page.locator("#setting-camera-name")).toHaveValue(
    "Browser validated camera",
  );
  await page.reload();
  await expect(page.locator("#workspace")).toBeVisible();
  await page.locator(".nav-item[data-view=settings]").click();
  await expect(page.locator("#setting-camera-name")).toHaveValue(
    "Browser validated camera",
  );
  await page.screenshot({
    path: "test-results/settings-desktop.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("explicit encoder settings persist and automatic selection reports a tested profile", async ({
  page,
}) => {
  await signin(page);
  await page.locator(".nav-item[data-view=settings]").click();
  await page.locator("#setting-encoder").selectOption("libx264");
  await page.locator("#camera-settings-form button").click();
  await expect
    .poll(async () => {
      const cameras = await (await page.request.get("/api/v1/cameras")).json();
      return cameras[0].encoding.selected;
    })
    .toBe("libx264");
  await page.reload();
  await expect(page.locator("#workspace")).toBeVisible();
  await page.locator(".nav-item[data-view=settings]").click();
  await expect(page.locator("#setting-encoder")).toHaveValue("libx264");
  await page.locator("#setting-encoder").selectOption("auto");
  await page.locator("#camera-settings-form button").click();
  await expect
    .poll(async () => {
      const cameras = await (await page.request.get("/api/v1/cameras")).json();
      return cameras[0].encoding.profile_tested;
    })
    .toBe(true);
  await page.locator(".nav-item[data-view=devices]").click();
  await expect(page.locator("#device-list")).toContainText("Selected encoder");
});

test("live JPEG preview can exceed 30 requests per second without parallel buffering", async ({
  page,
}) => {
  await signin(page);
  await page.locator(".nav-item[data-view=settings]").click();
  await page.locator("#setting-fps").fill("60");
  await page.locator("#camera-settings-form button").click();
  await expect
    .poll(async () => {
      const cameras = await (await page.request.get("/api/v1/cameras")).json();
      return cameras[0].fps;
    })
    .toBeGreaterThan(40);
  let requests = 0;
  const pending = new Set();
  let maximum = 0;
  page.on("request", (request) => {
    if (!isPreviewRequest(request.url())) return;
    requests++;
    pending.add(request);
    maximum = Math.max(maximum, pending.size);
  });
  page.on("requestfinished", (request) => {
    pending.delete(request);
  });
  page.on("requestfailed", (request) => {
    pending.delete(request);
  });
  await page.locator(".nav-item[data-view=overview]").click();
  await expect(page.locator("#transport-select")).toHaveValue("mjpeg");
  await expect(page.locator("#live-badge")).toBeVisible();
  const first = requests;
  await page.waitForTimeout(3000);
  expect(requests - first).toBeGreaterThan(100);
  expect(maximum).toBe(1);
  await page.locator(".nav-item[data-view=settings]").click();
  await page.locator("#setting-fps").fill("10");
  await page.locator("#camera-settings-form button").click();
  await expect
    .poll(async () => {
      const cameras = await (await page.request.get("/api/v1/cameras")).json();
      return cameras[0].fps_target;
    })
    .toBe(10);
});

test("DirectShow webcams are offered and unsafe device names are rejected", async ({
  page,
}) => {
  await signin(page);
  await page.locator("[data-view-panel=overview] [data-add-camera]").click();
  await expect(page.locator("#camera-dialog")).toBeVisible();
  await page.locator("#new-camera-backend").selectOption("dshow");
  await expect(page.locator("#new-camera-source")).toHaveValue("");
  await page.locator("#camera-form [name=name]").fill("Webcam");
  // ":" would let a device name add an audio device to FFmpeg's DirectShow input.
  await page.locator("#new-camera-source").fill("Webcam:audio=Microphone");
  await page.locator("#camera-form .full-button").click();
  await expect(page.locator("#toast")).toHaveClass(/error-toast/);
  await expect(page.locator("#toast-message")).toContainText(
    "DirectShow source",
  );
  await expect(page.locator("#camera-count")).toHaveText("1 / 1");
});

test("live JPEG preview stays bounded and cancels pending frames when leaving", async ({
  page,
}) => {
  await signin(page);
  await page.locator(".nav-item[data-view=media]").click();
  const jpeg = await (
    await page.request.get("/api/v1/cameras/demo/preview.jpg")
  ).body();
  let requests = 0;
  let aborted = 0;
  const releases = [];
  page.on("requestfailed", (request) => {
    if (isPreviewRequest(request.url())) aborted++;
  });
  await page.route(previewRoute("demo"), async (route) => {
    requests++;
    await new Promise((resolve) => releases.push(resolve));
    await route
      .fulfill({ contentType: "image/jpeg", body: jpeg })
      .catch(() => {});
  });
  await page.locator(".nav-item[data-view=overview]").click();
  await expect.poll(() => requests).toBe(1);
  await expect(page.locator("#live-badge")).toBeHidden();
  await page.waitForTimeout(350);
  expect(requests).toBe(1);
  releases[0]();
  await expect(page.locator("#live-badge")).toBeVisible();
  await expect
    .poll(() =>
      page.locator("#live-image").evaluate((image) => image.naturalWidth),
    )
    .toBe(640);
  await expect(page.locator("#live-image")).toHaveAttribute("src", /^blob:/);
  await expect.poll(() => requests).toBe(2);
  await page.locator(".nav-item[data-view=media]").click();
  await expect.poll(() => aborted).toBe(1);
  await expect(page.locator("#live-image")).not.toHaveAttribute("src");
  releases[1]();
  await page.waitForTimeout(200);
  expect(requests).toBe(2);
  await expect(page.locator("#live-badge")).toBeHidden();
  await page.locator(".nav-item[data-view=overview]").click();
  await expect.poll(() => requests).toBe(3);
  releases[2]();
  await expect(page.locator("#live-badge")).toBeVisible();
  await page.locator(".nav-item[data-view=media]").click();
  releases.forEach((release) => release());
});

test("live JPEG preview rejects expired sessions and clears the displayed frame", async ({
  page,
}) => {
  await signin(page);
  await page.locator(".nav-item[data-view=media]").click();
  await page.route(previewRoute("demo"), (route) =>
    route.fulfill({ status: 401, json: { detail: "Authentication required" } }),
  );
  await page.locator(".nav-item[data-view=overview]").click();
  await expect(page.locator("#login-screen")).toBeVisible();
  await expect(page.locator("#live-image")).not.toHaveAttribute("src");
  await expect(page.locator("#live-badge")).toBeHidden();
});

test("live JPEG preview abandons stalled frames and recovers from invalid images", async ({
  page,
}) => {
  await signin(page);
  await page.locator(".nav-item[data-view=media]").click();
  const jpeg = await (
    await page.request.get("/api/v1/cameras/demo/preview.jpg")
  ).body();
  let requests = 0;
  let aborted = 0;
  let release;
  page.on("requestfailed", (request) => {
    if (isPreviewRequest(request.url())) aborted++;
  });
  await page.route(previewRoute("demo"), async (route) => {
    const request = ++requests;
    if (request === 1) await new Promise((resolve) => (release = resolve));
    await route
      .fulfill({
        contentType: "image/jpeg",
        body: request === 2 ? Buffer.from("invalid JPEG") : jpeg,
      })
      .catch(() => {});
  });
  await page.locator(".nav-item[data-view=overview]").click();
  await expect.poll(() => requests).toBe(1);
  await expect(page.locator("#live-badge")).toBeHidden();
  await expect.poll(() => aborted, { timeout: 5000 }).toBe(1);
  await expect(page.locator("#live-empty p")).toHaveAttribute(
    "data-i18n",
    "liveUnavailable",
  );
  await expect.poll(() => requests).toBeGreaterThanOrEqual(3);
  await expect(page.locator("#live-badge")).toBeVisible();
  await expect
    .poll(() =>
      page.locator("#live-image").evaluate((image) => image.naturalWidth),
    )
    .toBe(640);
  release();
  await page.locator(".nav-item[data-view=media]").click();
});

test("live JPEG preview cancels camera switches and pauses in a hidden page", async ({
  page,
}) => {
  await signin(page);
  await page.locator(".nav-item[data-view=media]").click();
  const cameras = await (await page.request.get("/api/v1/cameras")).json();
  const jpeg = await (
    await page.request.get("/api/v1/cameras/demo/preview.jpg")
  ).body();
  await page.route("**/api/v1/cameras", (route) =>
    route.fulfill({
      json: [
        ...cameras,
        { ...cameras[0], id: "alternate", name: "Alternate live camera" },
      ],
    }),
  );
  await page.locator("#refresh-button").click();
  await expect(page.locator("#camera-count")).toHaveText("2 / 2");
  let originalRequests = 0;
  let alternateRequests = 0;
  let aborted = 0;
  let release;
  page.on("requestfailed", (request) => {
    if (isPreviewRequest(request.url(), "demo")) aborted++;
  });
  await page.route(previewRoute("demo"), async (route) => {
    originalRequests++;
    await new Promise((resolve) => (release = resolve));
    await route
      .fulfill({ contentType: "image/jpeg", body: jpeg })
      .catch(() => {});
  });
  await page.route(previewRoute("alternate"), (route) => {
    alternateRequests++;
    return route.fulfill({ contentType: "image/jpeg", body: jpeg });
  });
  await page.locator(".nav-item[data-view=overview]").click();
  await expect.poll(() => originalRequests).toBe(1);
  await page.locator("#camera-select").selectOption("alternate");
  await expect.poll(() => aborted).toBe(1);
  await expect(page.locator("#live-badge")).toBeVisible();
  await expect(page.locator("#active-camera-name")).toHaveText(
    "Alternate live camera",
  );
  release();
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      value: true,
      configurable: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.locator("#live-image")).not.toHaveAttribute("src");
  await expect(page.locator("#live-badge")).toBeHidden();
  const pausedRequests = alternateRequests;
  await page.waitForTimeout(200);
  expect(alternateRequests).toBe(pausedRequests);
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      value: false,
      configurable: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => alternateRequests).toBeGreaterThan(pausedRequests);
  await expect(page.locator("#live-badge")).toBeVisible();
  await expect
    .poll(() =>
      page.locator("#live-image").evaluate((image) => image.naturalWidth),
    )
    .toBe(640);
  await page.locator(".nav-item[data-view=media]").click();
});

test("live JPEG preview abandons a stalled decode without replaying it", async ({
  page,
}) => {
  await signin(page);
  await page.locator(".nav-item[data-view=media]").click();
  const jpeg = await (
    await page.request.get("/api/v1/cameras/demo/preview.jpg")
  ).body();
  await page.evaluate(() => {
    const decode = Image.prototype.decode;
    let calls = 0;
    Image.prototype.decode = function () {
      if (this.src.startsWith("blob:") && ++calls === 1)
        return new Promise((resolve) => {
          window.resolveStalledPreviewDecode = resolve;
        });
      return decode.call(this);
    };
  });
  let requests = 0;
  const releases = [];
  await page.route(previewRoute("demo"), async (route) => {
    if (++requests > 2) await new Promise((resolve) => releases.push(resolve));
    await route
      .fulfill({ contentType: "image/jpeg", body: jpeg })
      .catch(() => {});
  });
  await page.locator(".nav-item[data-view=overview]").click();
  await expect.poll(() => requests).toBe(1);
  await expect(page.locator("#live-badge")).toBeHidden();
  await expect.poll(() => requests, { timeout: 5000 }).toBe(3);
  await expect(page.locator("#live-badge")).toBeVisible();
  await expect
    .poll(() =>
      page.locator("#live-image").evaluate((image) => image.naturalWidth),
    )
    .toBe(640);
  const displayed = await page.locator("#live-image").getAttribute("src");
  await page.evaluate(() => window.resolveStalledPreviewDecode());
  await page.waitForTimeout(150);
  await expect(page.locator("#live-image")).toHaveAttribute("src", displayed);
  await page.locator(".nav-item[data-view=media]").click();
  releases.forEach((release) => release());
});

test("live JPEG preview recovers when a later frame decode stalls", async ({
  page,
}) => {
  await signin(page);
  await page.locator(".nav-item[data-view=media]").click();
  const jpeg = await (
    await page.request.get("/api/v1/cameras/demo/preview.jpg")
  ).body();
  await page.evaluate(() => {
    const decode = Image.prototype.decode;
    let calls = 0;
    Image.prototype.decode = function () {
      if (this.src.startsWith("blob:") && ++calls === 2)
        return new Promise((resolve) => {
          window.resolveStalledPreviewDecode = resolve;
        });
      return decode.call(this);
    };
  });
  let requests = 0;
  const releases = [];
  await page.route(previewRoute("demo"), async (route) => {
    if (++requests > 3) await new Promise((resolve) => releases.push(resolve));
    await route
      .fulfill({ contentType: "image/jpeg", body: jpeg })
      .catch(() => {});
  });
  await page.locator(".nav-item[data-view=overview]").click();
  await expect.poll(() => requests).toBe(1);
  await expect(page.locator("#live-badge")).toBeVisible();
  await expect.poll(() => requests, { timeout: 8000 }).toBe(4);
  await expect(page.locator("#live-badge")).toBeVisible();
  await expect
    .poll(() =>
      page.locator("#live-image").evaluate((image) => image.naturalWidth),
    )
    .toBe(640);
  const displayed = await page.locator("#live-image").getAttribute("src");
  await page.evaluate(() => window.resolveStalledPreviewDecode());
  await page.waitForTimeout(150);
  await expect(page.locator("#live-image")).toHaveAttribute("src", displayed);
  await page.locator(".nav-item[data-view=media]").click();
  releases.forEach((release) => release());
});

test("viewer cannot capture or administer; logout revokes the session", async ({
  page,
}) => {
  await signin(page, "viewer");
  await expect(page.locator("#record-button")).toBeDisabled();
  await expect(
    page.locator("[data-view-panel=overview] [data-add-camera]"),
  ).toBeHidden();
  await page.locator(".nav-item[data-view=settings]").click();
  await expect(page.locator("#settings-forbidden")).toBeVisible();
  await expect(page.locator("#admin-settings")).toBeHidden();
  await page.locator("#logout-button").click();
  await expect(page.locator("#login-screen")).toBeVisible();
  expect((await page.request.get("/api/v1/cameras")).status()).toBe(401);
});

test("live JPEG fullscreen has a visible exit and supports keyboard and localization", async ({
  page,
}) => {
  await signin(page, "viewer");
  await expect(page.locator("#live-badge")).toBeVisible();
  const entry = page.locator("#fullscreen-button");
  await entry.click();
  await expect
    .poll(() => page.evaluate(() => document.fullscreenElement?.id))
    .toBe("live-stage");
  const exit = page.locator("#live-stage").getByRole("button", {
    name: "Vollbild beenden",
    exact: true,
  });
  await expect(exit).toBeVisible();
  await expect(exit).toBeFocused();
  await expect(page.locator("#live-image")).toHaveCSS("object-fit", "contain");
  expect(
    await page.locator("#live-stage").evaluate((stage) => {
      const bounds = stage.getBoundingClientRect();
      return bounds.width === innerWidth && bounds.height === innerHeight;
    }),
  ).toBe(true);
  await page.screenshot({ path: "test-results/live-fullscreen-jpeg.png" });
  await exit.press("Enter");
  await expect
    .poll(() => page.evaluate(() => document.fullscreenElement))
    .toBeNull();
  await expect(entry).toBeFocused();
  await expect(entry).toHaveAccessibleName("Vollbild");
  await expect(entry).toHaveAttribute("aria-expanded", "false");
  await entry.press("Enter");
  await expect(exit).toBeVisible();
  await expect(entry).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("Escape");
  await expect(exit).toBeHidden();
  await expect(entry).toBeFocused();
  await entry.click();
  await expect(exit).toBeVisible();
  await page.evaluate(() => document.exitFullscreen());
  await expect(exit).toBeHidden();
  await expect(entry).toBeFocused();
  await page.locator("#language-button").click();
  await expect(entry).toHaveAccessibleName("Fullscreen");
  await entry.click();
  await page
    .locator("#live-stage")
    .getByRole("button", { name: "Exit fullscreen", exact: true })
    .click();
  await expect(page.locator("#live-badge")).toBeVisible();
});

for (const mode of ["unavailable", "denied"]) {
  test(`mobile live view expands when browser fullscreen is ${mode} and cleans up on session expiry`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await signin(page, "operator");
    await expect(page.locator("#live-badge")).toBeVisible();
    await page.evaluate((mode) => {
      document.querySelector("#live-stage").requestFullscreen =
        mode === "unavailable"
          ? undefined
          : () => Promise.reject(new Error("Browser denied fullscreen"));
    }, mode);
    const entry = page.locator("#fullscreen-button");
    const exit = page.locator("#fullscreen-exit");
    await entry.click();
    await expect(page.locator("html")).toHaveClass(/live-expanded/);
    await expect(exit).toBeVisible();
    await expect(exit).toBeFocused();
    await expect(entry).toHaveAttribute("aria-expanded", "true");
    for (const viewport of [
      { width: 390, height: 844 },
      { width: 844, height: 390 },
    ]) {
      await page.setViewportSize(viewport);
      expect(
        await page.locator("#live-stage").evaluate((stage) => {
          const bounds = stage.getBoundingClientRect();
          return (
            bounds.x === 0 &&
            bounds.y === 0 &&
            bounds.width === innerWidth &&
            bounds.height === innerHeight
          );
        }),
      ).toBe(true);
      await expect(exit).toBeInViewport({ ratio: 1 });
      if (mode === "unavailable" && viewport.width === 390)
        await page.screenshot({
          path: "test-results/live-expanded-mobile.png",
        });
    }
    await page.keyboard.press("Tab");
    await expect(exit).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(exit).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.locator("html")).not.toHaveClass(/live-expanded/);
    await expect(entry).toBeFocused();
    await expect(entry).toHaveAttribute("aria-expanded", "false");
    await entry.click();
    await expect(exit).toBeVisible();
    await page.route(previewRoute("demo"), (route) =>
      route.fulfill({
        status: 401,
        json: { detail: "Authentication required" },
      }),
    );
    await expect(page.locator("#login-screen")).toBeVisible();
    await expect(page.locator("html")).not.toHaveClass(/live-expanded/);
    await expect(page.locator("#live-image")).not.toHaveAttribute("src");
    await expect(exit).toBeHidden();
  });
}

test("session expiry exits native live fullscreen and clears the frame", async ({
  page,
}) => {
  await signin(page);
  await expect(page.locator("#live-badge")).toBeVisible();
  await page.locator("#fullscreen-button").click();
  await expect
    .poll(() => page.evaluate(() => document.fullscreenElement?.id))
    .toBe("live-stage");
  await page.route(previewRoute("demo"), (route) =>
    route.fulfill({ status: 401, json: { detail: "Authentication required" } }),
  );
  await expect(page.locator("#login-screen")).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => document.fullscreenElement))
    .toBeNull();
  await expect(page.locator("#live-image")).not.toHaveAttribute("src");
  await expect(page.locator("#fullscreen-exit")).toBeHidden();
});

test("a pending fullscreen rejection cannot reopen the live view after session expiry", async ({
  page,
}) => {
  await signin(page);
  await expect(page.locator("#live-badge")).toBeVisible();
  await page.evaluate(() => {
    document.querySelector("#live-stage").requestFullscreen = () =>
      new Promise((_, reject) => {
        window.rejectPendingFullscreen = () => reject(new Error("Denied"));
      });
  });
  await page.locator("#fullscreen-button").click();
  await expect(page.locator("#fullscreen-button")).toBeDisabled();
  await page.route(previewRoute("demo"), (route) =>
    route.fulfill({ status: 401, json: { detail: "Authentication required" } }),
  );
  await expect(page.locator("#login-screen")).toBeVisible();
  await page.evaluate(() => window.rejectPendingFullscreen());
  await expect(page.locator("#fullscreen-button")).toBeEnabled();
  await expect(page.locator("html")).not.toHaveClass(/live-expanded/);
  await expect(page.locator("#fullscreen-exit")).toBeHidden();
});

test("mobile navigation and layout remain usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signin(page, "operator");
  await expect(page.locator("#live-image")).toBeVisible();
  await expect
    .poll(() =>
      page.locator("#live-image").evaluate((image) => image.naturalWidth),
    )
    .toBe(640);
  await expect(page.locator("[data-view-panel=overview]")).toHaveCSS(
    "opacity",
    "1",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: "test-results/overview-mobile.png",
    fullPage: true,
  });
  await page.locator("#mobile-menu").click();
  await page.locator(".nav-item[data-view=media]").click();
  await expect(page.locator("[data-view-panel=media]")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
});

test("library search reaches older photos and timelapse uses the match", async ({
  page,
}) => {
  test.setTimeout(90000);
  await signin(page);
  const session = await page.request.get("/api/v1/auth/me");
  expect(session.ok()).toBeTruthy();
  const { csrf } = await session.json();
  const capture = async () => {
    const response = await page.request.post("/api/v1/cameras/demo/snapshot", {
      headers: { "X-CSRF-Token": csrf },
    });
    expect(response.ok()).toBeTruthy();
    return response.json();
  };
  const older = await capture();
  for (let index = 0; index < 24; index += 1) await capture();
  await page.locator(".nav-item[data-view=media]").click();
  await expect(page.locator("#media-grid .gallery-card")).toHaveCount(24);
  await expect(page.locator("#media-grid")).not.toContainText(older.filename);
  const libraryTotal = await page.locator("#nav-media-count").textContent();
  await page.locator("#media-search").fill(older.filename);
  await expect(page.locator("#media-grid .gallery-card")).toHaveCount(1);
  await expect(page.locator("#media-grid")).toContainText(older.filename);
  await expect(page.locator("#media-page")).toHaveText("1 · 1");
  await expect(page.locator("#nav-media-count")).toHaveText(libraryTotal);
  await page.screenshot({
    path: "test-results/media-search.png",
    fullPage: true,
  });
  await page.locator(".nav-item[data-view=overview]").click();
  await expect(page.locator("#recent-media .media-card")).toHaveCount(3);
  await expect(page.locator("#recent-media")).not.toContainText(older.filename);
  await page.locator(".nav-item[data-view=media]").click();
  await page.locator("#media-search").fill("");
  await expect(page.locator("#media-grid .gallery-card")).toHaveCount(24);
  const photos = await page.request.get(
    "/api/v1/media?kind=snapshot&limit=500",
  );
  expect(photos.ok()).toBeTruthy();
  const photoList = await photos.json();
  expect(photoList.total).toBeGreaterThan(24);
  expect(photoList.total).toBeLessThanOrEqual(500);
  let exported = null;
  await page.route("**/api/v1/timelapse", async (route) => {
    exported = route.request().postDataJSON();
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ id: "browser-timelapse", status: "queued" }),
    });
  });
  page.once("dialog", (dialog) => {
    expect(dialog.message()).toContain(String(photoList.total));
    dialog.accept();
  });
  await page.locator("#timelapse-button").click();
  await expect.poll(() => exported?.media_ids?.length).toBe(photoList.total);
  expect(exported.media_ids).toContain(older.id);
});

test("theme, language and authenticated downloads", async ({ page }) => {
  await signin(page);
  await page.locator("#language-button").click();
  await expect(page.locator("[data-view-panel=overview] h1")).toHaveText(
    "Camera control center",
  );
  await page.locator("#theme-toggle").click();
  await page.locator("[data-view-panel=overview] [data-capture]").click();
  await expect(
    page.locator("#recent-media .media-card").first(),
  ).toHaveAttribute("aria-label", /\.jpg$/);
  await page.locator("#recent-media .media-card").first().click();
  const downloadPromise = page.waitForEvent("download");
  await page.locator("#media-dialog-actions a").click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/\.jpg$/);
  await page.locator("#media-dialog [data-close]").click();
  await page.screenshot({
    path: "test-results/overview-desktop-light.png",
    fullPage: true,
  });
});

test("optional MediaMTX delivers real WebRTC video and authenticated HLS", async ({
  page,
}) => {
  test.skip(
    !process.env.CAMPIX_TEST_MEDIAMTX_BINARY,
    "Pinned MediaMTX binary not configured",
  );
  await signin(page);
  await expect
    .poll(
      async () => {
        const response = await page.request.get("/api/v1/system");
        return (await response.json()).media_plane.publishers.demo?.online;
      },
      { timeout: 20000 },
    )
    .toBeTruthy();
  await page.locator("#transport-select").selectOption("auto");
  await expect(page.locator("#actual-transport")).toHaveText("WebRTC", {
    timeout: 20000,
  });
  await expect
    .poll(() =>
      page.locator("#live-video").evaluate((video) => video.videoWidth),
    )
    .toBe(640);
  const hls = await page.request.get("/api/v1/streams/demo/hls/index.m3u8");
  expect(hls.status()).toBe(200);
  expect(await hls.text()).toContain("#EXTM3U");
  const sourceBefore = (
    await (await page.request.get("/api/v1/cameras")).json()
  )[0];
  expect(sourceBefore.encoding.shared).toBe(true);
  expect(sourceBefore.encoding.packet_ready).toBe(true);
  const sessions = sourceBefore.encoding.encoder_sessions_started;
  const epoch = sourceBefore.encoding.packet_stream.epoch;
  const framesBefore = await page
    .locator("#live-video")
    .evaluate((video) => video.getVideoPlaybackQuality().totalVideoFrames);
  const started = page.waitForResponse(
    (response) =>
      response.url().endsWith("/demo/recording") &&
      response.request().method() === "POST",
  );
  await page.locator("#record-button").click();
  expect((await started).status()).toBe(200);
  await expect(page.locator("#record-label")).toHaveText("Aufnahme beenden");
  await expect
    .poll(() =>
      page
        .locator("#live-video")
        .evaluate((video) => video.getVideoPlaybackQuality().totalVideoFrames),
    )
    .toBeGreaterThan(framesBefore + 2);
  const stopped = page.waitForResponse(
    (response) =>
      response.url().endsWith("/demo/recording") &&
      response.request().method() === "DELETE",
  );
  await page.locator("#record-button").click();
  const stopResponse = await stopped;
  expect(stopResponse.status()).toBe(200);
  const recorded = await stopResponse.json();
  expect(recorded.kind).toBe("video");
  const sourceAfter = (
    await (await page.request.get("/api/v1/cameras")).json()
  )[0];
  expect(sourceAfter.encoding.encoder_sessions_started).toBe(sessions);
  expect(sourceAfter.encoding.packet_stream.epoch).toBe(epoch);
  const system = await (await page.request.get("/api/v1/system")).json();
  expect(system.media_plane.publishers.demo.packet_passthrough).toBe(true);
  expect(system.media_plane.publishers.demo.packet_ready).toBe(true);
  const videoReady = await page.evaluate(async (id) => {
    const response = await fetch(`/api/v1/media/${id}/file`);
    const url = URL.createObjectURL(await response.blob());
    const video = document.createElement("video");
    video.muted = true;
    video.hidden = true;
    document.body.append(video);
    try {
      return await new Promise((resolve) => {
        const timer = setTimeout(() => resolve(false), 8000);
        video.onloadeddata = () => {
          clearTimeout(timer);
          resolve(video.videoWidth > 0);
        };
        video.onerror = () => {
          clearTimeout(timer);
          resolve(false);
        };
        video.src = url;
        video.load();
      });
    } finally {
      video.removeAttribute("src");
      video.load();
      video.remove();
      URL.revokeObjectURL(url);
    }
  }, recorded.id);
  expect(videoReady).toBe(true);
  await page.screenshot({
    path: "test-results/webrtc-desktop.png",
    fullPage: true,
  });
  await page.locator("#fullscreen-button").click();
  await expect
    .poll(() => page.evaluate(() => document.fullscreenElement?.id))
    .toBe("live-stage");
  await expect(page.locator("#fullscreen-exit")).toBeVisible();
  await expect(page.locator("#live-video")).toBeVisible();
  await expect(page.locator("#live-video")).toHaveCSS("object-fit", "contain");
  const jpeg = await (
    await page.request.get("/api/v1/cameras/demo/preview.jpg")
  ).body();
  let release;
  let requested = false;
  await page.route(previewRoute("demo"), async (route) => {
    requested = true;
    await new Promise((resolve) => (release = resolve));
    await route
      .fulfill({ contentType: "image/jpeg", body: jpeg })
      .catch(() => {});
  });
  await page.locator("#live-video").dispatchEvent("error");
  await expect.poll(() => requested).toBe(true);
  try {
    expect(await page.locator("#live-badge").isVisible()).toBe(false);
  } finally {
    release();
  }
  await expect(page.locator("#actual-transport")).toHaveText("JPEG");
  await expect(page.locator("#live-image")).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => document.fullscreenElement?.id))
    .toBe("live-stage");
  await page.locator("#fullscreen-exit").click();
  await expect
    .poll(() => page.evaluate(() => document.fullscreenElement))
    .toBeNull();
  await page.locator(".nav-item[data-view=media]").click();
});
