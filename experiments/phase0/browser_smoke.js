// Run with Playwright CLI's run-code --filename=browser_smoke.js.
async page => {
  page.setDefaultTimeout(5000);
  const base = new URL(page.url()).origin;
  if (base !== "http://127.0.0.1:8010") throw new Error("Expected the local Phase 0 app.");
  const username = "browser" + Date.now();
  const password = "Synthetic!purple-bridge-419";
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Register" }).click();
  await page.waitForURL(base + "/notes/");
  await page.getByLabel("Title").fill("Browser smoke note");
  await page.getByLabel("Body").fill("<b>Text stays plain</b>");
  await page.getByRole("button", { name: "Save note" }).click();
  await page.getByText("<b>Text stays plain</b>", { exact: false }).waitFor();
  await page.reload();
  await page.getByText("Browser smoke note", { exact: false }).waitFor();
  await page.getByRole("link", { name: "Account page" }).click();
  await page.getByRole("link", { name: "Continue to notes" }).click();
  await page.getByText("Browser smoke note", { exact: false }).waitFor();
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByLabel("Title").fill("Edited browser note");
  await page.getByRole("button", { name: "Save note" }).click();
  await page.getByText("Edited browser note", { exact: false }).waitFor();
  await page.getByRole("button", { name: "Delete" }).click();
  await page.getByText("Edited browser note", { exact: false }).waitFor({ state: "hidden" });
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.waitForURL(base + "/");
  await page.goto(base + "/notes/");
  await page.waitForURL(base + "/");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(base + "/notes/");
  await page.getByRole("heading", { name: "My private notes" }).waitFor();
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.waitForURL(base + "/");
  return "Browser smoke passed: register, navigate, refresh, CRUD, logout, reject and log in again.";
}
