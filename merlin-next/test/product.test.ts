import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { join } from "node:path";
import test from "node:test";
import { product, runtimePaths } from "../src/product.js";

test("product identity does not replace stable Merlin", () => {
  assert.equal(product.name, "merlin-next");
  assert.notEqual(product.name, "merlin");
  assert.notEqual(product.bundleId, "ai.merlin");
  assert.equal(product.releaseChannel, "next");
});

test("all state is isolated from stable Merlin", () => {
  const paths = runtimePaths("/home/operator");
  const serialized = JSON.stringify(paths);

  assert.ok(Object.values(paths).every((path) => path.includes("merlin-next")));
  assert.ok(!serialized.includes(".merlin-python"));
  assert.ok(!serialized.includes(".merlin\""));
  assert.ok(!serialized.includes("share/merlin\""));
  assert.ok(!serialized.includes(".pg0"));
});

test("CLI reports its independent version and paths", () => {
  const cli = join(process.cwd(), "dist", "src", "cli.js");
  const version = execFileSync(process.execPath, [cli, "--version"], { encoding: "utf8" });
  const paths = JSON.parse(execFileSync(process.execPath, [cli, "paths"], { encoding: "utf8" }));

  assert.equal(version.trim(), "merlin-next 0.1.0");
  assert.ok(paths.data.endsWith("share/merlin-next"));
});
