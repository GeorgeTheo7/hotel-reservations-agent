// ESLint: typescript-eslint's strict, type-aware rules and the React hooks rules
// (SPEC, Open items). tsc does the type checking; this catches likely bugs.
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";

export default defineConfig(
  { ignores: ["dist", "node_modules"] },
  js.configs.recommended,
  tseslint.configs.strictTypeChecked,
  {
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
  },
  reactHooks.configs.flat.recommended,
  { files: ["eslint.config.js"], extends: [tseslint.configs.disableTypeChecked] },
);
