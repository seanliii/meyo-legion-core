import { createHash } from 'node:crypto'
import { isBuiltin } from 'node:module'
import { types } from 'node:util'

/** Text-only packaging limits, measured in UTF-8 bytes (including generated output). */
export const BUNDLE_LIMITS = Object.freeze({
  files: 256,
  plugins: 64,
  dependencies: 128,
  fileBytes: 1024 * 1024,
  totalBytes: 8 * 1024 * 1024,
  provenanceBytes: 256 * 1024,
  pathBytes: 512,
})

const ROOT_METADATA = new Set([
  'package.json', 'cordis.patch.yml', 'readme.md', 'provenance.json',
  'source-notice.md', 'icon.svg', 'locale',
])
const PACKAGE_CONTROL_FILES = new Set([
  'package.json', 'package-lock.json', 'npm-shrinkwrap.json', 'pnpm-lock.yaml',
  'pnpm-workspace.yaml', 'yarn.lock', '.npmrc', '.yarnrc', '.yarnrc.yml',
  '.pnpmfile.cjs', '.npmignore', '.gitignore', 'binding.gyp',
])
const CONTROL_DIRECTORIES = new Set(['node_modules', '.git', '.hg', '.svn'])
const UNSAFE_NAMES = new Set(['__proto__', 'prototype', 'constructor', 'node_modules', 'favicon.ico'])
const RESERVED_IDS = new Set(['icon', 'locale', 'package.json', ...UNSAFE_NAMES])
const PACKAGE_NAME = /^(?:@[a-z0-9][a-z0-9._-]*\/)?[a-z0-9][a-z0-9._-]*$/u
const SEMVER = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?$/u

// Original, shared artwork. No scripts, external resources, filters or animation.
const ICON = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect x="6" y="6" width="52" height="52" rx="12" fill="#163c52"/>
  <path d="M19 23h26M19 41h26M23 19v26M41 19v26" fill="none" stroke="#79d8bf" stroke-width="4" stroke-linecap="round"/>
  <circle cx="32" cy="32" r="6" fill="#ffffff"/>
