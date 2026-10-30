#!/usr/bin/env node
import { product, runtimePaths } from "./product.js";

const command = process.argv[2];

switch (command) {
  case "--version":
  case "version":
    console.log(`${product.name} ${product.version}`);
    break;
  case "paths":
    console.log(JSON.stringify(runtimePaths(), null, 2));
    break;
  case undefined:
  case "--help":
  case "help":
    console.log("usage: merlin-next [--version|paths]");
    break;
  default:
    console.error(`unknown command: ${command}`);
    process.exitCode = 2;
}
