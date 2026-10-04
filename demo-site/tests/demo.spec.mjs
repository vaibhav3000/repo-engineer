import { test, expect } from "@playwright/test";

const URL = "/index.html";

function watchErrors(page) {
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  return errors;
}

test("page loads with replay, benchmark record, and attack panel", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto(URL);
  await expect(page).toHaveTitle(/Agent Run Explorer/);
  await expect(page.locator("#pipeline .g")).toHaveCount(6);
  await expect(page.locator("#task-btns button")).toHaveCount(4);
  await expect(page.locator("#attacks details")).toHaveCount(8);
  const ok = await page.evaluate(() => ({
    traces: Object.keys(window.DATA.traces).length,
    benchmark: window.DATA.benchmark.summary.n_success,
  }));
  expect(ok.traces).toBe(4);
  expect(ok.benchmark).toBe(4);
  expect(errors).toEqual([]);
});

test("replay Step/Back/Play/Reset works", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto(URL);
  const progress = page.locator("#progress");
  await page.locator("#btn-step").click();
  await expect(progress).toContainText("step 1 of 6");
  await page.locator("#btn-back").click();
  await expect(progress).toContainText("step 0 of 6");
  const play = page.locator("#btn-play");
  await play.click();
  await expect(play).toHaveAttribute("aria-pressed", "true");
  await play.click();
  await expect(play).toHaveAttribute("aria-pressed", "false");
  await page.locator("#btn-reset").click();
  await expect(progress).toContainText("step 0 of 6");
  expect(errors).toEqual([]);
});

test("failure-to-fix walkthrough reaches the verified final state", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto(URL);
  await page.locator("#story-failure").click();
  const card = page.locator("#tour-card");
  await expect(card).toBeVisible();
  // step 1: the suite is red
  await expect(page.locator("#obs-view")).toContainText("exit=1");
  await expect(page.locator("#pipeline .g.stop")).toHaveText("OBSERVATION");
  // walk to the end
  for (let i = 0; i < 5; i++) await page.locator("#tour-next").click();
  await expect(page.locator("#tour-title")).toContainText("VERIFY, then COMPLETE");
  await expect(page.locator("#obs-view")).toContainText("exit=0");
  await page.keyboard.press("Escape");
  await expect(card).toBeHidden();
  expect(errors).toEqual([]);
});

test("attack panel stops at the real gate with the real rejection", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto(URL);
  const first = page.locator("#attacks details").first();
  await first.locator("summary").click();
  await expect(first.locator(".g.stop")).toHaveText("JAIL");
  await expect(first.locator(".err")).toContainText("path escape rejected");
  // allowlist attack stops at POLICY
  const curl = page.locator("#attacks details", { hasText: "curl" });
  await curl.locator("summary").click();
  await expect(curl.locator(".g.stop")).toHaveText("POLICY");
  await expect(curl.locator(".err")).toContainText("not in allowlist");
  // replay animation resets then re-applies the final state
  await first.locator(".attack-replay").click();
  await expect(first.locator(".gatepath")).toBeHidden();
  await expect(first.locator(".g.stop")).toHaveText("JAIL", { timeout: 4000 });
  expect(errors).toEqual([]);
});

test("keyboard: Space on body drives the player, focused buttons keep Space", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto(URL);
  const play = page.locator("#btn-play");
  await page.locator("body").press("Space");
  await expect(play).toHaveAttribute("aria-pressed", "true");
  await page.locator("body").press("Space");
  await expect(play).toHaveAttribute("aria-pressed", "false");
  // a focused task button must not be hijacked by the page handler:
  // Space activates the button natively, and the player stays paused
  const task = page.locator("#task-btns button").nth(2);
  await task.focus();
  await page.keyboard.press("Space");
  await expect(page.locator("#btn-play")).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator('#task-btns button[aria-pressed="true"]')).toHaveCount(1);
  expect(errors).toEqual([]);
});

test("no horizontal overflow at 390px", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(URL);
  const o = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    w: document.documentElement.clientWidth,
  }));
  expect(o.sw).toBeLessThanOrEqual(o.w);
});