</svg>
`

function invalid(label, reason) {
  throw new TypeError(`Invalid bundle ${label}: ${reason}`)
}

function size(value, max, label) {
  if (Buffer.byteLength(value, 'utf8') > max) invalid(label, `size exceeds ${max} UTF-8 bytes`)
  return value
}

/**
 * Accept JSON data, not behavior. In particular JSON.stringify must never call
 * an input getter/toJSON, and even reflective operations must not trigger proxies.
 */
function snapshotJson(input, label, byteLimit) {
  let bytes = 0
  let nodes = 0
  const ancestors = new Set()
  const charge = value => {
    bytes += Buffer.byteLength(value, 'utf8')
    if (bytes > byteLimit) invalid(label, `JSON data size exceeds ${byteLimit} bytes`)
  }
  const visit = (value, at, depth) => {
    if (++nodes > 20000 || depth > 32) invalid(at, 'JSON data count/depth limit exceeded')
    if (typeof value === 'string') {
      if (!value.isWellFormed()) invalid(at, 'JSON text must contain well-formed Unicode')
      charge(value)
      return value
    }
    if (value === null || typeof value === 'boolean') return value
    if (typeof value === 'number' && Number.isFinite(value)) return value
    if (typeof value !== 'object') invalid(at, 'expected plain JSON data, not functions or undefined')
    if (types.isProxy(value)) invalid(at, 'Proxy objects are not JSON data')
    if (ancestors.has(value)) invalid(at, 'cyclic JSON data is not supported')
    const array = Array.isArray(value)
    const prototype = Object.getPrototypeOf(value)
    if (prototype !== (array ? Array.prototype : Object.prototype) && !(prototype === null && !array)) {
      invalid(at, 'expected plain JSON data')
    }
    const descriptors = Object.getOwnPropertyDescriptors(value)
    const keys = Reflect.ownKeys(descriptors)
    ancestors.add(value)
    const read = key => {
      const descriptor = descriptors[key]
      if (!descriptor || !Object.hasOwn(descriptor, 'value') || !descriptor.enumerable) {
        invalid(at, 'accessors/getters and hidden properties are not JSON data')
      }
      return visit(descriptor.value, `${at}.${String(key)}`, depth + 1)
    }
    let result
    if (array) {
      const length = descriptors.length.value
      if (length > 20000 || keys.length !== length + 1 ||
          keys.some(key => key !== 'length' && (typeof key !== 'string' || !/^(0|[1-9]\d*)$/u.test(key) || Number(key) >= length))) {
        invalid(at, 'expected a dense JSON array without extra fields')
      }
      result = Array.from({ length }, (_, index) => read(String(index)))
    } else {
      if (keys.some(key => typeof key !== 'string')) invalid(at, 'symbol fields are not JSON data')
      result = Object.fromEntries(keys.sort().map(key => {
        charge(key)
        return [key, read(key)]
      }))
    }
    ancestors.delete(value)
    return result
  }
  return visit(input, label, 0)
}

function fields(value, label, required, optional = []) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) invalid(label, 'expected a JSON object')
  const allowed = new Set([...required, ...optional])
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) invalid(label, `unknown field ${JSON.stringify(key)}`)
  }
  for (const key of required) {
    if (!Object.hasOwn(value, key)) invalid(label, `missing required field ${key}`)
  }
}

function text(value, label, max) {
  if (typeof value !== 'string' || !value.trim() || value.includes('\0')) invalid(label, 'expected non-empty text without NUL')
  return size(value, max, label)
}

function list(value, label, max, nonempty = false) {
  if (!Array.isArray(value) || (nonempty && !value.length)) invalid(label, 'expected a non-empty array')
  if (value.length > max) invalid(label, `count exceeds limit ${max}`)
  return value
}

function packageName(value, label) {
  text(value, label, 214)
  if (value.trim() !== value || !PACKAGE_NAME.test(value) || UNSAFE_NAMES.has(value) || isBuiltin(value)) {
    invalid(label, 'expected a safe lowercase npm package name')
  }
  return value
}

function exactVersion(value, label) {
  text(value, label, 256)
  const match = SEMVER.exec(value)
  if (value.trim() !== value || !match || match.slice(1, 4).some(part => !Number.isSafeInteger(Number(part)))) {
    invalid(label, 'expected an exact npm semver, not a range, URL, alias or install spec')
  }
  return value
}

function relativePath(value, label) {
  text(value, label, BUNDLE_LIMITS.pathBytes)
  if (value.startsWith('/') || /[\\\u0000-\u001f\u007f:%<>"|?*#!\[\]{}()]/u.test(value)) {
    invalid(label, 'path must be relative, without backslashes, URL/glob syntax or control characters')
  }
  const path = value.startsWith('./') ? value.slice(2) : value
  for (const segment of path.split('/')) {
    if (!segment || segment === '.' || segment === '..' || segment.trim() !== segment ||
        segment.endsWith('.') || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/iu.test(segment)) {
      invalid(label, 'path has traversal, empty or reserved segments')
    }
    size(segment, 255, `${label} path segment`)
  }
  return path
}

const pathKey = path => path.normalize('NFC').toLowerCase()
const json = value => `${JSON.stringify(value, null, 2)}\n`
const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0

function sourceFiles(value) {
  const files = list(value, 'files', BUNDLE_LIMITS.files, true)
  const seen = new Set()
  return files.map((item, index) => {
    const label = `files[${index}]`
    fields(item, label, ['path', 'content'])
    const path = relativePath(item.path, `${label}.path`)
    const key = pathKey(path)
    const parts = key.split('/')
    if (ROOT_METADATA.has(parts[0]) || parts.some(part => PACKAGE_CONTROL_FILES.has(part) || CONTROL_DIRECTORIES.has(part))) {
      invalid(`${label}.path`, 'reserved metadata or package-manager control path')
    }
    for (const previous of seen) {
      if (previous === key) invalid(`${label}.path`, 'duplicate file path (including case/Unicode aliases)')
      if (previous.startsWith(`${key}/`) || key.startsWith(`${previous}/`)) invalid(`${label}.path`, 'file/directory path conflict')
    }
    seen.add(key)
    return { path, content: text(item.content, `${label}.content`, BUNDLE_LIMITS.fileBytes) }
  })
}

function pluginRows(value, name, files) {
  const plugins = list(value, 'plugins', BUNDLE_LIMITS.plugins, true)
  const available = new Set(files.map(item => item.path))
  const seen = new Set()
  return plugins.map((item, index) => {
    const label = `plugins[${index}]`
    fields(item, label, ['id', 'title', 'description', 'entry'])
    const id = text(item.id, `${label}.id`, 80)
    if (id.trim() !== id || !/^[a-z0-9][a-z0-9._-]*$/u.test(id) || RESERVED_IDS.has(id)) {
      invalid(`${label}.id`, 'invalid or reserved plugin id')
    }
    relativePath(id, `${label}.id`)
    if (seen.has(id)) invalid(`${label}.id`, 'duplicate plugin id')
    seen.add(id)
    const entry = relativePath(item.entry, `${label}.entry`)
    if (!/\.(?:js|mjs)$/u.test(entry) || !available.has(entry)) {
      invalid(`${label}.entry`, 'entry must be a listed relative .js or .mjs file')
    }
    return {
      id,
      rowId: `${name}/${id}`,
      name: `${name}/${id}`,
      title: text(item.title, `${label}.title`, 512),
      description: text(item.description, `${label}.description`, 16384),
      entry,
    }
  })
}

function dependencies(value, name) {
  const seen = new Set()
  return Object.fromEntries(list(value, 'dependencies', BUNDLE_LIMITS.dependencies).map((item, index) => {
    const label = `dependencies[${index}]`
    fields(item, label, ['name', 'version'])
    const dependency = packageName(item.name, `${label}.name`)
    if (dependency === name) invalid(label, 'self dependency is not allowed')
    if (seen.has(dependency)) invalid(label, 'duplicate dependency')
    seen.add(dependency)
    return [dependency, exactVersion(item.version, `${label}.version`)]
  }).sort(([a], [b]) => compare(a, b)))
}

// Caller text is documentation, not active HTML, Markdown links or directives.
function markdown(value) {
  return value.replace(/&/gu, '&amp;').replace(/</gu, '&lt;').replace(/>/gu, '&gt;')
    .replace(/[\\`*_{}\[\]()#+.!|~-]/gu, '\\$&').replace(/\r?\n/gu, ' ')
}

