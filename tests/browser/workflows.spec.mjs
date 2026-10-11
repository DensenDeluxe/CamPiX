import { test, expect } from "@playwright/test";

async function signin(page, role = "admin") {
  await page.goto("/");
  await page.locator("#login-form [name=username]").fill(role);
  await page
    .locator("#login-form [name=password]")
    .fill(`test-only-${role}-password`);
  await page.locator("#login-form button").click();
  await expect(page.locator("#workspace")).toBeVisible();
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
  await page.screenshot({
    path: "test-results/webrtc-desktop.png",
    fullPage: true,
  });
});
