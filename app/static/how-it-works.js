"use strict";

// Interactive pitch-curve demo. Everything runs in the browser; nothing is sent anywhere.
const SPEED_OF_SOUND = 340;
const DURATION = 240;
const speedInput = document.getElementById("demoSpeed");
const distanceInput = document.getElementById("demoDistance");
const toneInput = document.getElementById("demoTone");
const curve = document.getElementById("demoCurve");
const topLabel = document.getElementById("demoTop");
const bottomLabel = document.getElementById("demoBottom");
const summary = document.getElementById("demoSummary");
const playButton = document.getElementById("demoPlay");

function pitchAt(time, tone, speed, distance) {
  // Sound heard at `time` left the aircraft earlier; refine that emission time.
  const heard = time - DURATION / 2;
  let emitted = heard;
  for (let step = 0; step < 6; step += 1) {
    emitted = heard - Math.hypot(speed * emitted, distance) / SPEED_OF_SOUND;
  }
  const along = speed * emitted;
  const receding = (speed * along) / Math.hypot(along, distance);
  return (tone * SPEED_OF_SOUND) / (SPEED_OF_SOUND + receding);
}

function values() {
  return {
    speed: Number(speedInput.value),
    distance: Number(distanceInput.value) * 1000,
    tone: Number(toneInput.value),
  };
}

function draw() {
  const { speed, distance, tone } = values();
  document.getElementById("demoSpeedOut").textContent = `${Math.round(speed * 1.944)} knots`;
  document.getElementById("demoDistanceOut").textContent = `${(distance / 1000).toFixed(1)} km`;
  document.getElementById("demoToneOut").textContent = `${tone} Hz`;
  const samples = [];
  for (let index = 0; index <= 200; index += 1) {
    samples.push(pitchAt((index / 200) * DURATION, tone, speed, distance));
  }
  const high = tone * 1.45;
  const low = tone * 0.72;
  const points = samples.map((hertz, index) => {
    const x = 40 + (index / 200) * 610;
    const y = 12 + (1 - (hertz - low) / (high - low)) * 190;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  curve.setAttribute("points", points.join(" "));
  topLabel.textContent = `${Math.round(high)} Hz`;
  bottomLabel.textContent = `${Math.round(low)} Hz`;
  const first = samples[0];
  const last = samples[samples.length - 1];
  summary.textContent = `Pitch falls from ${first.toFixed(0)} Hz to ${last.toFixed(0)} Hz. Faster planes fall further. Closer planes fall more suddenly.`;
}

let audio = null;
function stopSound() {
  if (audio) {
    audio.close();
    audio = null;
  }
  playButton.textContent = "Play this flyover";
}

playButton.addEventListener("click", () => {
  if (audio) {
    stopSound();
    return;
  }
  const { speed, distance, tone } = values();
  const AudioEngine = window.AudioContext || window.webkitAudioContext;
  if (!AudioEngine) return;
  audio = new AudioEngine();
  const seconds = 8;
  const pitches = new Float32Array(200);
  const loudness = new Float32Array(200);
  for (let index = 0; index < 200; index += 1) {
    const time = (index / 199) * DURATION;
    pitches[index] = pitchAt(time, tone, speed, distance);
    const range = Math.hypot(speed * (time - DURATION / 2), distance);
    loudness[index] = 0.04 + 0.2 * Math.min(1, distance / range);
  }
  const oscillator = audio.createOscillator();
  oscillator.type = "sawtooth";
  const gain = audio.createGain();
  const start = audio.currentTime + 0.05;
  oscillator.frequency.setValueCurveAtTime(pitches, start, seconds);
  gain.gain.setValueCurveAtTime(loudness, start, seconds);
  oscillator.connect(gain).connect(audio.destination);
  oscillator.start(start);
  oscillator.stop(start + seconds);
  oscillator.onended = stopSound;
  playButton.textContent = "Stop";
});

for (const input of [speedInput, distanceInput, toneInput]) input.addEventListener("input", draw);
draw();
