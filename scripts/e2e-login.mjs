/**
 * Drive the real sign-in form in a real browser, against a running Devrimo.
 *
 * The credentials come from the environment, so they stay on the machine that
 * runs this and appear in no transcript, no log and no argument list. The
 * script prints what it checked and never what it typed.
 *
 * Run it:
 *
 *   npx playwright install chromium          # once
 *   DEVRIMO_BASE_URL=https://devrimo.ates.digital \
 *   DEVRIMO_TEST_EMAIL=... DEVRIMO_TEST_PASSWORD=... \
 *   node scripts/e2e-login.mjs
 *
 * On Windows PowerShell:
 *
 *   $env:DEVRIMO_BASE_URL="https://devrimo.ates.digital"
 *   $env:DEVRIMO_TEST_EMAIL="a@a.co"; $env:DEVRIMO_TEST_PASSWORD="..."
 *   node scripts/e2e-login.mjs
 *
 * What it proves, in order:
 *
 *  1. A wrong password is refused, and refused in words a student can act on
 *     rather than Supabase's English.
 *  2. The right password signs in and lands on the application.
 *  3. "?next=" cannot take the browser to another site. This is the one that
 *     matters: /login?next=/\elsewhere passed the old guard, and a browser
 *     resolves that to http://elsewhere/, so a signed-in student was one link
 *     away from someone else's page with their guard down.
 *  4. "?next=/schedule" still goes where it says.
 *  5. Signing in again while signed in does not strand anyone on /login.
 */

import { chromium } from "playwright";

const BASE = (process.env.DEVRIMO_BASE_URL || "http://127.0.0.1:3000").replace(/\/+$/, "");
const EMAIL = process.env.DEVRIMO_TEST_EMAIL;
const PASSWORD = process.env.DEVRIMO_TEST_PASSWORD;

if (!EMAIL || !PASSWORD) {
  console.error("Set DEVRIMO_TEST_EMAIL and DEVRIMO_TEST_PASSWORD. They are never printed.");
  process.exit(2);
}

const results = [];
function check(name, passed, detail = "") {
  results.push({ name, passed, detail });
  console.log(`${passed ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
}

async function signIn(page, { password = PASSWORD, next = null } = {}) {
  const url = next === null ? `${BASE}/login` : `${BASE}/login?next=${encodeURIComponent(next)}`;
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.locator('input[type="email"]').fill(EMAIL);
  await page.locator('input[type="password"]').fill(password);
  await Promise.all([
    page.locator('button[type="submit"]').click(),
    page.waitForLoadState("networkidle").catch(() => {}),
  ]);
  await page.waitForTimeout(2500);
}

const browser = await chromium.launch();
try {
  // 1. A wrong password, in the student's own language.
  {
    const page = await browser.newPage();
    await signIn(page, { password: `${PASSWORD}-definitely-wrong` });
    const alert = await page.locator('[role="alert"]').first().textContent().catch(() => "");
    const stayed = new URL(page.url()).pathname === "/login";
    check("a wrong password is refused and stays on the sign-in page", stayed, page.url());
    check(
      "the refusal is written for a student, not copied from the provider",
      Boolean(alert && !/invalid login credentials/i.test(alert)),
      (alert || "(no message)").trim().slice(0, 80),
    );
    await page.close();
  }

  // 2. The real thing.
  {
    const page = await browser.newPage();
    await signIn(page);
    const path = new URL(page.url()).pathname;
    check("the right password signs in and leaves the sign-in page", path !== "/login", page.url());
    await page.close();
  }

  // 3. The redirect that used to leave the site.
  for (const hostile of ["/\\elsewhere.example", "/\t/elsewhere.example", "//elsewhere.example", "https://elsewhere.example/pay"]) {
    const page = await browser.newPage();
    await signIn(page, { next: hostile });
    const sameSite = new URL(page.url()).origin === new URL(BASE).origin;
    check(`?next=${JSON.stringify(hostile)} cannot leave this site`, sameSite, page.url());
    await page.close();
  }

  // 4. And still goes where it honestly says.
  {
    const page = await browser.newPage();
    await signIn(page, { next: "/schedule" });
    check("?next=/schedule lands on the schedule", new URL(page.url()).pathname === "/schedule", page.url());
    await page.close();
  }

  // 5. Already signed in, asked for the sign-in page.
  {
    const page = await browser.newPage();
    await signIn(page);
    await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    check("a signed-in visit to /login is not a dead end", new URL(page.url()).pathname !== "/login", page.url());
    await page.close();
  }
} finally {
  await browser.close();
}

const failed = results.filter((r) => !r.passed);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
