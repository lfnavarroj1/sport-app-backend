// HU036 (ADR-044 §13): 50 sesiones concurrentes que envían un lote de frecuencia cardiaca cada
// 5 s durante 10 min por `bff-mobile`. Umbrales: p95 < 2 s (HA015/HA016) y < 1 % de lotes
// fallidos (HA008). Solo datos sintéticos.
//
// Requisitos: un ambiente con `bff-mobile`, Usuarios, Afiliación y Entrenamientos, y cuentas
// `athlete` sintéticas con el plan `INTERMEDIATE` (capacidades `devices.measurement.connect` y
// `training.realtime.monitor`). Las credenciales llegan por entorno, nunca en el código:
//   ACCOUNTS='[{"email":"...","password":"..."}, ...]'  (una por sesión; 50 para la prueba)
// Uso:
//   docker run --rm -i -e BASE_URL=http://host.docker.internal:18081 -e ACCOUNTS="$ACCOUNTS" \
//     grafana/k6 run - < load/k6/training_monitoring.js
// Variables opcionales: SESSIONS (50), DURATION (10m), BATCH_SECONDS (5).
import http from "k6/http";
import { check, fail, sleep } from "k6";
import crypto from "k6/crypto";
import exec from "k6/execution";
import { Counter } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://127.0.0.1:18081";
const SESSIONS = Number(__ENV.SESSIONS || 50);
const DURATION = __ENV.DURATION || "10m";
const BATCH_SECONDS = Number(__ENV.BATCH_SECONDS || 5);

const alerts = new Counter("monitoring_alerts");
const noData = new Counter("monitoring_no_data");

export const options = {
  scenarios: {
    monitoring: {
      executor: "constant-vus",
      vus: SESSIONS,
      duration: DURATION,
    },
  },
  thresholds: {
    "http_req_duration{operation:measurements}": ["p(95)<2000"],
    "http_req_failed{operation:measurements}": ["rate<0.01"],
    checks: ["rate>0.99"],
  },
};

function uuid4() {
  const bytes = new Uint8Array(crypto.randomBytes(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (item) => item.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function utc(date) {
  return `${date.toISOString().slice(0, 19)}Z`;
}

function json(method, path, token, body, extraHeaders) {
  const headers = { "Content-Type": "application/json", ...(extraHeaders || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  return http.request(method, `${BASE_URL}${path}`, body === undefined ? null : JSON.stringify(body), {
    headers,
    tags: { operation: path.endsWith("/measurements") ? "measurements" : "setup" },
  });
}

// Cada VU prepara su sesión: inicia sesión, vincula un monitor sintético y empieza la práctica.
export function setup() {
  const accounts = JSON.parse(__ENV.ACCOUNTS || "[]");
  if (accounts.length < SESSIONS) fail(`ACCOUNTS necesita ${SESSIONS} cuentas sintéticas`);
  return accounts.slice(0, SESSIONS).map((account, index) => {
    const login = json("POST", "/v1/sessions", null, account);
    if (login.status !== 200) fail(`login ${index}: ${login.status}`);
    const token = login.json("access_token");
    const deviceId = crypto.sha256(`k6-monitor-${index}-${uuid4()}`, "hex");
    const linked = json("POST", "/v1/me/devices", token, {
      device_id: deviceId,
      display_name: `Monitor sintético ${index}`,
      device_type: "HEART_RATE_MONITOR",
      protocol: "BLE_HRS",
    }, { "Idempotency-Key": uuid4() });
    if (linked.status !== 201 && linked.status !== 200) fail(`device ${index}: ${linked.status}`);
    const sessionId = uuid4();
    const started = json("POST", "/v1/training-sessions", token, {
      session_id: sessionId,
      sport: "running",
      started_at: utc(new Date(Date.now() - 2000)),
    }, { "Idempotency-Key": uuid4() });
    if (started.status !== 201) fail(`session ${index}: ${started.status}`);
    return { token, deviceId, sessionId };
  });
}

export default function (sessions) {
  const session = sessions[(exec.vu.idInTest - 1) % sessions.length];
  const end = Math.floor(Date.now() / 1000);
  const samples = [];
  for (let offset = BATCH_SECONDS - 1; offset >= 0; offset -= 1) {
    samples.push({
      measured_at: utc(new Date((end - offset) * 1000)),
      type: "HEART_RATE",
      value: 120 + Math.floor(Math.random() * 60),
      unit: "bpm",
    });
  }
  const response = json(
    "POST",
    `/v1/training-sessions/${session.sessionId}/measurements`,
    session.token,
    { device_id: session.deviceId, samples },
  );
  check(response, {
    "lote 200": (item) => item.status === 200,
    "con evaluación": (item) => item.status === 200 && item.json("evaluation.status") !== undefined,
  });
  if (response.status === 200) {
    alerts.add(response.json("evaluation.alerts").length);
    if (response.json("evaluation.status") === "NO_DATA") noData.add(1);
  }
  sleep(BATCH_SECONDS);
}

export function teardown(sessions) {
  for (const session of sessions) {
    json("POST", `/v1/training-sessions/${session.sessionId}/finish`, session.token, {
      ended_at: utc(new Date()),
    }, { "Idempotency-Key": uuid4() });
  }
}
