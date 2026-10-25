// HU039 / HA021 (ADR-045 §12): ramp-up de 10 a 700 publicaciones/alertas en 5 min; criterio:
// al menos el 70 % de las alertas disponibles en la bandeja en < 4 s, medido desde la solicitud
// de publicación hasta que `GET /v1/me/notifications` (`bff-mobile`) devuelve la notificación
// `EVENT_PUBLISHED_MATCH` de ese `event_id`. También reporta retraso, errores y duplicados. La
// entrega push no se mide aquí. Solo datos sintéticos; fuera de cada PR. El HPA del worker de
// notificaciones y del proyector es de la plataforma y debe estar activo en el ambiente medido.
//
// Supuesto de la forma de carga: `ramping-arrival-rate` de START_RATE (10) a PEAK_RATE (700)
// iteraciones por TIME_UNIT (1m) durante RAMP_DURATION (5m). Cada iteración crea y publica un
// evento de running cuya ciudad coincide con un solo deportista sintético, así que cada
// publicación genera exactamente una alerta (publicaciones = alertas) y la bandeja de cada
// destinatario recibe pocas por segundo. Las ciudades son únicas por corrida
// (`K6 HA021 <corrida> <n>`) para que las publicaciones no lleguen a otros deportistas.
//
// Requisitos: un ambiente con `bff-web`, `bff-mobile`, Usuarios (API, outbox y proyector),
// Eventos (API, outbox, worker de notificaciones y proyector) y Pub/Sub. Nunca producción.
// Uso:
//   docker run --rm -i -e BFF_WEB_URL=http://host.docker.internal:8000 \
//     -e BFF_MOBILE_URL=http://host.docker.internal:18081 \
//     grafana/k6 run - < load/k6/notifications_ha021.js
// (con k6 instalado: `k6 run -e BFF_WEB_URL=... -e BFF_MOBILE_URL=... load/k6/notifications_ha021.js`)
//
// Preparación (en `setup()`, sin medir): registra RECIPIENTS deportistas por `bff-web`
// `POST /v1/users`, inicia sesión por `bff-mobile` `POST /v1/sessions` (el token sirve en ambos
// BFF), guarda su perfil demográfico (su ciudad, CO) y sus hábitos (running) por `bff-web`, espera
// a que `GET /v1/me/profile` muestre ambas secciones y registra un organizador sintético (o usa
// ORGANIZER_EMAIL/ORGANIZER_PASSWORD). El access token dura 15 min (HU003): la preparación y la
// corrida completa deben caber en ese tiempo; las sesiones se renuevan al final de `setup()`.
//
// Variables opcionales: RECIPIENTS (50), START_RATE (10), PEAK_RATE (700), TIME_UNIT (1m),
// RAMP_DURATION (5m), PRE_VUS (100), MAX_VUS (400), MAX_WAIT_SECONDS (30), POLL_MS (250),
// AVAILABLE_MS (4000), REGISTRATION_POLICIES_VERSION (local-dev), BATCH (25),
// PROJECTION_TIMEOUT_SECONDS (300), SETUP_TIMEOUT (15m).
// Umbrales: `available_within_4s` rate >= 0.70 (HA021); error < 1 % y cero duplicados (CA3),
// como supuestos de esta prueba.
import http from "k6/http";
import { fail, sleep } from "k6";
import crypto from "k6/crypto";
import exec from "k6/execution";
import { Counter, Rate, Trend } from "k6/metrics";

const BFF_WEB_URL = __ENV.BFF_WEB_URL || "http://127.0.0.1:8000";
const BFF_MOBILE_URL = __ENV.BFF_MOBILE_URL || "http://127.0.0.1:18081";
const RECIPIENTS = Number(__ENV.RECIPIENTS || 50);
const START_RATE = Number(__ENV.START_RATE || 10);
const PEAK_RATE = Number(__ENV.PEAK_RATE || 700);
const TIME_UNIT = __ENV.TIME_UNIT || "1m";
const RAMP_DURATION = __ENV.RAMP_DURATION || "5m";
const MAX_WAIT_SECONDS = Number(__ENV.MAX_WAIT_SECONDS || 30);
const POLL_MS = Number(__ENV.POLL_MS || 250);
const AVAILABLE_MS = Number(__ENV.AVAILABLE_MS || 4000);
const POLICIES_VERSION = __ENV.REGISTRATION_POLICIES_VERSION || "local-dev";
const BATCH = Number(__ENV.BATCH || 25);
const PROJECTION_TIMEOUT_SECONDS = Number(__ENV.PROJECTION_TIMEOUT_SECONDS || 300);
const COUNTRY_CODE = "CO";
const MATCH = "EVENT_PUBLISHED_MATCH";