function readme(name, description, plugins) {
  return `# ${markdown(name)} — DSH 适配 bundle

${markdown(description)}

## 范围

这是已经完成源码适配的工程文件的原生 bundle 包装，不是自动把任意源码转换为插件的转换器。
输入源码按原文保留；包装器没有导入、执行或验收这些 JavaScript，也不提供另一个运行器。
MCP、skill 注册和工具执行由适配文件使用 DSH 原生能力完成；仅把文件放进包不代表已经接入。

## 原生接入参考

用 Host 的 skill 工具读取 cordis-plugin-development，再按它返回的基目录读取
references/host-plugin.md 和 references/practices.md，不猜测安装路径。
访问 ctx.tools 等 Cordis 服务需要原生依赖声明／注入；服务注入不等于工具执行授权。
模块导入、fiber 激活和真实调用是不同事实；还应观察每个插件卸载后的资源清理。
仅查看已挂载行可使用官方 cordis_inspect_query；安装和开关仍走 plugin_manager 的原生权限。
包装器不会猜测服务依赖、自动补 inject，或执行适配代码来替作者判断成功。

## 验收状态

- installed: unverified（未验收）
- active: unverified（未验收）
- called: unverified（未验收）

生成清单和哈希不代表安装、激活、显示、业务能力或调用成功。

## 工作目录安装与独立启停

将返回的 files 写入一个独立的工作目录，保留相对目录结构。审核适配代码和依赖后，
通过原生 plugin_manager 安装；target 使用 file: 加该工作目录的绝对路径，路径中的空格原样保留：

\`\`\`json
{"action":"install_bundle","target":"file:/absolute/workspace/adapted-bundle","enabled":false}
\`\`\`

file:目录或已审核的 tar 归档按包安装并处理声明依赖；这不保证依赖可信、构建获准或运行兼容。
pnpm 裸目录可能成为开发 link:，不会递归安装链接目标的依赖；仅在目标的真实解析位置已准备好依赖时使用。
file:目录仍可变化，不是内容哈希；归档也应核对具体字节。写入目录本身仍是普通绝对路径，不加 file:。
安装单位是整个 bundle 包；subpath 不是独立 npm 包，不能把某一项的模块名当作独立安装目标。
初始保持 bundle 禁用；禁用不等于禁止依赖构建脚本，构建许可仍由原生策略和审批决定，不编造 approvedBuilds。
安装、选择、激活和调用是不同事实：set_bundle 选择该层时会尝试加载其中默认启用的条目，不能代替单项开关。
安装和开关各自走原生审批；加载失败也可能已保存选择，须保留 changed、application 和诊断，不推断自动回滚。
不要手改 profile 的 package.json / cordis.patch.yml，也不要另造启动器或在 profile 中运行包管理器。
安装结果的 application、warnings、待审批构建项和重启要求必须如实保留；不能把安装结果当作调用验收。
声明 patch rowId / moduleName 不是运行 entryId。disabled 的 bundle 可以没有 entryId，不要猜或把声明行当作运行条目。
使用原生 plugin_manager 的 list_plugins 实际返回的 entryId，再用 set_plugin 独立开关；没有条目时如实保留未挂载/加载诊断。

${plugins.map(plugin => `- **${markdown(plugin.title)}** — ${markdown(plugin.description)}\n  - 声明 patch rowId / moduleName（不是运行 entryId）：\`${plugin.rowId}\`\n  - 入口：\`${markdown(plugin.entry)}\``).join('\n')}

