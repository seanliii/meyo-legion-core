#!/usr/bin/env node
'use strict';
// Bind fixed browser materials to the original MCP process, not a new daemon.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const Module = require('node:module');
const {createRequire} = Module;

// Share a finite action ceiling across the original CLI and managed connection.
// Successful actions do not wait out the budget; native cancellation stays intact.
const DEFAULT_ACTION_TIMEOUT_MS = 15000;

function checkedFile(file, expected) {
  if (!path.isAbsolute(file)) throw new Error('runtime material path must be absolute');
  const info = fs.statSync(file);
  if (!info.isFile()) throw new Error('runtime material is not a regular file');
  if (expected) {
    if (!/^[a-f0-9]{64}$/.test(expected)) throw new Error('invalid runtime material digest');
    const actual = crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
    if (actual !== expected) throw new Error('runtime material checksum mismatch: ' + path.basename(file));
  }
  return fs.realpathSync(file);
}

function validateManagedPackages(packageFile) {
  if (!process.env.MEYO_MANAGED_BROWSER_CONFIG) return;
  // This is supplied by the parent after filtering the caller's environment,
  // before HOME/TMPDIR are replaced with host scratch. Never infer it here.
  let roots;
  try { roots = JSON.parse(process.env.MEYO_MANAGED_BROWSER_WRITABLE_ROOTS); }
  catch { throw new Error('missing native-writable boundary for managed browser'); }
  if (!Array.isArray(roots) || !roots.length ||
      roots.some(root => typeof root !== 'string' || !path.isAbsolute(root)))
    throw new Error('invalid native-writable boundary for managed browser');
  roots = roots.map(root => path.resolve(root));
  const inside = (file, root) => file === root ||
    file.startsWith(root.endsWith(path.sep) ? root : root + path.sep);
  function readonly(file, directory = false) {
    const real = fs.realpathSync(file);
    if (roots.some(root => inside(path.resolve(file), root) || inside(real, root) ||
        (directory && inside(root, real))))
      throw new Error('browser module is selected from a native-writable root');
    return real;
  }
  // Check links anywhere in the selected package, including nested imports;
  // checking only package.json would leave cli.js/index.js/dependencies mutable.
  const directories = new Set(), packages = new Set(), queue = [packageFile];
  function walk(directory) {
    const real = readonly(directory, true);
    if (directories.has(real)) return;
    directories.add(real);
    for (const name of fs.readdirSync(real)) {
      const file = path.join(real, name);
      if (fs.statSync(file).isDirectory()) walk(file);
      else readonly(file);
    }
  }
  while (queue.length) {
    const manifest = readonly(queue.pop());
    if (packages.has(manifest)) continue;
    packages.add(manifest);
    walk(path.dirname(manifest));
    const metadata = JSON.parse(fs.readFileSync(manifest, 'utf8'));
    const resolver = createRequire(manifest);
    const required = metadata.dependencies || {};
    const optional = {...metadata.peerDependencies, ...metadata.optionalDependencies};
    for (const name of Object.keys({...required, ...optional})) {
      // Use Node's lookup directories, not task cwd or a guessed node_modules.
      // Inspect manifests without executing modules or requiring JSON exports.
      const dependency = (resolver.resolve.paths(name) || [])
        .map(root => path.join(root, name, 'package.json')).find(file => fs.existsSync(file));
      if (dependency) queue.push(dependency);
      else if (Object.hasOwn(required, name) && !Object.hasOwn(optional, name))
        throw new Error('managed browser dependency is missing');
    }
  }
  // This is one owned worker with the pinned CommonJS runtime. Enforce the
  // same boundary at actual resolution, including later transitive imports.
  const resolveFilename = Module._resolveFilename;
  Module._resolveFilename = function(...args) {
    const file = resolveFilename.apply(this, args);
    if (path.isAbsolute(file)) readonly(file);
    return file;
  };
}

