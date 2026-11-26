// Run with playwright-cli run-code --filename=tests/identity_browser.js.
async page => {
  const origin = new URL(page.url()).origin;
  if (origin !== "http://127.0.0.1:8014") throw new Error("Expected the disposable local identity app.");
  const username = "browser" + Date.now();
  const password = "Synthetic!purple-bridge-419";
  page.setDefaultTimeout(8000);
  await page.getByRole("link", { name: "Account", exact: true }).click();
  const register = page.locator("form").filter({ has: page.getByRole("heading", { name: "Register", exact: true }) });
  await register.getByLabel("Username").fill(username);
  await register.getByLabel("Password").fill(password);
  await register.getByRole("button", { name: "Register", exact: true }).click();
  await page.getByText("Signed in as " + username).waitFor();
  await page.getByRole("link", { name: "Home" }).click();
  await page.getByRole("link", { name: "Account", exact: true }).click();
  await page.getByText("Signed in as " + username).waitFor();
  await page.reload();
  await page.getByText("Signed in as " + username).waitFor();
  const blocked = await page.evaluate(async () => (await fetch("/api/account/profile", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({first_name: "Unauthenticated", last_name: "Mutation"}),
  })).status);
  if (blocked !== 403) throw new Error("Expected CSRF rejection.");
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByText("Signed out", { exact: true }).waitFor();
  const login = page.locator("form").filter({ has: page.getByRole("heading", { name: "Sign in with Username" }) });
  await login.getByLabel("Username").fill(username);
  await login.getByLabel("Password").fill(password);
  await login.getByRole("button", { name: "Sign in with Username" }).click();
  await page.getByText("Signed in as " + username).waitFor();
  return "Browser passed registration, navigation, refresh, CSRF rejection, logout and login.";
}
