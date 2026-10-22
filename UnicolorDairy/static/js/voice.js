// Voice-note recorder for the "Assign a task" form.
// Records with the microphone and puts the audio into the hidden <input name="voice_note">.
(function () {
  const box = document.getElementById("voiceBox");
  if (!box) return;
  const input = box.querySelector('input[name="voice_note"]');
  const recBtn = box.querySelector("[data-rec]");
  const delBtn = box.querySelector("[data-del]");
  const player = box.querySelector("audio");
  const status = box.querySelector("[data-status]");

  if (!navigator.mediaDevices || !window.MediaRecorder) {
    recBtn.disabled = true;
    status.textContent = "Voice recording isn't supported in this browser. Use Chrome, or attach an audio file instead.";
    return;
  }

  let recorder = null, chunks = [], timer = null, seconds = 0, stream = null;
  const pickType = () =>
    ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].find(t => MediaRecorder.isTypeSupported(t)) || "";
  const extFor = type => (type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm");
  const fmt = s => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

  function setFile(blob, type) {
    const file = new File([blob], `voice-note-${Date.now()}.${extFor(type)}`, { type: type.split(";")[0] || "audio/webm" });
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    player.src = URL.createObjectURL(blob);
    player.hidden = false;
    delBtn.hidden = false;
    status.textContent = `Voice note ready (${fmt(seconds)}). It will be sent with the task.`;
  }

  function clear() {
    input.value = "";
    player.removeAttribute("src");
    player.hidden = true;
    delBtn.hidden = true;
    status.textContent = "";
  }

  async function start() {
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      status.textContent = "Microphone permission was blocked. Allow the microphone in the browser's address bar and try again.";
      return;
    }
    const type = pickType();
    recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    chunks = [];
    recorder.ondataavailable = e => e.data.size && chunks.push(e.data);
    recorder.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
      clearInterval(timer);
      setFile(new Blob(chunks, { type: recorder.mimeType || type }), recorder.mimeType || type || "audio/webm");
      recBtn.textContent = "🎙 Re-record";
      recBtn.classList.remove("recording");
    };
    clear();
    recorder.start();
    seconds = 0;
    status.textContent = "Recording… 0:00";
    timer = setInterval(() => {
      seconds += 1;
      status.textContent = `Recording… ${fmt(seconds)}`;
      if (seconds >= 300) stop(); // 5 minute limit
    }, 1000);
    recBtn.textContent = "■ Stop";
    recBtn.classList.add("recording");
  }

  function stop() {
    if (recorder && recorder.state === "recording") recorder.stop();
  }

  recBtn.addEventListener("click", () => (recorder && recorder.state === "recording" ? stop() : start()));
  delBtn.addEventListener("click", clear);
})();
