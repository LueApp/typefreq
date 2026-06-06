import { cp, mkdir, rm, readFile, copyFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = dirname(fileURLToPath(new URL('../package.json', import.meta.url)));
const version = await readVersion();
const buildRoot = join(root, '.download-build');
const packageRoot = join(buildRoot, 'keyfreq');
const outputDir = join(root, 'public', 'downloads');
const versionedOutput = join(outputDir, `keyfreq-${version}.tar`);
const latestOutput = join(outputDir, 'keyfreq-latest.tar');

await rm(buildRoot, { recursive: true, force: true });
await mkdir(packageRoot, { recursive: true });
await mkdir(outputDir, { recursive: true });

const filter = (src) => {
  const normalized = src.replaceAll('\\', '/');
  return !(
    normalized.includes('/__pycache__') ||
    normalized.endsWith('.pyc') ||
    normalized.endsWith('.db') ||
    normalized.endsWith('.db-journal')
  );
};

await cp(join(root, 'keyfreq'), join(packageRoot, 'keyfreq'), { recursive: true, filter });
await cp(join(root, 'systemd'), join(packageRoot, 'systemd'), { recursive: true, filter });
for (const file of ['install.sh', 'requirements.txt', 'smoke_test.py', 'README.md']) {
  if (existsSync(join(root, file))) {
    await cp(join(root, file), join(packageRoot, file), { filter });
  }
}

const tar = spawnSync('tar', ['-cf', versionedOutput, '-C', buildRoot, 'keyfreq'], {
  stdio: 'inherit',
});
if (tar.status !== 0) {
  throw new Error(`tar failed with exit code ${tar.status ?? 'unknown'}`);
}

await copyFile(versionedOutput, latestOutput);
await rm(buildRoot, { recursive: true, force: true });
console.log(`Built download archive: ${latestOutput}`);

async function readVersion() {
  const init = await readFile(join(root, 'keyfreq', '__init__.py'), 'utf8');
  const match = init.match(/__version__\s*=\s*["']([^"']+)["']/);
  return match?.[1] ?? '0.1.0';
}
