import { build } from 'esbuild';
import { mkdir, copyFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const root = dirname(fileURLToPath(import.meta.url));
await mkdir(resolve(root, 'dist'), { recursive: true });
await build({
  entryPoints: [resolve(root, 'src/main.ts')],
  outfile: resolve(root, 'dist/app.js'),
  bundle: true,
  minify: true,
  sourcemap: false,
  target: ['es2020'],
  alias: { vue: 'vue/dist/vue.esm-bundler.js' },
  define: {
    'process.env.NODE_ENV': '"production"',
    __VUE_OPTIONS_API__: 'true',
    __VUE_PROD_DEVTOOLS__: 'false',
    __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false'
  }
});
await copyFile(resolve(root, 'index.html'), resolve(root, 'dist/index.html'));
console.log('Built offline frontend assets in frontend/dist/');
