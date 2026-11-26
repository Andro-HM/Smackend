import { account, request, showError } from "/assets/browser.js";

const editor = document.querySelector("#editor");
const notes = document.querySelector("#notes");
const status = document.querySelector("#status");
let editing = null;

function resetEditor() {
  editing = null;
  editor.reset();
}

async function call(path, method, data) {
  const { response, payload } = await request(path, method, data);
  if (response.status === 401) {
    location.replace("/");
    throw new Error("Please sign in.");
  }
  if (!response.ok) throw new Error(showError(response, payload));
  return payload;
}

async function act(operation) {
  try {
    await operation();
  } catch (error) {
    status.textContent = error.message;
  }
}

async function refresh() {
  const records = await call("/api/notes/");
  notes.replaceChildren();
  for (const note of records) {
    const item = document.createElement("li");
    item.textContent = `${note.title}\n${note.body}\n`;
    const edit = document.createElement("button");
    edit.textContent = "Edit";
    edit.onclick = () => {
      editing = note.id;
      editor.elements.title.value = note.title;
      editor.elements.body.value = note.body;
      editor.elements.title.focus();
    };
    const remove = document.createElement("button");
    remove.textContent = "Delete";
    remove.onclick = () => act(async () => {
      await call(`/api/notes/${note.id}`, "DELETE");
      if (editing === note.id) resetEditor();
      await refresh();
    });
    item.append(edit, remove);
    notes.append(item);
  }
}

editor.addEventListener("submit", event => {
  event.preventDefault();
  act(async () => {
    const payload = Object.fromEntries(new FormData(editor));
    if (editing === null) await call("/api/notes/", "POST", payload);
    else await call(`/api/notes/${editing}`, "PUT", payload);
    resetEditor();
    await refresh();
  });
});
document.querySelector("#cancel").onclick = resetEditor;
document.querySelector("#logout").onclick = () => act(async () => {
  const { response, payload } = await request(account + "session", "DELETE");
  if (response.ok || response.status === 401) location.replace("/");
  else throw new Error(showError(response, payload));
});
document.querySelector("#provider").onclick = () => act(async () => {
  const result = await call("/api/provider");
  status.textContent = result.results.join(", ");
});
act(refresh);
