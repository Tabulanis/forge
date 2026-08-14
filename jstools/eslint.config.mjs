// Dummy-proofing config for Merge's JS. Deliberately conservative: broad
// globals so it never false-alarms on browser (window, document) or node
// (require, module) names, and a curated set of rules that catch REAL bugs
// with near-zero false positives. Style is prettier's job, not eslint's — so
// no stylistic rules here, only correctness ones.
import globals from "globals";

export default [
  {
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: { ...globals.browser, ...globals.node, ...globals.es2021 },
    },
    rules: {
      eqeqeq: ["warn", "smart"],      // == vs === (the classic JS footgun)
      "no-const-assign": "error",     // reassigning a const
      "no-dupe-keys": "error",        // duplicate object keys
      "no-dupe-args": "error",        // duplicate function params
      "no-dupe-else-if": "warn",
      "no-unreachable": "warn",       // code after return/throw
      "no-cond-assign": "warn",       // if (x = 1) — meant == ?
      "use-isnan": "error",           // x === NaN (always false)
      "valid-typeof": "error",        // typeof x === "strnig"
      "no-undef": "warn",             // undefined names / typos'd calls
      "no-redeclare": "warn",
      "no-unsafe-negation": "warn",   // !x in y
      "no-self-compare": "warn",
      "no-unused-vars": ["warn", { args: "none" }],
    },
  },
];
