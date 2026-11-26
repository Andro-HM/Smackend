// Run with Playwright CLI on the combined offline scaffold.
async page => {
  const host = new URL(page.url()).origin;
  if (host !== "http://127.0.0.1:8016") throw new Error("Expected the disposable Phase 3 application.");
  page.setDefaultTimeout(12000);
  await page.goto(host + "/account/");
  const form = page.locator("form").filter({has: page.getByRole("heading", {name: "Register", exact: true})});
  await form.getByLabel("Username").fill("phase3_browser");
  await form.getByLabel("Password").fill("Synthetic!mountain-bridge-672");
  await form.getByRole("button", {name: "Register", exact: true}).click();
  await page.getByText("Signed in as phase3_browser").waitFor();
  await page.goto(host + "/assist/");
  const chat = page.locator("#chat");
  await chat.getByLabel("Message", {exact: true}).fill("ordinary response");
  await chat.getByRole("button", {name: "Send", exact: true}).click();
  await page.getByText("Offline response: ordinary response", {exact: true}).waitFor();
  await chat.getByLabel("Stream the response").check();
  await chat.getByLabel("Optional image or PDF").setInputFiles({
    name: "fixture.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7 synthetic"),
  });
  await chat.getByLabel("Message", {exact: true}).fill("attachment response");
  await chat.getByRole("button", {name: "Send", exact: true}).click();
  await page.getByText("Offline response: attachment response [1 attachments]", {exact: true}).waitFor();
  await page.getByLabel("WebSocket message").fill("Point-to-point browser");
  await page.getByRole("button", {name: "Send over WebSocket"}).click();
  await page.getByText("Point-to-point browser", {exact: true}).waitFor();

  const pcm = await page.evaluate(() => new Promise((resolve, reject) => {
    const socket = new WebSocket("ws://" + location.host + "/ws/voice/");
    socket.binaryType = "arraybuffer";
    const timeout = setTimeout(() => { socket.close(); reject(new Error("Timed out waiting for mock PCM.")); }, 5000);
    socket.onopen = () => socket.send(JSON.stringify({type: "start", key: crypto.randomUUID()}));
    socket.onmessage = message => {
      if (typeof message.data === "string") {
        if (JSON.parse(message.data).type === "voice_ready") socket.send(new Int16Array([42, -42]));
        return;
      }
      const samples = Array.from(new Int16Array(message.data));
      clearTimeout(timeout);
      socket.close();
      resolve(samples);
    };
  }));
  if (pcm.join(",") !== "42,-42") throw new Error("The browser did not receive its PCM frame.");
  return "Browser verified chat, attachment streaming, WebSocket echo and synthetic voice.";
}
