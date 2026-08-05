import {bundle} from '@remotion/bundler';
import {renderMedia, renderStill, selectComposition} from '@remotion/renderer';
import {createReadStream, existsSync, readFileSync, statSync} from 'node:fs';
import {createServer} from 'node:http';
import {extname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const args = process.argv.slice(2);
const valueOf = (flag) => {
  const index = args.indexOf(flag);
  return index >= 0 ? args[index + 1] : '';
};
const manifestPath = resolve(valueOf('--manifest'));
const outputPath = resolve(valueOf('--output'));
const renderStillOnly = args.includes('--still');
if (!manifestPath || !outputPath || !existsSync(manifestPath)) {
  throw new Error('Usage: node render.mjs --manifest <file> --output <file>');
}

const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
if (manifest.composition_id !== 'KnowledgeVideoV1' || manifest.brand_overlay !== null) {
  throw new Error('Unsupported or unsafe render manifest');
}
if (!manifest.ai_content_label?.enabled) {
  throw new Error('AI content label cannot be disabled');
}

const sourceByPath = new Map();
const resolvedAssets = {};
for (const [assetId, filePath] of Object.entries(manifest.resolved_assets || {})) {
  const absolute = resolve(String(filePath));
  if (!existsSync(absolute) || !statSync(absolute).isFile()) {
    throw new Error(`Missing render asset: ${assetId}`);
  }
  const route = `/assets/${encodeURIComponent(assetId)}${extname(absolute)}`;
  sourceByPath.set(route, absolute);
  resolvedAssets[assetId] = route;
}

const mime = (path) => ({
  '.wav': 'audio/wav',
  '.mp3': 'audio/mpeg',
  '.mp4': 'video/mp4',
  '.webm': 'video/webm',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
}[extname(path).toLowerCase()] || 'application/octet-stream');

const server = createServer((request, response) => {
  const path = String(request.url || '').split('?', 1)[0];
  const source = sourceByPath.get(path);
  if (!source) {
    response.writeHead(404).end();
    return;
  }
  const size = statSync(source).size;
  const range = request.headers.range;
  response.setHeader('Accept-Ranges', 'bytes');
  response.setHeader('Content-Type', mime(source));
  if (range) {
    const match = /^bytes=(\d*)-(\d*)$/.exec(range);
    if (!match) {
      response.writeHead(416).end();
      return;
    }
    const start = match[1] ? Number(match[1]) : 0;
    const end = match[2] ? Math.min(Number(match[2]), size - 1) : size - 1;
    if (start > end || start >= size) {
      response.writeHead(416, {'Content-Range': `bytes */${size}`}).end();
      return;
    }
    response.writeHead(206, {
      'Content-Length': end - start + 1,
      'Content-Range': `bytes ${start}-${end}/${size}`,
    });
    createReadStream(source, {start, end}).pipe(response);
    return;
  }
  response.writeHead(200, {'Content-Length': size});
  createReadStream(source).pipe(response);
});

await new Promise((accept, reject) => {
  server.once('error', reject);
  server.listen(0, '127.0.0.1', accept);
});
const address = server.address();
if (!address || typeof address === 'string') throw new Error('Asset server failed to start');
const assetBase = `http://127.0.0.1:${address.port}`;
const inputProps = {
  ...manifest,
  resolved_assets: Object.fromEntries(
    Object.entries(resolvedAssets).map(([key, route]) => [key, `${assetBase}${route}`]),
  ),
};

const rendererRoot = fileURLToPath(new URL('.', import.meta.url));
const browserExecutable = process.env.REMOTION_BROWSER_EXECUTABLE || undefined;
const browserLaunch = browserExecutable ? {browserExecutable} : {};
const rawConcurrency = process.env.REMOTION_CONCURRENCY || '1';
const renderConcurrency = /^\d+$/.test(rawConcurrency)
  ? Number(rawConcurrency)
  : rawConcurrency;
try {
  const serveUrl = await bundle({
    entryPoint: resolve(rendererRoot, 'src/index.ts'),
    onProgress: () => undefined,
  });
  const composition = await selectComposition({
    serveUrl,
    id: renderStillOnly ? 'KnowledgeCoverV1' : 'KnowledgeVideoV1',
    inputProps,
    ...browserLaunch,
  });
  if (renderStillOnly) {
    await renderStill({
      composition,
      serveUrl,
      inputProps,
      output: outputPath,
      imageFormat: 'jpeg',
      overwrite: true,
      ...browserLaunch,
    });
  } else {
    await renderMedia({
      composition,
      serveUrl,
      inputProps,
      codec: 'h264',
      audioCodec: 'aac',
      audioBitrate: '192k',
      sampleRate: 48000,
      pixelFormat: 'yuv420p',
      colorSpace: 'bt709',
      outputLocation: outputPath,
      crf: 20,
      x264Preset: 'veryfast',
      concurrency: renderConcurrency,
      overwrite: true,
      chromiumOptions: {enableMultiProcessOnLinux: true},
      ...browserLaunch,
      onProgress: ({progress}) => {
        if (Math.round(progress * 100) % 10 === 0) {
          process.stdout.write(`render-progress=${Math.round(progress * 100)}%\n`);
        }
      },
    });
  }
  process.stdout.write(`rendered=${outputPath}\n`);
} finally {
  await new Promise((accept) => server.close(accept));
}
