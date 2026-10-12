import { cp, mkdir, rm } from "node:fs/promises";
const source = new URL("../dist/", import.meta.url);
const destination = new URL("../../src/merlin_web/static/", import.meta.url);
await rm(destination, { recursive: true, force: true });
await mkdir(destination, { recursive: true });
await cp(source, destination, { recursive: true });
console.log("Staged compiled UI in the Python package.");
