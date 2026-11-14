// Node 21+ treats `node --test` positional arguments as entry points or glob patterns and does not expand a bare directory into the test files inside it. Both CI (.github/workflows/ci.yml) and the quality gate run exactly `node --test tests/web/`, so this file makes that path loadable: Node's CJS directory resolution finds this index.js, which re-runs every real *.test.mjs in this directory and propagates its exit code.
"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

// Only the test files; env.mjs is a helper, not a test.
const files = fs.readdirSync(__dirname).filter((name) => name.endsWith(".test.mjs")).sort();
if (files.length === 0) process.exit(0);

// The outer runner sets NODE_TEST_CONTEXT in our environment; without stripping it the inner node --test believes it is already running and silently skips every file.
const env = { ...process.env };
delete env.NODE_TEST_CONTEXT;

const result = spawnSync(process.execPath, ["--test", ...files.map((name) => path.join(__dirname, name))], { stdio: "inherit", env });
process.exit(result.status === null ? 1 : result.status);