const delay = new Trend("notification_delay", true);
const withinTarget = new Rate("available_within_4s");
const errors = new Rate("notification_errors");
const duplicates = new Counter("notification_duplicates");
const available = new Counter("notifications_available");

export const options = {
  setupTimeout: __ENV.SETUP_TIMEOUT || "15m",
  scenarios: {
    alerts: {
      executor: "ramping-arrival-rate",
      startRate: START_RATE,
      timeUnit: TIME_UNIT,
      preAllocatedVUs: Number(__ENV.PRE_VUS || 100),
      maxVUs: Number(__ENV.MAX_VUS || 400),
      stages: [{ target: PEAK_RATE, duration: RAMP_DURATION }],
      gracefulStop: `${MAX_WAIT_SECONDS + 10}s`,
    },
  },
  thresholds: {
    available_within_4s: ["rate>=0.70"],
    notification_errors: ["rate<0.01"],
    notification_duplicates: ["count==0"],
  },
};

function uuid4() {
  const bytes = new Uint8Array(crypto.randomBytes(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (item) => item.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// Petición para `http.batch`; la clave de idempotencia es nueva en cada comando.
function request(method, baseUrl, path, token, body, operation, idempotent) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (idempotent) headers["Idempotency-Key"] = uuid4();
  return {
    method,
    url: `${baseUrl}${path}`,
    body: body === undefined ? null : JSON.stringify(body),
    params: { headers, tags: { operation } },
  };
}

function send(item) {
  return http.request(item.method, item.url, item.body, item.params);
}

// Ejecuta en lotes de BATCH y falla la preparación ante un estado no esperado.
function inBatches(items, build, expected, label) {
  const responses = [];
  for (let start = 0; start < items.length; start += BATCH) {
    const chunk = items.slice(start, start + BATCH);
    const replies = http.batch(chunk.map(build));
    replies.forEach((reply, offset) => {
      if (!expected.includes(reply.status)) fail(`${label} ${start + offset}: ${reply.status}`);
    });
    responses.push(...replies);
  }
  return responses;
}

function register(account, actorType, fullName) {
  return request("POST", BFF_WEB_URL, "/v1/users", null, {
    full_name: fullName,
    email: account.email,
    password: account.password,
    actor_type: actorType,
    accepted_policies_version: POLICIES_VERSION,
  }, "setup", true);
}

function login(account) {
  return request("POST", BFF_MOBILE_URL, "/v1/sessions", null, account, "setup", false);
}

function loginAll(accounts) {
  return inBatches(accounts, login, [200], "login").map((reply) => reply.json("access_token"));
}

function prepareAthletes(accounts) {
  inBatches(accounts, (account) => register(account, "athlete", "Deportista Sintético"), [201], "registro");
  const tokens = loginAll(accounts);
  inBatches(accounts.map((account, index) => ({ ...account, token: tokens[index] })), (account) =>
    request("PUT", BFF_WEB_URL, "/v1/me/demographic-profile", account.token, {
      birth_date: "1990-05-10",
      gender: "PREFER_NOT_TO_SAY",
      country_code: COUNTRY_CODE,
      city: account.city,
    }, "setup", true), [200, 201], "perfil demográfico");
  inBatches(tokens, (token) => request("PUT", BFF_WEB_URL, "/v1/me/sport-habits", token, {
    sports: [{
      sport: "running",
      frequency_per_week: 3,
      session_duration_minutes: 45,
      intensity: "MODERATE",
      level: "INTERMEDIATE",
      years_practicing: 2,
    }],
    availability: { days: ["SAT"], time_slots: ["MORNING"] },
    primary_goal: "HEALTH",
  }, "setup", true), [200, 201], "hábitos deportivos");
  // La búsqueda de destinatarios lee la proyección: se espera a que muestre ambas secciones.
  let pending = tokens;
  const deadline = Date.now() + PROJECTION_TIMEOUT_SECONDS * 1000;
  while (pending.length > 0) {
    if (Date.now() > deadline) fail(`${pending.length} perfiles sin proyectar a tiempo`);
    const replies = [];
    for (let start = 0; start < pending.length; start += BATCH) {
      const chunk = pending.slice(start, start + BATCH);
      replies.push(...http.batch(chunk.map((token) => request("GET", BFF_WEB_URL, "/v1/me/profile", token, undefined, "setup", false))));
    }
    pending = pending.filter((_, index) => {
      const reply = replies[index];
      return !(reply.status === 200 && reply.json("demographic") && reply.json("sport_habits"));
    });
    if (pending.length > 0) sleep(2);
  }
}

// Inicio futuro con desfase explícito coherente con `America/Bogota` (UTC-5 sin horario de verano).
function bogotaStart(daysAhead) {
  const local = new Date(Date.now() + daysAhead * 86400000 - 5 * 3600000);
  return `${local.toISOString().slice(0, 19)}-05:00`;
}

export function setup() {
  const tag = Array.from(new Uint8Array(crypto.randomBytes(4)), (item) => item.toString(16).padStart(2, "0")).join("");
  const accounts = Array.from({ length: RECIPIENTS }, (_, index) => ({
    // email-validator rechaza dominios reservados como `.test`.
    email: `k6-ha021-${tag}-${index}@example.com`,
    password: `clave-sintetica-${tag}-${index}`,
    city: `K6 HA021 ${tag} ${index}`,
  }));
  prepareAthletes(accounts);
  let organizer = { email: __ENV.ORGANIZER_EMAIL, password: __ENV.ORGANIZER_PASSWORD };
  if (!organizer.email || !organizer.password) {
    organizer = { email: `k6-ha021-org-${tag}@example.com`, password: `clave-sintetica-org-${tag}` };
    inBatches([organizer], (account) => register(account, "organizer", "Organizador Sintético"), [201], "organizador");
  }
  const [organizerToken] = loginAll([organizer]);
  // Sesiones nuevas justo antes de medir.
  const tokens = loginAll(accounts.map(({ email, password }) => ({ email, password })));
  return {
    tag,
    organizerToken,
    recipients: accounts.map((account, index) => ({ city: account.city, token: tokens[index] })),
  };
}

function failed() {
  errors.add(true);
  withinTarget.add(false);
}

export default function (data) {
  const iteration = exec.scenario.iterationInTest;
  const recipient = data.recipients[iteration % data.recipients.length];
  const created = send(request("POST", BFF_WEB_URL, "/v1/events", data.organizerToken, {
    name: `Carrera sintética HA021 ${data.tag} ${iteration}`,
    description: "Evento sintético de la prueba de carga HA021 (HU039).",
    discipline: "running",
    location: {
      name: "Parque sintético",
      address: "Calle sintética 1 # 2-3",
      city: recipient.city,
      country_code: COUNTRY_CODE,
    },
    starts_at: bogotaStart(30),
    time_zone: "America/Bogota",
    duration_minutes: 60,
    capacity: 10,
  }, "create", true));
  if (created.status !== 201) return failed();
  const eventId = created.json("event_id");
  const requestedAt = Date.now();
  const published = send(request("POST", BFF_WEB_URL, `/v1/events/${eventId}/publish`, data.organizerToken, undefined, "publish", true));
  if (published.status !== 200) return failed();

  const deadline = requestedAt + MAX_WAIT_SECONDS * 1000;
  while (Date.now() <= deadline) {
    const reply = send(request("GET", BFF_MOBILE_URL, "/v1/me/notifications?limit=50", recipient.token, undefined, "inbox", false));
    if (reply.status === 200) {
      const found = reply.json("items").filter((item) => item.event_id === eventId && item.type === MATCH);
      if (found.length > 0) {
        const elapsed = Date.now() - requestedAt;
        delay.add(elapsed);
        available.add(1);
        withinTarget.add(elapsed < AVAILABLE_MS);
        errors.add(false);
        if (found.length > 1) duplicates.add(found.length - 1);
        return undefined;
      }
    }
    sleep(POLL_MS / 1000);
  }
  return failed();
}