每项分别验收安装状态、激活状态、显示名称/图标和真实调用；当前这些检查都没有执行。
中英文 locale 均保留调用方提供的文字，并不声称完成翻译。

## 来源、许可与安全

provenance.json 原样保留调用方提供的 JSON 来源事实，未自行联网核验，也不补造许可证或权利声明。
许可边界见 SOURCE-NOTICE.md；只有调用方提供 LICENSE 时才包含其原文。
本包没有 prepare、postinstall 或其他安装 scripts；直接依赖只接受固定 npm semver。
本包装只把 spec.dependencies 的精确版本写入 dependencies，不会扫描 imports 或自动补齐运行时依赖。
调用方须依据适配源码列出实际使用的 npm 包及精确版本。本包装不生成 peerDependencies 或 engines，
因此不声明或验证宿主运行时的 peer 兼容性；固定直接依赖不能代替该项检查。
这不等于依赖可信：传递依赖、依赖安装脚本、适配代码行为和运行兼容性都未审计，安装仍走原生审批。
`
}

/**
 * Package already-adapted Host plugin files. Synchronous and data-only: no I/O,
 * installation, source conversion, evaluation, syntax checking or license inference.
 *
 * @param {object} spec package_name, version, description, plugins, files;
 *   optional dependencies [{name, version}] and caller-declared license.
 * @param {object[]} provenance Caller-supplied JSON facts, stored without inference.
 * @returns {{files: {path: string, content: string, sha256: string}[],
 *   packageName: string, version: string,
 *   plugins: {id: string, rowId: string, name: string, title: string,
 *     description: string, entry: string}[], totalBytes: number,
 *   verification: {installed: string, active: string, called: string}}}
 * @throws {TypeError} Invalid data, unsafe paths/fields or exceeded packaging limits.
 */
export function buildBundle(spec, provenance = []) {
  const input = snapshotJson(spec, 'spec', BUNDLE_LIMITS.totalBytes + 1024 * 1024)
  fields(input, 'spec', ['package_name', 'version', 'description', 'plugins', 'files'], ['dependencies', 'license'])
  const name = packageName(input.package_name, 'package_name')
  const version = exactVersion(input.version, 'version')
  const description = text(input.description, 'description', 16384)
  const sources = sourceFiles(input.files)
  const plugins = pluginRows(input.plugins, name, sources)
  const declaredDependencies = dependencies(Object.hasOwn(input, 'dependencies') ? input.dependencies : [], name)
  const facts = snapshotJson(provenance, 'provenance', BUNDLE_LIMITS.provenanceBytes)
  list(facts, 'provenance', 20000)
  const provenanceText = size(json(facts), BUNDLE_LIMITS.provenanceBytes, 'provenance')

  const exports = {
    '.': `./${plugins[0].entry}`,
    './package.json': './package.json',
    './locale/*.json': './locale/*.json',
    './icon': './icon.svg',
  }
  const emitted = new Map(sources.map(item => [item.path, item.content]))
  const locale = (path, title, summary) => emitted.set(path, json({ meta: { title, description: summary } }))
  for (const language of ['en', 'zh']) locale(`locale/${language}.json`, name, description)
  for (const plugin of plugins) {
    exports[`./${plugin.id}`] = `./${plugin.entry}`
    exports[`./${plugin.id}/locale/*.json`] = `./locale/plugins/${plugin.id}/*.json`
    exports[`./${plugin.id}/icon`] = './icon.svg'
    for (const language of ['en', 'zh']) locale(`locale/plugins/${plugin.id}/${language}.json`, plugin.title, plugin.description)
  }
  // JSON is a literal subset of the official Loader YAML dialect: no !!js evaluation.
  emitted.set('cordis.patch.yml', json([{
    insert: plugins.map(plugin => ({ id: plugin.rowId, name: plugin.name, config: {} })),
  }]))
  emitted.set('icon.svg', ICON)
  emitted.set('provenance.json', provenanceText)
  emitted.set('README.md', readme(name, description, plugins))
  emitted.set('SOURCE-NOTICE.md', `# 来源与许可说明

本目录是调用方已适配工程的包装。来源事实见 provenance.json；没有来源事实时该文件为 []。
本包装不会替原项目或适配代码授予许可，不从包名、URL、代码或来源摘要推断许可证。
${Object.hasOwn(input, 'license') ? 'package.json 中的 license 仅为调用方明确提供的声明，未经核验。' : '调用方未提供 license 字段，因此 package.json 不包含许可证声明。'}
${emitted.has('LICENSE') ? 'LICENSE 为调用方提供的原文；本包装未修改，也未核验适用范围。' : '调用方未提供 LICENSE 正文，因此本包装不生成 LICENSE。'}
使用、改编、分发前须自行核对各来源和依赖的许可证及归属要求。
共用 icon.svg 为本包装生成的简单原创图形，不代表任何上游项目标识或授权。
`)
  const paths = [...emitted.keys(), 'package.json'].sort(compare)
  const pkg = {
    name, version, description, private: true, type: 'module', exports, files: paths,
    dsh: { bundle: { patch: './cordis.patch.yml' } },
  }
  if (Object.keys(declaredDependencies).length) pkg.dependencies = declaredDependencies
  if (Object.hasOwn(input, 'license')) pkg.license = text(input.license, 'license', 1024)
  emitted.set('package.json', json(pkg))
  let totalBytes = 0
  const files = paths.map(path => {
    const content = size(emitted.get(path), BUNDLE_LIMITS.fileBytes, path)
    totalBytes += Buffer.byteLength(content, 'utf8')
    if (totalBytes > BUNDLE_LIMITS.totalBytes) invalid('files', `total output size exceeds ${BUNDLE_LIMITS.totalBytes} bytes`)
    return { path, content, sha256: createHash('sha256').update(content, 'utf8').digest('hex') }
  })
  return {
    files, packageName: name, version, plugins, totalBytes,
    verification: { installed: 'unverified', active: 'unverified', called: 'unverified' },
  }
}
