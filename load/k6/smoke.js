// Smoke de rendimiento con umbrales que hacen fallar la ejecución.
// Uso: docker run --rm -i -e BASE_URL=http://host.docker.internal:8000 grafana/k6 run - < load/k6/smoke.js
// Los umbrales de negocio (p. ej. HA de registro) se agregan cuando exista la operación y su criterio aprobado.
import http from "k6/http";
import { check } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://127.0.0.1:8000";

export const options = {
  vus: 2,
  duration: "20s",
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<500"],
  },
};

export default function () {
  const live = http.get(`${BASE_URL}/health/live`);
  check(live, { "live 200": (response) => response.status === 200 });
  const ready = http.get(`${BASE_URL}/health/ready`);
  check(ready, { "ready 200": (response) => response.status === 200 });
}
