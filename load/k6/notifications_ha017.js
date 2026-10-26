// HU039 / HA017 (ADR-045 §12): un evento de running publicado en Bogotá con 1.000 deportistas
// coincidentes. Mide, sobre una muestra de destinatarios, el tiempo desde la solicitud de
// publicación hasta que la notificación `EVENT_PUBLISHED_MATCH` de ese `event_id` está disponible
// en la bandeja (`GET /v1/me/notifications` de `bff-mobile`), con errores y duplicados. La
// generación (inserciones) y la publicación del outbox se leen en las métricas y los logs del
// `eventos-notificaciones-worker` y del publicador; la entrega push no se mide aquí. Solo datos
// sintéticos; fuera de cada PR.
//
// Requisitos: un ambiente con `bff-web`, `bff-mobile`, Usuarios (API, outbox y proyector),
// Eventos (API, outbox, worker de notificaciones y proyector) y Pub/Sub. Nunca producción.
// Uso:
//   docker run --rm -i -e BFF_WEB_URL=http://host.docker.internal:8000 \
//     -e BFF_MOBILE_URL=http://host.docker.internal:18081 \
//     grafana/k6 run - < load/k6/notifications_ha017.js
// (con k6 instalado: `k6 run -e BFF_WEB_URL=... -e BFF_MOBILE_URL=... load/k6/notifications_ha017.js`)
//
// Preparación (en `setup()`, sin medir):
//   - Si no se da `ACCOUNTS_FILE`, registra RECIPIENTS deportistas sintéticos por `bff-web`
//     `POST /v1/users`, inicia su sesión por `bff-mobile` `POST /v1/sessions` (el access token
//     sirve en ambos BFF), guarda `PUT /v1/me/demographic-profile` (CITY/COUNTRY_CODE) y
//     `PUT /v1/me/sport-habits` (running) por `bff-web`, y espera a que `GET /v1/me/profile`
//     muestre ambas secciones (la búsqueda de destinatarios lee esa proyección).
//   - Con `ACCOUNTS_FILE` (JSON `[{"email":"...","password":"..."}, ...]`, ruta legible por k6)
//     usa cuentas `athlete` ya sembradas con esa ciudad y running; no las modifica.
//   - Organizador: ORGANIZER_EMAIL/ORGANIZER_PASSWORD o, si faltan, uno sintético registrado aquí.
//   - Al final renueva la sesión de la muestra (el access token dura 15 min, HU003), crea el
//     evento (`POST /v1/events`) y lo publica (`POST /v1/events/{id}/publish`) por `bff-web`.
//
// Variables opcionales: RECIPIENTS (1000), SAMPLE (100), CITY (Bogotá), COUNTRY_CODE (CO),
// REGISTRATION_POLICIES_VERSION (local-dev; la de `users-management`), BATCH (25 solicitudes en
// paralelo durante la preparación), PROJECTION_TIMEOUT_SECONDS (300), MAX_WAIT_SECONDS (120),
// POLL_MS (1000), SETUP_TIMEOUT (30m), DELAY_P95_MS (sin valor: el retraso solo se reporta).
// Supuestos: error < 1 % de la muestra (como HA008), cero duplicados (CA3) y la notificación
// disponible para toda la muestra; HA017 no fija un umbral de retraso.
import http from "k6/http";
import { check, fail, sleep } from "k6";
import crypto from "k6/crypto";
import exec from "k6/execution";
import { Counter, Rate, Trend } from "k6/metrics";

const BFF_WEB_URL = __ENV.BFF_WEB_URL || "http://127.0.0.1:8000";
const BFF_MOBILE_URL = __ENV.BFF_MOBILE_URL || "http://127.0.0.1:18081";
const RECIPIENTS = Number(__ENV.RECIPIENTS || 1000);
const SAMPLE = Number(__ENV.SAMPLE || 100);
const CITY = __ENV.CITY || "Bogotá";
const COUNTRY_CODE = __ENV.COUNTRY_CODE || "CO";
const POLICIES_VERSION = __ENV.REGISTRATION_POLICIES_VERSION || "local-dev";
const BATCH = Number(__ENV.BATCH || 25);
const PROJECTION_TIMEOUT_SECONDS = Number(__ENV.PROJECTION_TIMEOUT_SECONDS || 300);
const MAX_WAIT_SECONDS = Number(__ENV.MAX_WAIT_SECONDS || 120);
const POLL_MS = Number(__ENV.POLL_MS || 1000);
const MATCH = "EVENT_PUBLISHED_MATCH";
// `open()` solo existe en la fase de inicio.
const ACCOUNTS = __ENV.ACCOUNTS_FILE ? JSON.parse(open(__ENV.ACCOUNTS_FILE)) : null;

