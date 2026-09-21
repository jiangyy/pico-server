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
 *   node e2e_client.mjs <port> <clientDir> <dataDir> submit <playgroundId> <versionId> <name>
 *
 * The data directory is reused between invocations, so the local store (the
 * installed blobs, the content cursor and the pending queue) survives from one
 * step to the next exactly as it does between two launches of the app.
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

// The client only ever builds an https origin; the test server speaks plain
// HTTP on loopback, so the one injected primitive rewrites the scheme here
// rather than the client being taught to accept http.
const deps = {
  baseUrl: "https://127.0.0.1",
  fetch: (url, init) => fetch(url.replace("https://127.0.0.1", `http://127.0.0.1:${port}`), init),
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

/** Install the whole catalog and adopt the cursor, as a first launch does. */
async function bootstrap() {
  const result = await syncModule.bootstrap(deps, dataDir);
  const installed = await store.readLocalCatalog(dataDir);
  const snapshot = await syncModule.snapshot(dataDir);
  return {
    upto_seq: result.upto_seq,
    installed: installed.items.map((item) => ({
      playgroundId: item.playgroundId,
      versionId: item.versionId,
    })),
    cursor: snapshot.lastContentSeq,
  };
}

/** One regular sync round after bootstrap. */
async function syncOnce() {
  const result = await syncModule.sync(deps, dataDir);
  const installed = await store.readLocalCatalog(dataDir);
  const snapshot = await syncModule.snapshot(dataDir);
  return {
    accepted: result.accepted,
    rejected: result.rejected,
    installed: result.installed.map((entry) => entry.playground_id),
    retired: result.retired,
    upto_seq: result.upto_seq,
    cursor: snapshot.lastContentSeq,
    pending: snapshot.pendingCount,
    active: installed.items.map((item) => item.playgroundId),
  };
}

/** Queue one hand-in and push it, as the submit view does. */
async function submit([playgroundId, versionId, name]) {
  const text = await readFile(join(dataDir, "hand-in.py"), "utf8").catch(() => null);
  const body = text ?? `# 姓名：${name}\nprint(1)\n`;
  const queued = await submitModule.enqueueSubmission(dataDir, {
    playgroundId,
    versionId,
    files: { "work/main.py": body },
  });
  const result = await syncModule.sync(deps, dataDir);
  const snapshot = await syncModule.snapshot(dataDir);
  return {
    event_id: queued.event_id,
    accepted: result.accepted,
    rejected: result.rejected,
    pending: snapshot.pendingCount,
  };
}
