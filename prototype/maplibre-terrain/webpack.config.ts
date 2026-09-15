import type { Configuration } from 'webpack';
import { merge } from 'webpack-merge';
import CopyWebpackPlugin from 'copy-webpack-plugin';
import path from 'path';
import grafanaConfig, { type Env } from './.config/webpack/webpack.config';

const config = async (env: Env): Promise<Configuration> => {
  const baseConfig = await grafanaConfig(env);
  return merge(baseConfig, {
    plugins: [
      new CopyWebpackPlugin({
        patterns: [
          { from: path.resolve(__dirname, 'node_modules/maplibre-gl/dist/maplibre-gl-worker.mjs'), to: '.' },
          { from: path.resolve(__dirname, 'node_modules/maplibre-gl/dist/maplibre-gl-shared.mjs'), to: '.' },
        ],
      }),
    ],
  });
};

export default config;
