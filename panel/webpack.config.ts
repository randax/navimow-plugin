import CopyWebpackPlugin from 'copy-webpack-plugin';
import path from 'path';
import type { Configuration } from 'webpack';
import { merge } from 'webpack-merge';
import grafanaConfig, { type Env } from './.config/webpack/webpack.config';

// MapLibre loads its worker as a separate module, which must be served from the plugin's own
// origin. Source paths are absolute because relative ones resolve against src/ and fail.
const maplibreDist = path.resolve(__dirname, 'node_modules/maplibre-gl/dist');

const config = async (env: Env): Promise<Configuration> =>
  merge(await grafanaConfig(env), {
    plugins: [
      new CopyWebpackPlugin({
        patterns: ['maplibre-gl-worker.mjs', 'maplibre-gl-shared.mjs'].map((file) => ({
          from: path.join(maplibreDist, file),
          to: '.',
        })),
      }),
    ],
  });

export default config;
