import js from "@eslint/js";
import tsParser from "@typescript-eslint/parser";
import pluginVue from "eslint-plugin-vue";

export default [
  js.configs.recommended,
  ...pluginVue.configs["flat/recommended"],
  { files: ["src/**/*.ts"], languageOptions: { parser: tsParser }, rules: { "no-undef": "off" } },
  { files: ["src/**/*.vue"], languageOptions: { parserOptions: { parser: tsParser } }, rules: { "no-undef": "off" } },
  { ignores: ["dist", "node_modules"] },
];
