/**
 * k6 read scenario
 *
 * Runs 1,000 concurrent VUs for 30 minutes against the REST read surface.
 *
 * Env vars (required):
 *   BASE_URL   — e.g. https://catalog.example.com
 *   API_TOKEN  — Bearer token with read scope
 *   TENANT_ID  — Tenant slug (external id) to scope requests to, e.g. "dev".
 *                The X-Tenant-Id header matches against the entitlement
 *                grant's external id, which is the slug — a tenant UUID in
 *                this header is rejected with 403 on every request.
 *
 * Run (smoke):
 *   k6 run --vus 10 --duration 1m scripts/load_test/read_scenario.js
 *
 * Run (full SLO gate):
 *   k6 run scripts/load_test/read_scenario.js
 *
 * Scope note: this scenario covers REST reads only. The MCP surface uses an
 * SSE transport (GET /mcp/sse stream + POST /mcp/messages/ channel) that k6
 * cannot speak, so MCP tool-call latency is not exercised here — and as of
 * 2026-08 no automated gate covers it anywhere; the only MCP latency signal
 * is the per-tool Prometheus timer feeding the operations dashboard. An
 * in-process perf test is the planned home for that gate.
 */

import http from "k6/http";
import { check, sleep } from "k6";
import { Counter } from "k6/metrics";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const API_TOKEN = __ENV.API_TOKEN || "dev-token";
const TENANT_ID = __ENV.TENANT_ID || "dev"; // tenant slug, not UUID

// A small fixed set of IDs used for detail + dependency lookups. They are
// not required to exist — the checks below accept 404 — but a seeded
// environment (make dev-seed) gives the search branch real data to return.
const SEED_IDS = [
  "cap-seed-001",
  "cap-seed-002",
  "cap-seed-003",
  "cap-seed-004",
  "cap-seed-005",
];

const SEARCH_QUERIES = [
  "authentication",
  "payment processing",
  "data pipeline",
  "notification service",
  "user management",
  "file storage",
  "analytics engine",
  "recommendation",
  "search indexing",
  "rate limiting",
];

// ---------------------------------------------------------------------------
// k6 options
// ---------------------------------------------------------------------------

export const options = {
  scenarios: {
    read_load: {
      executor: "constant-vus",
      vus: 1000,
      duration: "30m",
    },
  },
  thresholds: {
    // Read endpoints (search, detail, dependencies)
    "http_req_duration{type:read}": ["p(95)<200", "p(99)<500"],
    // Overall error rate guard
    http_req_failed: ["rate<0.01"],
  },
};

// ---------------------------------------------------------------------------
// Custom counters
// ---------------------------------------------------------------------------

const readRequests = new Counter("read_requests");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function randomItem(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

function authHeaders() {
  return {
    Authorization: `Bearer ${API_TOKEN}`,
    "X-Tenant-Id": TENANT_ID, // slug — see the env-var note in the header
    "Content-Type": "application/json",
  };
}

// ---------------------------------------------------------------------------
// VU default function — search 50%, detail 30%, dependencies 20%
// ---------------------------------------------------------------------------

export default function () {
  const step = Math.random();

  if (step < 0.5) {
    // --- Search ---
    const q = encodeURIComponent(randomItem(SEARCH_QUERIES));
    const res = http.get(
      `${BASE_URL}/v1/search?q=${q}&limit=20`,
      {
        headers: authHeaders(),
        tags: { type: "read" },
      }
    );
    check(res, {
      "search 200": (r) => r.status === 200,
    });
    readRequests.add(1);
  } else if (step < 0.8) {
    // --- Capability detail ---
    const id = randomItem(SEED_IDS);
    const res = http.get(
      `${BASE_URL}/v1/capabilities/${id}`,
      {
        headers: authHeaders(),
        tags: { type: "read" },
      }
    );
    check(res, {
      "detail 200 or 404": (r) => r.status === 200 || r.status === 404,
    });
    readRequests.add(1);
  } else {
    // --- Dependencies ---
    const id = randomItem(SEED_IDS);
    const res = http.get(
      `${BASE_URL}/v1/capabilities/${id}/dependencies`,
      {
        headers: authHeaders(),
        tags: { type: "read" },
      }
    );
    check(res, {
      "deps 200 or 404": (r) => r.status === 200 || r.status === 404,
    });
    readRequests.add(1);
  }

  // Minimal think time: ~50 ms average keeps 1000 VUs at ~20k RPS max.
  sleep(Math.random() * 0.1);
}
