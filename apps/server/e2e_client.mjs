/**
 * Drives the real client modules against a running server.
 *
 * This is the other half of `test_client_contract.py`: the Python test owns a
 * real server and a real operator, and this script plays the client. It uses
 * the client's own `transport`, `store`, `sync` and `submit` modules — nothing
 * here re-implements the protocol, so a shape the two sides disagree about
 * fails the test instead of passing a hand-written stub.
 *
 *   node e2e_client.mjs <port> <clientDir> <dataDir> bootstrap
 *   node e2e_client.mjs <port> <clientDir> <dataDir> sync
 *   node e2e_client.mjs <port> <clientDir> <dataDir> submit <assignmentId> <versionId> <name>
 *
 * The data directory is reused between invocations, so the local store (the
 * installed blobs and the queue of hand-ins the server has not answered)
 * survives from one step to the next exactly as it does between two launches of
 * the app.
 *
 * Every step prints one `RESULT <json>` line; a failure prints `FAILED <code>
 * <message>` and exits non-zero.
 */

import { readFile } from "node:fs/promises";
import { join } from "node:path";

const [port, clientDir, dataDir, step, ...rest] = process.argv.slice(2);

const transport = await import(join(clientDir, "transport.ts"));
const store = await import(join(clientDir, "store.ts"));
const syncModule = await import(join(clientDir, "sync.ts"));
const submitModule = await import(join(clientDir, "submit.ts"));

// How many requests each step makes, and of which kind. The Python half uses
// this to assert what a round *did* — "it installed the new course and did not
// re-fetch the old one" is a statement about requests, not about the answer.
const http = { requests: 0, catalog: 0, manifests: 0, files: 0, submissions: 0 };

// The test server is the local development server: plain http on loopback,
// which is exactly the origin rule the client applies (`origin.ts`).
const deps = {
  baseUrl: `http://127.0.0.1:${port}`,
  fetch: (url, init) => {
    http.requests += 1;
    const pathname = new URL(url).pathname;
    if (pathname === "/api/v1/catalog") http.catalog += 1;
    else if (pathname.endsWith("/manifest")) http.manifests += 1;
    else if (pathname.includes("/files/")) http.files += 1;
    else if (pathname === "/api/v1/submissions") http.submissions += 1;
    return globalThis.fetch(url, init);
  },
};

try {
  const result = await run(step, rest);
  console.log("RESULT " + JSON.stringify(result));
} catch (error) {
  console.log(`FAILED ${error.code ?? error.name} ${error.message}`);
  process.exitCode = 1;
}

async function run(step, args) {
  switch (step) {
    case "bootstrap":
      return await bootstrap();
    case "sync":
      return await syncOnce();
    case "submit":
      return await submit(args);
    default:
      throw new Error(`unknown step: ${step}`);
  }
}

/** The delivery outcomes of one round, in a shape the Python half can assert on. */
function deliveries(result) {
  return {
    stored: result.deliveries
      .filter((delivery) => delivery.kind === "stored")
      .map((delivery) => delivery.submission_id),
    refused: result.deliveries
      .filter((delivery) => delivery.kind === "refused")
      .map((delivery) => ({ submission_id: delivery.submission_id, reason: delivery.reason })),
    undelivered: result.deliveries
      .filter((delivery) => delivery.kind === "undelivered")
      .map((delivery) => delivery.submission_id),
  };
}

/** Install the whole catalog and make it the local set, as a first launch does. */
async function bootstrap() {
  await syncModule.bootstrap(deps, dataDir);
  const installed = await store.readLocalCatalog(dataDir);
  return {
    installed: installed.items.map((item) => ({
      playgroundId: item.playgroundId,
      versionId: item.versionId,
    })),
    active: installed.items.map((item) => item.playgroundId),
    problems: installed.problems.map((problem) => problem.code),
    http: { ...http },
  };
}

/** One regular round after bootstrap: read the catalog, then hand in what waits. */
async function syncOnce() {
  const result = await syncModule.sync(deps, dataDir);
  const installed = await store.readLocalCatalog(dataDir);
  const snapshot = await syncModule.snapshot(dataDir);
  return {
    ...deliveries(result),
    installed: result.installed.map((entry) => entry.playground_id),
    retired: result.retired,
    pending: snapshot.pendingCount,
    active: installed.items.map((item) => item.playgroundId),
    http: { ...http },
  };
}

/** Queue one hand-in and push it, as the submit view does. */
async function submit([assignmentId, versionId, name]) {
  const text = await readFile(join(dataDir, "hand-in.py"), "utf8").catch(() => null);
  const body = text ?? `# 姓名：${name}\nprint(1)\n`;
  const queued = await submitModule.enqueueSubmission(dataDir, {
    assignmentId,
    versionId,
    files: { "work/main.py": body },
  });
  const result = await syncModule.sync(deps, dataDir);
  const snapshot = await syncModule.snapshot(dataDir);
  const outcome = result.deliveries.find(
    (delivery) => delivery.submission_id === queued.submission_id,
  );
  return {
    submission_id: queued.submission_id,
    outcome: outcome?.kind ?? "none",
    reason: outcome?.reason ?? "",
    pending: snapshot.pendingCount,
    http: { ...http },
  };
}
