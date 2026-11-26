"use strict";

const routes = "/identity/browser/v1/";
const forms = document.querySelector("#forms");
const status = document.querySelector("#session");
const result = document.querySelector("#result");

function csrf() {
  return document.cookie.split("; ").find(value => value.startsWith("csrftoken="))?.split("=")[1] ?? "";
}

async function request(url, method = "GET", data) {
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const json = response.headers.get("Content-Type")?.includes("application/json") ? await response.json() : {};
  return { response, json };
}

function button(label, callback) {
  const element = document.createElement("button");
  element.type = "button";
  element.textContent = label;
  element.onclick = callback;
  return element;
}

const labels = { username: "Username", email: "Email", phone: "Phone (E.164)", password: "Password" };

function field(name, required = true) {
  const label = document.createElement("label");
  label.textContent = labels[name] ?? name.replaceAll("_", " ");
  const input = document.createElement("input");
  input.name = name;
  input.required = required;
  input.type = name.includes("password") ? "password" : name === "email" ? "email" : name === "phone" ? "tel" : "text";
  if (name === "code" || name === "key") input.autocomplete = "one-time-code";
  label.append(input);
  return label;
}

function form(label, fields, url, method = "POST") {
  const element = document.createElement("form");
  const heading = document.createElement("h2");
  heading.textContent = label;
  element.append(heading);
  for (const item of fields) {
    element.append(typeof item === "string" ? field(item) : field(item.name, item.required));
  }
  const submit = document.createElement("button");
  submit.textContent = label;
  element.append(submit);
  element.onsubmit = async event => {
    event.preventDefault();
    await act(url, method, Object.fromEntries(new FormData(element)));
  };
  forms.append(element);
}

async function act(url, method, data) {
  try {
    const {response, json} = await request(url, method, data);
    result.textContent = json.errors?.map(error => error.message).join(" ")
      ?? json.message ?? (response.ok || json.data?.flows ? "Request processed." : "Request failed.");
    await render(json.data?.flows ?? []);
  } catch {
    result.textContent = "Account service unavailable.";
  }
}

function google(label, process) {
  const element = document.createElement("form");
  element.method = "post";
  element.action = routes + "auth/provider/redirect";
  for (const [name, value] of Object.entries({
    provider: "google", process, callback_url: "/account/", csrfmiddlewaretoken: csrf(),
  })) {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    element.append(input);
  }
  const submit = document.createElement("button");
  submit.textContent = label;
  element.append(submit);
  forms.append(element);
}

async function render(recentFlows = []) {
  const {json: profile} = await request("/api/account/capabilities");
  const {json: session} = await request(routes + "auth/session");
  const loggedIn = session.meta?.is_authenticated;
  const flow = recentFlows.find(item => item.is_pending) ?? session.data?.flows?.find(item => item.is_pending);
  status.textContent = loggedIn ? "Signed in as " + session.data?.user?.display : "Signed out";
  forms.replaceChildren();
  if (loggedIn) {
    forms.append(button("Sign out", () => act(routes + "auth/session", "DELETE")));
    form("Update profile", ["first_name", "last_name"], "/api/account/profile", "PATCH");
    if (profile.password_login.length) {
      form("Change password", [{name: "current_password", required: false}, "new_password"], routes + "account/password/change");
      form("Confirm password", ["password"], routes + "auth/reauthenticate");
    }
    for (const channel of ["email", "phone"].filter(channel => profile.signup.some(item => item.name === channel))) {
      form("Request " + channel + " identity confirmation", [channel], routes + "auth/reauthenticate/code/request");
      form("Change " + channel, [channel], routes + "account/" + channel);
    }
    form("Confirm identity code", ["code"], routes + "auth/reauthenticate/code/confirm");
    if (profile.google) google("Connect Google", "connect");
    forms.append(button("Deactivate account", () => {
      if (window.confirm("Deactivate this account and end its sessions?")) act("/api/account/deactivate", "POST");
    }));
  } else {
    if (profile.allow_signup && !flow) form("Register", profile.signup, routes + "auth/signup");
    for (const identifier of profile.password_login) {
      form("Sign in with " + labels[identifier], [identifier, "password"], routes + "auth/login");
    }
    for (const channel of profile.otp) form("Send " + channel + " login code", [channel], routes + "auth/code/request");
    if (profile.otp.length) form("Confirm login code", ["code"], routes + "auth/code/confirm");
    if (profile.reset) {
      form("Request password reset", ["email"], routes + "auth/password/request");
      form("Reset password", ["key", "password"], routes + "auth/password/reset");
    }
    if (profile.google) google("Sign in with linked Google account", "login");
  }
  for (const channel of ["email", "phone"]) {
    if (profile.signup.some(item => item.name === channel)) {
      form("Verify " + channel, [channel === "email" ? "key" : "code"], routes + "auth/" + channel + "/verify");
    }
  }
}

render().then(() => {
  const key = new URLSearchParams(location.hash.slice(1)).get("verify-email");
  if (key) {
    history.replaceState(null, "", location.pathname);
    act(routes + "auth/email/verify", "POST", {key});
  }
}).catch(() => { result.textContent = "Account service unavailable."; });