function loadMaterials() {
  const data = process.env.MEYO_LEGION_DIR || path.join(os.homedir(), '.meyo', 'legion');
  const explicit = process.env.MEYO_PLAYWRIGHT_RUNTIME_PROFILE;
  const profilePath = explicit || path.join(data, 'config', 'playwright-runtime.json');
  let profile, info;
  try { info = fs.lstatSync(profilePath); }
  catch (error) {
    if (error.code !== 'ENOENT') throw new Error('runtime profile cannot be inspected: ' + error.code);
  }
  if (info) {
    if (!info.isFile() || (info.mode & 0o077) ||
        (process.getuid && info.uid !== process.getuid()))
      throw new Error('unsafe runtime profile owner or permissions');
    profile = JSON.parse(fs.readFileSync(profilePath, 'utf8'));
    if (profile.schema_version !== 1) throw new Error('unsupported runtime profile');
    if (profile.platform && profile.platform !== process.platform)
      throw new Error('runtime profile platform does not match this host');
    const required = profile.platform === 'darwin'
      ? ['runtime_root', 'mcp_package_path', 'mcp_package_sha256',
         'browser_executable_path', 'browser_sha256']
      : ['runtime_root', 'native_libs_root', 'font_path',
         'native_manifest_sha256', 'font_sha256', 'mcp_package_sha256'];
    for (const key of required)
      if (typeof profile[key] !== 'string' || !profile[key]) throw new Error('missing runtime profile field: ' + key);
  } else if (explicit) {
    throw new Error('configured runtime profile is missing');
  }
  const repo = path.resolve(__dirname, '../../..');
  const candidates = profile ? [
    profile.platform === 'darwin' ? profile.mcp_package_path :
      path.join(profile.runtime_root, 'node-tools/packages/playwright-mcp/lib/node_modules/@playwright/mcp/package.json'),
  ] : [
    path.join(repo, 'runtime/node-tools/packages/playwright-mcp/lib/node_modules/@playwright/mcp/package.json'),
    path.join(os.homedir(), '.npm-global/lib/node_modules/@playwright/mcp/package.json'),
  ];
  const packageFile = candidates.find(file => fs.existsSync(file));
  if (!packageFile) throw new Error('existing Playwright MCP runtime not found; no install attempted');
  const packagePath = checkedFile(packageFile, profile && profile.mcp_package_sha256);
  const metadata = JSON.parse(fs.readFileSync(packagePath, 'utf8'));
  if (metadata.name !== '@playwright/mcp') throw new Error('unexpected MCP runtime package');
  const cli = checkedFile(path.join(path.dirname(packagePath), 'cli.js'));
  validateManagedPackages(packagePath);
  let executable, libraries, font;
  if (profile?.platform === 'darwin') {
    executable = checkedFile(profile.browser_executable_path, profile.browser_sha256);
    fs.accessSync(executable, fs.constants.X_OK);
  } else if (profile) {
    const manifest = checkedFile(path.join(profile.native_libs_root, 'manifest.json'), profile.native_manifest_sha256);
    libraries = fs.realpathSync(path.join(profile.native_libs_root, 'lib'));
    if (!fs.statSync(libraries).isDirectory()) throw new Error('runtime native library directory missing');
    const records = JSON.parse(fs.readFileSync(manifest, 'utf8')).libraries;
    if (!records || Array.isArray(records) || typeof records !== 'object')
      throw new Error('invalid runtime native library manifest');
    // Bounded to this existing manifest, never scan another member's source tree.
    for (const [name, record] of Object.entries(records)) {
      if (name !== path.basename(name) || !record || typeof record.sha256 !== 'string')
        throw new Error('invalid runtime library record');
      const library = path.join(libraries, name);
      if (!fs.realpathSync(library).startsWith(libraries + path.sep))
        throw new Error('runtime library link escapes its declared directory');
      checkedFile(library, record.sha256);
    }
    font = checkedFile(profile.font_path, profile.font_sha256);
    executable = checkedFile(path.join(profile.runtime_root, 'browser/chrome-linux64/chrome'));
    fs.accessSync(executable, fs.constants.X_OK);
  } else if (process.env.PLAYWRIGHT_MCP_EXECUTABLE_PATH) {
    executable = checkedFile(process.env.PLAYWRIGHT_MCP_EXECUTABLE_PATH);
  }
  return {cli, executable, libraries, font, managed: !!profile, version: metadata.version};
}