const delay = new Trend("notification_delay", true);
const errors = new Rate("notification_errors");
const duplicates = new Counter("notification_duplicates");
const available = new Counter("notifications_available");
const publishDuration = new Trend("event_publish_duration", true);

const thresholds = {
  notification_errors: ["rate<0.01"],
  notification_duplicates: ["count==0"],
  notifications_available: [`count>=${SAMPLE}`],
};
if (__ENV.DELAY_P95_MS) thresholds.notification_delay = [`p(95)<${Number(__ENV.DELAY_P95_MS)}`];

export const options = {
  setupTimeout: __ENV.SETUP_TIMEOUT || "30m",
  scenarios: {
    inbox: {
      executor: "per-vu-iterations",
      vus: SAMPLE,
      iterations: 1,
      maxDuration: `${MAX_WAIT_SECONDS + 60}s`,
    },
  },
  thresholds,
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
  inBatches(tokens, (token) => request("PUT", BFF_WEB_URL, "/v1/me/demographic-profile", token, {
    birth_date: "1990-05-10",
    gender: "PREFER_NOT_TO_SAY",
    country_code: COUNTRY_CODE,
    city: CITY,
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
  const runId = crypto.randomBytes(4);
  const tag = Array.from(new Uint8Array(runId), (item) => item.toString(16).padStart(2, "0")).join("");
  let accounts = ACCOUNTS;
  if (accounts) {
    if (accounts.length < SAMPLE) fail(`ACCOUNTS_FILE necesita al menos ${SAMPLE} cuentas`);
  } else {
    accounts = Array.from({ length: RECIPIENTS }, (_, index) => ({
      // email-validator rechaza dominios reservados como `.test`.
      email: `k6-ha017-${tag}-${index}@example.com`,
      password: `clave-sintetica-${tag}-${index}`,
    }));
    prepareAthletes(accounts);
  }
  let organizer = { email: __ENV.ORGANIZER_EMAIL, password: __ENV.ORGANIZER_PASSWORD };
  if (!organizer.email || !organizer.password) {
    organizer = { email: `k6-ha017-org-${tag}@example.com`, password: `clave-sintetica-org-${tag}` };
    inBatches([organizer], (account) => register(account, "organizer", "Organizador Sintético"), [201], "organizador");
  }
  const [organizerToken] = loginAll([organizer]);
  // Sesión nueva para la muestra: la preparación de 1.000 cuentas puede tardar minutos.
  const sampleTokens = loginAll(accounts.slice(0, SAMPLE));

  const created = send(request("POST", BFF_WEB_URL, "/v1/events", organizerToken, {
    name: `Carrera sintética HA017 ${tag}`,
    description: "Evento sintético de la prueba de carga HA017 (HU039).",
    discipline: "running",
    location: {
      name: "Parque sintético",
      address: "Calle sintética 1 # 2-3",
      city: CITY,
      country_code: COUNTRY_CODE,
    },
    starts_at: bogotaStart(30),
    time_zone: "America/Bogota",
    duration_minutes: 120,
    capacity: RECIPIENTS,
  }, "setup", true));
  if (created.status !== 201) fail(`crear evento: ${created.status}`);
  const eventId = created.json("event_id");
  const requestedAt = Date.now();
  const published = send(request("POST", BFF_WEB_URL, `/v1/events/${eventId}/publish`, organizerToken, undefined, "publish", true));
  if (published.status !== 200) fail(`publicar evento: ${published.status}`);
  publishDuration.add(published.timings.duration);
  return { eventId, requestedAt, sampleTokens };
}

// Notificaciones del evento en la primera página de la bandeja (la más reciente primero).
function matches(reply, eventId) {
  return reply.json("items").filter((item) => item.event_id === eventId && item.type === MATCH);
}

export default function (data) {
  const token = data.sampleTokens[(exec.vu.idInTest - 1) % data.sampleTokens.length];
  const deadline = data.requestedAt + MAX_WAIT_SECONDS * 1000;
  while (Date.now() <= deadline) {
    const reply = send(request("GET", BFF_MOBILE_URL, "/v1/me/notifications?limit=50", token, undefined, "inbox", false));
    if (reply.status === 200) {
      const found = matches(reply, data.eventId);
      if (found.length > 0) {
        delay.add(Date.now() - data.requestedAt);
        available.add(1);
        if (found.length > 1) duplicates.add(found.length - 1);
        errors.add(false);
        check(found[0], { "prioridad NORMAL": (item) => item.priority === "NORMAL" });
        return;
      }
    }
    sleep(POLL_MS / 1000);
  }
  errors.add(true);
}
