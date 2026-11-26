"use strict";

const element = id => document.getElementById(id);
const csrf = () => document.cookie.split("; ").find(item => item.startsWith("csrftoken="))?.split("=")[1] ?? "";
const wsUrl = path => (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path;

let history = [], pending;
function attachment(file) {
  if (file.size > 1400000) throw new Error("Keep the example attachment under 1.4 MB.");
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("The attachment could not be read."));
    reader.onload = () => resolve({mime: file.type, data: reader.result.split(",")[1]});
    reader.readAsDataURL(file);
  });
}

async function streamText(response, answer) {
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let pendingLine = "", text = "", complete = false;
  try {
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      pendingLine += value;
      const lines = pendingLine.split("\n");
      pendingLine = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const event = JSON.parse(line.slice(6));
        if (event.message) throw new Error(event.message);
        if (event.done) complete = true;
        if (event.text) {
          text += event.text;
          answer.textContent = text;
        }
      }
    }
    if (!complete) throw new Error("The response was interrupted.");
    return text;
  } finally {
    await reader.cancel().catch(() => {});
  }
}

if (element("chat")) {
  element("cancel").onclick = () => pending?.abort();
  element("chat").onsubmit = async event => {
    event.preventDefault();
    if (pending) return;
    const answer = element("answer");
    answer.textContent = "";
    const controller = new AbortController();
    pending = controller;
    try {
      await fetch("/identity/browser/v1/auth/session", {signal: controller.signal});
      const upload = element("attachment").files[0];
      const user = {role: "user", text: element("prompt").value};
      const messages = [...history.slice(-18), user];
      const stream = element("stream").checked;
      const response = await fetch("/api/ai/" + (stream ? "stream" : "chat"), {
        method: "POST", signal: controller.signal,
        headers: {"Content-Type": "application/json", "X-CSRFToken": csrf()},
        body: JSON.stringify({idempotency_key: crypto.randomUUID(), messages,
          attachment: upload ? await attachment(upload) : null}),
      });
      if (!response.ok) throw new Error((await response.json()).message ?? "The request failed.");
      const text = stream ? await streamText(response, answer) : (await response.json()).text;
      history = [...messages, {role: "assistant", text}];
      answer.textContent = text;
    } catch (error) {
      answer.textContent = error.name === "AbortError" ? "Cancelled." : error.message;
    } finally {
      pending = undefined;
    }
  };
}

if (element("echo")) {
  let echo, heartbeat;
  element("echo").onsubmit = event => {
    event.preventDefault();
    const answer = element("echo-answer");
    const message = JSON.stringify({type: "echo", text: element("echo-text").value});
    if (echo?.readyState === WebSocket.OPEN) return echo.send(message);
    if (echo?.readyState === WebSocket.CONNECTING) return;
    echo = new WebSocket(wsUrl("/ws/realtime/"));
    echo.onopen = () => {
      echo.send(message);
      heartbeat = setInterval(() => echo.send('{"type":"ping"}'), 15000);
    };
    echo.onmessage = event => {
      const result = JSON.parse(event.data);
      if (result.type === "echo") answer.textContent = result.text;
    };
    echo.onclose = () => {
      clearInterval(heartbeat);
      answer.textContent = "Disconnected. Send a new message to reconnect.";
    };
  };
}

if (element("start-voice")) {
  let socket, input, media, processor, player, heartbeat, next = 0, rate = 24000;
  const sources = new Set();
  const status = element("voice-status");
  function stopInput() {
    processor?.disconnect();
    media?.getTracks().forEach(track => track.stop());
    input?.close().catch(() => {});
    input = media = processor = undefined;
  }
  function stopPlayback() {
    for (const source of sources) source.stop();
    sources.clear();
    next = 0;
  }
  function play(data) {
    if (player.state === "closed") return;
    if (next - player.currentTime > 0.3) stopPlayback();
    const pcm = new DataView(data);
    const buffer = player.createBuffer(1, pcm.byteLength / 2, rate);
    const samples = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) samples[i] = pcm.getInt16(i * 2, true) / 32768;
    const source = player.createBufferSource();
    source.buffer = buffer;
    source.connect(player.destination);
    source.onended = () => sources.delete(source);
    sources.add(source);
    next = Math.max(player.currentTime + 0.025, next);
    source.start(next);
    next += buffer.duration;
  }
  element("start-voice").onclick = async () => {
    if (socket && socket.readyState < WebSocket.CLOSING) return;
    try {
      player = new AudioContext();
      await player.resume();
      socket = new WebSocket(wsUrl("/ws/voice/"));
      socket.binaryType = "arraybuffer";
      socket.onopen = () => {
        socket.send(JSON.stringify({type: "start", key: crypto.randomUUID()}));
        heartbeat = setInterval(() => socket.send('{"type":"ping"}'), 15000);
      };
      socket.onmessage = async event => {
        if (typeof event.data !== "string") return play(event.data);
        const value = JSON.parse(event.data);
        if (value.type === "error") {
          status.textContent = "Voice is unavailable.";
          socket.close();
        }
        if (value.type === "interrupted") stopPlayback();
        if (value.type !== "voice_ready") return;
        rate = value.sample_rate;
        try {
          media = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true}});
          if (socket.readyState !== WebSocket.OPEN) return stopInput();
          input = new AudioContext({sampleRate: 16000});
          if (input.sampleRate !== 16000) throw new Error("This browser cannot capture 16 kHz audio.");
          processor = input.createScriptProcessor(256, 1, 1);
          input.createMediaStreamSource(media).connect(processor);
          processor.connect(input.destination);
          processor.onaudioprocess = event => {
            if (socket.readyState !== WebSocket.OPEN) return;
            if (socket.bufferedAmount > 16384) return socket.close(4013);
            const samples = event.inputBuffer.getChannelData(0);
            const pcm = new DataView(new ArrayBuffer(samples.length * 2));
            for (let i = 0; i < samples.length; i++) pcm.setInt16(i * 2, Math.max(-1, Math.min(1, samples[i])) * 32767, true);
            socket.send(pcm.buffer);
          };
          status.textContent = "Microphone connected.";
        } catch (error) {
          status.textContent = error.message;
          socket.close();
        }
      };
      socket.onclose = () => {
        clearInterval(heartbeat);
        stopInput();
        stopPlayback();
        player?.close().catch(() => {});
        status.textContent = "Voice disconnected.";
      };
    } catch (error) {
      status.textContent = error.message;
    }
  };
  element("end-voice").onclick = () => {
    stopInput();
    if (socket?.readyState === WebSocket.OPEN) socket.send('{"type":"end"}');
  };
  element("stop-voice").onclick = () => socket?.close();
}
