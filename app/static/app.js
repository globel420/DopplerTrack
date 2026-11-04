"use strict";

const fileInput = document.getElementById("videoFile");
const dropZone = document.getElementById("dropZone");
const fileSummary = document.getElementById("fileSummary");
const quickButton = document.getElementById("quickButton");
const mediaUrlInput = document.getElementById("mediaUrl");
const urlImportButton = document.getElementById("urlImportButton");
const progressPanel = document.getElementById("progressPanel");
const progressLabel = document.getElementById("progressLabel");
const progressPercent = document.getElementById("progressPercent");
const progressBar = document.getElementById("progressBar");
const errorMessage = document.getElementById("errorMessage");
const deepError = document.getElementById("deepError");
const results = document.getElementById("results");
const deepResults = document.getElementById("deepResults");
let selectedFile = null;
let fileCanScan = false;
let isBusy = false;
let currentJob = null;
let pollTimer = null;
let activeUpload = null;
let currentUploadId = null;
let uploadCancelled = false;
const FILE_HINT = "MP4, MOV, MKV or WebM · up to 4 GiB · up to 1 hour";
const MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024;
const MAX_DURATION_SECONDS = 60 * 60;

const formatBytes = (bytes) => {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KiB`;
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
};

function setStep(step) {
  for (const item of document.querySelectorAll("#stepper li")) {
    const number = Number(item.dataset.step);
    item.classList.toggle("done", number < step);
    if (number === step) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  }
}

function reveal(element) {
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  element.scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "start" });
}

function selectTab(selected) {
  for (const [tab, panel] of [["tabFile", "panelFile"], ["tabLink", "panelLink"]]) {
    const button = document.getElementById(tab);
    const active = tab === selected;
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
    document.getElementById(panel).hidden = !active;
  }
}
for (const id of ["tabFile", "tabLink"]) {
  const tab = document.getElementById(id);
  tab.addEventListener("click", () => selectTab(id));
  tab.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      const other = id === "tabFile" ? "tabLink" : "tabFile";
      selectTab(other);
      document.getElementById(other).focus();
    }
  });
}

function fillOffsets() {
  const select = document.getElementById("recordedOffset");
  const quarter = new Set([345, 525, 765]);
  for (let minutes = -720; minutes <= 840; minutes += 15) {
    if (minutes % 30 !== 0 && !quarter.has(minutes)) continue;
    const sign = minutes < 0 ? "-" : "+";
    const absolute = Math.abs(minutes);
    const value = `${sign}${String(Math.floor(absolute / 60)).padStart(2, "0")}:${String(absolute % 60).padStart(2, "0")}`;
    const option = document.createElement("option");
    option.value = value;
    option.textContent = `UTC${value}`;
    select.append(option);
  }
  const local = -new Date().getTimezoneOffset();
  const sign = local < 0 ? "-" : "+";
  const absolute = Math.abs(local);
  select.value = `${sign}${String(Math.floor(absolute / 60)).padStart(2, "0")}:${String(absolute % 60).padStart(2, "0")}`;
  if (!select.value) select.value = "+00:00";
}
fillOffsets();

const wantLocation = document.getElementById("wantLocation");
wantLocation.addEventListener("change", () => {
  document.getElementById("locationFields").hidden = !wantLocation.checked;
});

function showError(message) {
  errorMessage.textContent = message;
  errorMessage.hidden = false;
}

function clearError() {
  errorMessage.textContent = "";
  errorMessage.hidden = true;
  deepError.textContent = "";
  deepError.hidden = true;
}

function showDeepError(message) {
  deepError.textContent = message;
  deepError.hidden = false;
}

function setProgress(label, value) {
  progressPanel.hidden = false;
  progressLabel.textContent = label;
  progressBar.value = Math.max(0, Math.min(100, value));
  progressPercent.textContent = `${Math.round(progressBar.value)}%`;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "duration unknown";
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function inspectFile(file) {
  selectedFile = file || null;
  fileCanScan = false;
  clearError();
  if (!selectedFile) {
    quickButton.disabled = true;
    fileSummary.textContent = FILE_HINT;
    return;
  }
  const ext = selectedFile.name.split(".").pop().toLowerCase();
  if (!new Set(["mp4", "mov", "mkv", "webm"]).has(ext)) {
    showError("Choose an MP4, MOV, MKV, or WebM video.");
    quickButton.disabled = true;
    return;
  }
  if (selectedFile.size > MAX_FILE_BYTES) {
    showError("This file is larger than the 4 GiB limit.");
    quickButton.disabled = true;
    fileSummary.textContent = `${selectedFile.name} · ${formatBytes(selectedFile.size)} · over size limit`;
    return;
  }
  quickButton.disabled = isBusy;
  fileCanScan = true;
  fileSummary.textContent = `${selectedFile.name} · ${formatBytes(selectedFile.size)} · checking duration…`;
  const objectUrl = URL.createObjectURL(selectedFile);
  const video = document.createElement("video");
  video.preload = "metadata";
  video.onloadedmetadata = () => {
    const duration = video.duration;
    fileSummary.textContent = `${selectedFile.name} · ${formatBytes(selectedFile.size)} · ${formatDuration(duration)}${duration > MAX_DURATION_SECONDS ? " · over 1-hour limit" : ""}`;
    fileCanScan = duration <= MAX_DURATION_SECONDS;
    quickButton.disabled = isBusy || !fileCanScan;
    URL.revokeObjectURL(objectUrl);
  };
  video.onerror = () => {
    fileSummary.textContent = `${selectedFile.name} · ${formatBytes(selectedFile.size)} · duration will be checked on upload`;
    URL.revokeObjectURL(objectUrl);
  };
  video.src = objectUrl;
}

function restoreSourceButtons() {
  isBusy = false;
  urlImportButton.disabled = false;
  quickButton.disabled = !fileCanScan;
}

function parseCoordinates(text) {
  const match = text.trim().match(/^(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)$/);
  if (!match) return null;
  const latitude = Number(match[1]);
  const longitude = Number(match[2]);
  if (Math.abs(latitude) > 90 || Math.abs(longitude) > 180) return null;
  return { latitude: match[1], longitude: match[2] };
}

function appendLocationRequest(body) {
  if (!wantLocation.checked) return true;
  const recorded = document.getElementById("recordedLocal").value;
  const coordinates = parseCoordinates(document.getElementById("centerCoords").value);
  const radius = document.getElementById("searchRadius").value.trim();
  if (!recorded) {
    showDeepError("Enter when the clip started, or turn off location search.");
    return false;
  }
  if (!coordinates) {
    showDeepError("Enter the search center as latitude, longitude, for example 38.5816, -121.4944.");
    return false;
  }
  if (!radius || Number(radius) < 0.5 || Number(radius) > 50) {
    showDeepError("The search radius must be between 0.5 and 50 km.");
    return false;
  }
  if (!document.getElementById("flightLookupConsent").checked) {
    showDeepError("Tick the box to allow the flight lookup, or turn off location search.");
    return false;
  }
  body.append("recording_datetime", `${recorded}${document.getElementById("recordedOffset").value}`);
  body.append("center_lat", coordinates.latitude);
  body.append("center_lon", coordinates.longitude);
  body.append("search_radius_km", radius);
  body.append("time_uncertainty_seconds", document.getElementById("timeUncertainty").value);
  body.append("flight_lookup_consent", "true");
  return true;
}

fileInput.addEventListener("change", () => inspectFile(fileInput.files[0]));
dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropZone.classList.add("dragging");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragging"));
dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropZone.classList.remove("dragging");
  const file = event.dataTransfer.files[0];
  if (file) inspectFile(file);
});
dropZone.addEventListener("keydown", (event) => {
  if (event.target === dropZone && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    fileInput.click();
  }
});

function api(path, options = {}) {
  return fetch(path, { credentials: "same-origin", cache: "no-store", ...options }).then(async (response) => {
    if (!response.ok) {
      let message = `Request failed (${response.status}).`;
      try {
        const body = await response.json();
        if (body.detail) message = body.detail;
      } catch (_) { /* Use the status message. */ }
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return response;
  });
}

function removeUploadSession(uploadId, retries = 8) {
  return api(`/api/uploads/${encodeURIComponent(uploadId)}`, { method: "DELETE" }).catch((error) => {
    if (error.status === 409 && retries > 0) {
      return new Promise((resolve) => setTimeout(resolve, 350)).then(() => removeUploadSession(uploadId, retries - 1));
    }
    throw error;
  });
}

function pollJob() {
  if (!currentJob) return;
  api(`/api/jobs/${encodeURIComponent(currentJob)}`)
    .then((response) => response.json())
    .then((job) => {
      const phase = job.phase === "deep" ? "Running the closer scan" : job.phase === "import" ? "Fetching the video" : "Checking the sound";
      const stage = job.status === "queued" ? "Waiting in line" : phase;
      setProgress(stage, job.progress || (job.status === "queued" ? 0 : 100));
      if (job.status === "completed" || job.status === "failed" || job.status === "cancelled") {
        clearInterval(pollTimer);
        pollTimer = null;
        document.getElementById("cancelButton").hidden = true;
        if (job.status === "failed") {
          showError(job.error || "Analysis failed.");
          document.getElementById("retryButton").hidden = job.attempt >= job.max_attempts;
        } else if (job.status === "cancelled") {
          document.getElementById("retryButton").hidden = true;
          progressLabel.textContent = "Cancelled";
        } else {
          document.getElementById("retryButton").hidden = true;
          clearError();
          progressPanel.hidden = true;
          if (job.phase === "deep") renderDeep(job);
          else renderQuick(job);
        }
        restoreSourceButtons();
      }
    })
    .catch((error) => showError(error.message));
}

quickButton.addEventListener("click", () => {
  if (!selectedFile || !fileCanScan) return;
  const uploadFile = selectedFile;
  clearError();
  results.hidden = true;
  deepResults.hidden = true;
  isBusy = true;
  quickButton.disabled = true;
  urlImportButton.disabled = true;
  setProgress("Uploading video", 0);
  document.getElementById("cancelButton").hidden = false;
  uploadCancelled = false;
  const metadata = { filename: uploadFile.name, size: uploadFile.size, flight_lookup_consent: false };
  api("/api/uploads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(metadata),
  })
    .then((response) => response.json())
    .then(async (session) => {
      currentUploadId = session.id;
      for (let index = 0; index < session.chunk_count; index += 1) {
        if (uploadCancelled) throw new DOMException("Upload cancelled", "AbortError");
        const start = index * session.chunk_size;
        const end = Math.min(uploadFile.size, start + session.chunk_size);
        const chunk = uploadFile.slice(start, end);
        await sendUploadChunk(session, index, chunk, start, end, uploadFile.size);
      }
      if (uploadCancelled) throw new DOMException("Upload cancelled", "AbortError");
      setProgress("Checking the uploaded video", 30);
      document.getElementById("cancelButton").hidden = true;
      const response = await api(`/api/uploads/${encodeURIComponent(session.id)}/complete`, { method: "POST" });
      const job = await response.json();
      currentUploadId = null;
      currentJob = job.id;
      document.getElementById("cancelButton").hidden = false;
      setProgress("Video accepted · waiting in line", 32);
      pollJob();
      pollTimer = setInterval(pollJob, 1400);
    })
    .catch((error) => {
      const uploadId = currentUploadId;
      currentUploadId = null;
      if (uploadId) removeUploadSession(uploadId).catch(() => {});
      if (!uploadCancelled && error.name !== "AbortError") showError(error.message || "Upload failed. Check your connection and try again.");
      restoreSourceButtons();
      progressPanel.hidden = true;
      document.getElementById("cancelButton").hidden = true;
    });
});

function sendUploadChunk(session, index, blob, start, end, totalSize) {
  let attempt = 0;
  const send = () => new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    activeUpload = xhr;
    xhr.open("PUT", `/api/uploads/${encodeURIComponent(session.id)}/chunks/${index}`);
    xhr.withCredentials = true;
    xhr.timeout = 10 * 60 * 1000;
    xhr.setRequestHeader("Content-Range", `bytes ${start}-${end - 1}/${totalSize}`);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        const completed = start + event.loaded;
        setProgress(`Uploading · part ${index + 1} of ${session.chunk_count}`, (completed / totalSize) * 30);
      }
    };
    xhr.onload = () => {
      activeUpload = null;
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else {
        let message = `Upload part failed (${xhr.status}).`;
        try { message = JSON.parse(xhr.responseText).detail || message; } catch (_) { /* Keep status message. */ }
        reject(new Error(message));
      }
    };
    xhr.onerror = () => { activeUpload = null; reject(new Error("Upload connection failed. Check your connection and retry.")); };
    xhr.ontimeout = () => { activeUpload = null; reject(new Error("An upload part timed out. Check your connection and retry.")); };
    xhr.onabort = () => { activeUpload = null; reject(new DOMException("Upload cancelled", "AbortError")); };
    xhr.send(blob);
  });
  const retry = () => send().catch((error) => {
    if (uploadCancelled || error.name === "AbortError" || attempt >= 2) throw error;
    attempt += 1;
    return new Promise((resolve) => setTimeout(resolve, attempt * 800)).then(retry);
  });
  return retry();
}

urlImportButton.addEventListener("click", () => {
  const url = mediaUrlInput.value.trim();
  if (!url) {
    showError("Paste a link to a public video first.");
    return;
  }
  clearError();
  results.hidden = true;
  deepResults.hidden = true;
  isBusy = true;
  quickButton.disabled = true;
  urlImportButton.disabled = true;
  setProgress("Starting the import", 0);
  document.getElementById("cancelButton").hidden = true;
  const form = new FormData();
  form.append("url", url);
  form.append("flight_lookup_consent", "false");
  api("/api/url-import", { method: "POST", body: form })
    .then((response) => response.json())
    .then((job) => {
      currentJob = job.id;
      document.getElementById("cancelButton").hidden = false;
      setProgress("Waiting in line", job.progress || 0);
      pollJob();
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(pollJob, 1400);
    })
    .catch((error) => {
      showError(error.message);
      restoreSourceButtons();
      progressPanel.hidden = true;
    });
});

document.getElementById("cancelButton").addEventListener("click", () => {
  if (currentUploadId || activeUpload) {
    uploadCancelled = true;
    const uploadId = currentUploadId;
    if (activeUpload) activeUpload.abort();
    activeUpload = null;
    currentUploadId = null;
    if (uploadId) removeUploadSession(uploadId).catch(() => {});
    progressPanel.hidden = true;
    document.getElementById("cancelButton").hidden = true;
    restoreSourceButtons();
    return;
  }
  if (!currentJob) return;
  api(`/api/jobs/${encodeURIComponent(currentJob)}/cancel`, { method: "POST" })
    .then(() => { if (pollTimer) pollJob(); })
    .catch((error) => showError(error.message));
});

function addList(parent, items) {
  const list = document.createElement("ul");
  for (const item of items || []) {
    const row = document.createElement("li");
    row.textContent = item;
    list.append(row);
  }
  parent.append(list);
}

function renderQuick(job) {
  const result = job.quick_result;
  if (!result) return;
  results.hidden = false;
  deepResults.hidden = true;
  document.getElementById("step1").hidden = true;
  setStep(2);
  const labels = {
    likely_suitable: ["Promising", "status-suitable", "We found moving tones worth measuring", "They could come from an aircraft, or from something else such as a siren or an engine. The closer scan will measure them."],
    inconclusive: ["Unclear", "status-inconclusive", "Nothing stood out clearly", "A faint aircraft can still be hiding here. You can run the closer scan anyway."],
    unsuitable: ["Not usable", "status-unsuitable", "There's no sound to analyze", "This video has no usable audio track, so it can't be analyzed."],
  };
  const [label, style, headline, description] = labels[result.qualification] || labels.inconclusive;
  const banner = document.getElementById("resultBanner");
  banner.replaceChildren();
  const pill = document.createElement("span");
  pill.className = `status-pill ${style}`;
  pill.textContent = label;
  const title = document.createElement("strong");
  title.textContent = headline;
  const bannerText = document.createElement("p");
  bannerText.textContent = description;
  banner.append(pill, title, bannerText);
  document.getElementById("audioValue").textContent = result.audio_present ? "Found" : "None";
  document.getElementById("audioDetail").textContent = result.audio_present ? `${formatDuration(result.duration_seconds)} long` : "No audio track in this file.";
  document.getElementById("qualityValue").textContent = result.signal_quality?.label || "Unknown";
  const score = result.signal_quality?.score;
  document.getElementById("qualityDetail").textContent = score === undefined ? "" : `${score} of 100. Rates the audio, not whether a plane is present.`;
  document.getElementById("featureValue").textContent = String((result.candidate_features || []).length);
  document.getElementById("featureDetail").textContent = result.candidate_features?.length ? "Not yet confirmed as aircraft." : "None met the threshold.";
  const reasons = document.getElementById("reasonsBlock");
  reasons.replaceChildren();
  addList(reasons, [...(result.reasons || []), ...(result.limitations || [])]);
  const plot = document.getElementById("quickPlot");
  plot.hidden = !result.plot;
  plot.closest("figure").hidden = !result.plot;
  if (result.plot) plot.src = `/api/jobs/${encodeURIComponent(job.id)}/spectrogram?phase=quick`;
  const unsuitable = result.qualification === "unsuitable";
  const inconclusive = result.qualification === "inconclusive";
  document.getElementById("deepCard").hidden = unsuitable;
  document.getElementById("deepButton").disabled = unsuitable;
  document.getElementById("overrideLabel").hidden = !inconclusive;
  document.getElementById("manualOverride").checked = false;
  reveal(results);
}

document.getElementById("deepButton").addEventListener("click", () => {
  if (!currentJob) return;
  clearError();
  const manualOverride = document.getElementById("manualOverride").checked;
  if (!document.getElementById("overrideLabel").hidden && !manualOverride) {
    showDeepError("Tick the box to scan a recording the quick check wasn't sure about.");
    return;
  }
  const body = new URLSearchParams({ manual_override: String(manualOverride) });
  if (!appendLocationRequest(body)) return;
  api(`/api/jobs/${encodeURIComponent(currentJob)}/deep`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  }).then((response) => response.json()).then(() => {
    clearError();
    isBusy = true;
    document.getElementById("deepButton").disabled = true;
    quickButton.disabled = true;
    urlImportButton.disabled = true;
    document.getElementById("cancelButton").hidden = false;
    setProgress("Waiting in line", 0);
    if (pollTimer) clearInterval(pollTimer);
    pollJob();
    pollTimer = setInterval(pollJob, 1400);
  }).catch((error) => showDeepError(error.message));
});

const ANSWERS = {
  not_requested: ["Measured only", "status-inconclusive", "No location search was run", "To search for a location, start over and turn on “Also try to find the location”."],
  needs_input: ["Needs details", "status-inconclusive", "No location search was run", "Enter the recording time and search area."],
  provider_disabled: ["Unavailable", "status-inconclusive", "Flight lookup is turned off on this server", "The server operator can enable it with ADSB_HISTORY_ENABLED."],
  duration_not_supported: ["Too long to match", "status-inconclusive", "Location undetermined", "Flight matching currently handles clips up to 10 minutes. Trim the video around the aircraft sound and try again."],
  insufficient_audio_evidence: ["Not enough sound", "status-inconclusive", "Location undetermined", "No curve was long enough to compare with flights, so nothing was looked up. Try a longer clip with a clearer aircraft."],
  rate_limited: ["Try again soon", "status-inconclusive", "Location undetermined", "The flight service allows one lookup every five minutes. Wait, then start over."],
  provider_unavailable: ["Service problem", "status-inconclusive", "Location undetermined", "The flight service couldn't answer. Try again later."],
  incomplete_data: ["Busy airspace", "status-inconclusive", "Location undetermined", "More flights matched than the 50 the lookup can return, and a partial list can't support a location. A shorter clip or a more exact time may help."],
  no_tracks_returned: ["No flights found", "status-inconclusive", "Location undetermined", "This doesn't prove no aircraft was there: many small planes don't appear in this data. Check the time and area."],
  insufficient_evidence: ["Not enough evidence", "status-inconclusive", "Location undetermined", "A location needs two separate aircraft that fit one spot and one clock. Longer recordings give more chances."],
  experimental_candidate: ["Experimental", "status-suitable", "Possible location found", "Treat this as a lead to check, not an answer. This method has not been validated on real recordings with known locations."],
};

function drawCurves(frame, candidates, duration) {
  frame.querySelector(".plot-overlay")?.remove();
  if (!candidates.length || !(duration > 0)) return;
  const ns = "http://www.w3.org/2000/svg";
  const overlay = document.createElementNS(ns, "svg");
  overlay.setAttribute("class", "plot-overlay");
  overlay.setAttribute("viewBox", "0 0 900 298");
  overlay.setAttribute("aria-hidden", "true");
  const height = 260;
  const span = Math.log(5000 / 100);
  candidates.forEach((item, index) => {
    const points = (item.measurements || [])
      .map(([time, hertz]) => [(time / duration) * 900, height * (1 - Math.log(hertz / 100) / span)])
      .filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y) && y >= 0 && y <= height);
    if (points.length < 2) return;
    const line = document.createElementNS(ns, "polyline");
    line.setAttribute("points", points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" "));
    overlay.append(line);
    const label = document.createElementNS(ns, "text");
    label.setAttribute("x", String(Math.min(880, points[0][0] + 4)));
    label.setAttribute("y", String(Math.max(14, points[0][1] - 6)));
    label.textContent = String(index + 1);
    overlay.append(label);
  });
  frame.append(overlay);
}

function addRow(tbody, values) {
  const row = document.createElement("tr");
  for (const value of values) {
    const cell = document.createElement("td");
    cell.textContent = value;
    row.append(cell);
  }
  tbody.append(row);
}

function renderDeep(job) {
  const result = job.deep_result;
  if (!result) return;
  deepResults.hidden = false;
  results.hidden = true;
  document.getElementById("step1").hidden = true;
  setStep(3);
  const candidates = result.candidate_tracks || [];
  document.getElementById("deepSummary").textContent = candidates.length
    ? `${candidates.length} curve${candidates.length === 1 ? "" : "s"} measured and drawn in blue. A curve is a measurement, not proof of an aircraft.`
    : "No curves met the measurement thresholds.";
  const plot = document.getElementById("deepPlot");
  plot.hidden = !result.plot;
  if (result.plot) plot.src = `/api/jobs/${encodeURIComponent(job.id)}/spectrogram?phase=deep`;
  drawCurves(document.getElementById("deepPlotFrame"), candidates, Number(result.duration_seconds));
  const tbody = document.getElementById("candidateRows");
  tbody.replaceChildren();
  document.getElementById("noCandidates").hidden = candidates.length > 0;
  candidates.forEach((item, index) => addRow(tbody, [
    String(index + 1),
    `${item.start_seconds.toFixed(1)}–${item.end_seconds.toFixed(1)} s`,
    `${item.start_hz.toFixed(1)} → ${item.end_hz.toFixed(1)} Hz`,
    `${item.slope_hz_per_second.toFixed(2)} Hz/s`,
    item.fit_r_squared.toFixed(2),
    `${item.median_prominence_db.toFixed(1)} dB`,
    item.harmonic_relation,
  ]));

  const flightMatching = result.flight_matching || {};
  const [pillText, pillStyle, headline, nextStep] = ANSWERS[flightMatching.status] || ["Unknown", "status-inconclusive", "Location undetermined", ""];
  const pill = document.getElementById("answerPill");
  pill.className = `status-pill ${pillStyle}`;
  pill.textContent = pillText;
  const location = flightMatching.location;
  const hasLocation = location && Number.isFinite(location.latitude) && Number.isFinite(location.longitude);
  document.getElementById("locationHeadline").textContent = hasLocation
    ? `${location.latitude.toFixed(5)}, ${location.longitude.toFixed(5)}`
    : headline;
  document.getElementById("flightStatus").textContent = flightMatching.reason || "";
  const next = document.getElementById("nextStep");
  next.textContent = nextStep;
  next.hidden = !nextStep;
  const facts = document.getElementById("locationFacts");
  facts.replaceChildren();
  facts.hidden = !hasLocation;
  if (hasLocation) {
    const rows = [
      ["Aircraft matched", String(location.matched_aircraft_count ?? "—")],
      ["Search grid spacing", `${location.grid_resolution_km} km (not an accuracy)`],
      ["Recorder clock offset", `${location.recording_clock_offset_seconds} s`],
    ];
    for (const [term, value] of rows) {
      const wrap = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = term;
      const dd = document.createElement("dd");
      dd.textContent = value;
      wrap.append(dt, dd);
      facts.append(wrap);
    }
  }
  const matchTable = document.getElementById("flightMatchTable");
  const matchRows = document.getElementById("flightMatchRows");
  matchRows.replaceChildren();
  const matches = flightMatching.matches || [];
  matchTable.hidden = matches.length === 0;
  for (const item of matches) {
    addRow(matchRows, [
      item.callsign || item.flight_id || "Unknown",
      item.icao_type || "Unknown",
      `Curve ${Number(item.curve_index) + 1}`,
      `${(item.median_error_hz || 0).toFixed(1)} Hz (${((item.median_relative_error || 0) * 100).toFixed(2)}%)`,
      String(item.samples || 0),
    ]);
  }
  const uncertainty = document.getElementById("uncertaintyContent");
  uncertainty.replaceChildren();
  addList(uncertainty, [
    ...(result.assumptions || []),
    ...(result.uncertainty || []),
    ...(flightMatching.limitations || []),
  ]);
  reveal(deepResults);
}

for (const id of ["startOverButton", "startOverQuick"]) {
  document.getElementById(id).addEventListener("click", () => window.location.assign("/"));
}

document.getElementById("exportButton").addEventListener("click", () => {
  if (!currentJob) return;
  api(`/api/jobs/${encodeURIComponent(currentJob)}/report`).then((response) => response.blob()).then((blob) => {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `dopplertrack-report-${currentJob.slice(0, 8)}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
  }).catch((error) => showError(error.message));
});

document.getElementById("retryButton").addEventListener("click", () => {
  if (!currentJob) return;
  api(`/api/jobs/${encodeURIComponent(currentJob)}/retry`, { method: "POST" })
    .then(() => {
      clearError();
      document.getElementById("retryButton").hidden = true;
      document.getElementById("cancelButton").hidden = false;
      setProgress("Waiting in line", 0);
      pollJob();
      pollTimer = setInterval(pollJob, 1400);
    })
    .catch((error) => showError(error.message));
});
