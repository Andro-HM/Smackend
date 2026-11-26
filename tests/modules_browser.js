// Run with Playwright CLI against a disposable auth, storage and admin scaffold.
async page => {
  const base = new URL(page.url()).origin;
  if (base !== "http://127.0.0.1:8015") throw new Error("Expected the local module test app.");
  page.setDefaultTimeout(8000);
  await page.goto(base + "/admin/");
  await page.waitForURL(base + "/account/");
  const password = "Browser-operator-481-demo!";
  const login = page.locator("form").filter({ has: page.getByRole("heading", { name: "Sign in with Username" }) });
  await login.getByLabel("Username").fill("browser_admin");
  await login.getByLabel("Password").fill(password);
  await login.getByRole("button", { name: "Sign in with Username" }).click();
  await page.getByText("Signed in as browser_admin").waitFor();
  await page.goto(base + "/admin/");
  await page.waitForURL(base + "/account/");

  const proof = page.locator("form").filter({ has: page.getByRole("heading", { name: "Confirm password" }) });
  await proof.getByLabel("Password").fill(password);
  const confirmed = page.waitForResponse(res => res.url().endsWith("/auth/reauthenticate") && res.status() === 200);
  await proof.getByRole("button", { name: "Confirm password" }).click();
  await confirmed;
  await page.goto(base + "/admin/");
  await page.getByRole("link", { name: "Groups", exact: true }).click();
  await page.getByRole("link", { name: "Add group" }).click();
  await page.getByLabel(/^Name/).fill("Browser reviewers");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await page.locator("#result_list").getByRole("link", { name: "Browser reviewers", exact: true }).waitFor();

  const asset = await page.evaluate(async () => {
    const csrf = document.cookie.split("; ").find(part => part.startsWith("csrftoken="))?.split("=")[1];
    const body = new FormData();
    body.append("files", new File(["Private browser file"], "browser.txt", { type: "text/plain" }));
    const result = await fetch("/api/files/", { method: "POST", headers: {"X-CSRFToken": csrf}, body });
    if (result.status !== 201) throw new Error("File upload failed.");
    return (await result.json())[0].id;
  });
  await page.goto(base + "/admin/media/asset/");
  await page.locator("#result_list").getByText("browser.txt", { exact: true }).waitFor();
  await page.locator("input.action-select").check();
  await page.locator("select[name=action]").selectOption("retire");
  await page.getByRole("button", { name: "Go", exact: true }).click();
  await page.getByText("Retired 1 files.", { exact: false }).waitFor();
  const gone = await page.evaluate(async id => (await fetch("/api/files/" + id)).status, asset);
  if (gone !== 404) throw new Error("Retired file metadata remained accessible.");
  await page.goto(base + "/admin/administration/auditevent/");
  await page.locator("#result_list").getByText("file_retired", { exact: true }).waitFor();
  return "Browser verified admin reauthentication, group creation, private upload and audited retirement.";
}
