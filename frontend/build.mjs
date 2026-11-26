import { build } from 'esbuild';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const root = dirname(fileURLToPath(import.meta.url));
await mkdir(resolve(root, 'dist'), { recursive: true });
await build({
  absWorkingDir: root,
  entryPoints: ['./src/main.ts'],
  preserveSymlinks: true,
  outfile: 'dist/app.js',
  bundle: true,
  // TDesign 的 es 入口使用 .mjs，逐个组件自带已编译的本地 CSS。
  resolveExtensions: ['.ts', '.mjs', '.js', '.json', '.css'],
  platform: 'browser',
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
// 本地服务复用同一个地址，构建后让已有浏览器标签加载本次的脚本和样式。
let html = await readFile(resolve(root, 'index.html'), 'utf8');
for (const asset of ['app.js', 'app.css']) {
  const bytes = await readFile(resolve(root, 'dist', asset));
  const version = createHash('sha256').update(bytes).digest('hex').slice(0, 12);
  html = html.replace(`"/${asset}"`, `"/${asset}?v=${version}"`);
}
await writeFile(resolve(root, 'dist/index.html'), html);
console.log('Built offline frontend assets in frontend/dist/');
