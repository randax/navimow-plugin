import type { PluginOptions } from '@grafana/plugin-e2e';
import { defineConfig } from '@playwright/test';
import baseConfig from './.config/playwright.config';

export default defineConfig<PluginOptions>(baseConfig, {
  // MapLibre renders through WebGL; headless Chromium has none unless pointed at SwiftShader.
  use: {
    launchOptions: { args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] },
  },
  // Software rendering makes the first frame slow.
  expect: { timeout: 20_000 },
});