function xml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;',
  }[ch]));
}

function prepareRuntime(materials) {
  // The execution boundary supplies a short private TMPDIR for Chromium's
  // socket paths. Global /tmp may be read-only; never escape that declared root.
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'mb-'));
  fs.chmodSync(temporary, 0o700);
  let cleaned = false;
  function cleanup() {
    if (!cleaned) {
      cleaned = true;
      fs.rmSync(temporary, {recursive: true, force: true});
    }
  }
  try {
    const env = {...process.env, TMPDIR: temporary};
    // Same-version MCP merges inherited environment after config. Keep its
    // sandbox env consistent too; --sandbox alone maps to undefined in 0.0.79.
    env.PLAYWRIGHT_MCP_SANDBOX = 'true';
    if (materials.libraries)
      env.LD_LIBRARY_PATH = materials.libraries +
        (env.LD_LIBRARY_PATH ? path.delimiter + env.LD_LIBRARY_PATH : '');
    if (materials.font) {
      const fonts = path.join(temporary, 'fonts.conf');
      fs.writeFileSync(fonts,
        '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd"><fontconfig>' +
        '<include ignore_missing="yes">/etc/fonts/fonts.conf</include><dir>' +
        xml(path.dirname(materials.font)) + '</dir><cachedir>' +
        xml(path.join(temporary, 'font-cache')) + '</cachedir></fontconfig>', {mode: 0o600});
      env.FONTCONFIG_FILE = fonts;
    }
    return {temporary, env, cleanup};
  } catch (error) {
    cleanup();
    throw error;
  }
}

function main() {
  const supplied = process.argv.slice(2);
  // The managed entry is an execution capability, not an installer or a
  // configurable escape from the browser sandbox. Other native options stay native.
  if (supplied.some(arg => arg === 'install-browser' || arg.startsWith('--no-sandbox') ||
      arg.startsWith('--disable-') || arg === '--single-process' || arg.startsWith('--config')))
    throw new Error('runtime launcher rejects installation or sandbox/config override');
  const materials = loadMaterials();
  if (supplied.includes('--describe')) {
    console.log(JSON.stringify({
      entry: 'original_playwright_mcp_cli', ...materials,
      sandbox_requested: true, runtime_execution_verified: false,
      scope: 'fixed profile selection and material checks; browser state requires live verification',
    }));
    return;
  }
  const {temporary, env, cleanup} = prepareRuntime(materials);
  process.once('exit', cleanup);
  // Keep original MCP signal/exit semantics. SIGKILL still requires parent cleanup.
  try {
    Object.assign(process.env, env);
    const config = path.join(temporary, 'playwright.json');
    fs.writeFileSync(config, JSON.stringify({timeouts: {action: DEFAULT_ACTION_TIMEOUT_MS}, browser: {
      isolated: true, launchOptions: {
        headless: true, chromiumSandbox: true,
        ...(materials.executable ? {executablePath: materials.executable} : {}),
      },
    }}), {mode: 0o600});
    // 0.0.79 CLI otherwise overrides config chromiumSandbox=true with false.
    process.argv = [process.execPath, materials.cli, ...supplied,
      '--headless', '--isolated', '--sandbox', '--config', config,
      ...(materials.executable ? ['--executable-path', materials.executable] : [])];
    require(materials.cli);
  } catch (error) {
    cleanup();
    throw error;
  }
}

if (require.main === module) {
  try { main(); }
  catch (error) {
    console.error('browser_runtime_unavailable: ' + error.message);
    process.exitCode = 78;
  }
}
module.exports = {loadMaterials, prepareRuntime, DEFAULT_ACTION_TIMEOUT_MS};
