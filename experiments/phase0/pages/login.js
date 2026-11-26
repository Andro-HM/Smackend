import { account, request, showError } from "/assets/browser.js";

const status = document.querySelector("#status");
document.querySelector("#credentials").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const mode = event.submitter?.value || "login";
  const credentials = Object.fromEntries(new FormData(form));
  form.elements.password.value = "";
  try {
    const { response, payload } = await request(account + mode, "POST", credentials);
    if (response.ok && payload?.meta?.is_authenticated) {
      location.assign("/notes/");
    } else {
      status.textContent = showError(response, payload);
    }
  } catch {
    status.textContent = "Could not reach the application.";
  }
});
