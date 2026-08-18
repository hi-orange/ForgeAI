import { readdir, readFile } from 'node:fs/promises'
import { extname, join, relative } from 'node:path'
import process from 'node:process'

const root = process.cwd()
const ignoredDirectories = new Set([
  '.git',
  '.mypy_cache',
  '.pytest_cache',
  '.ruff_cache',
  '.turbo',
  '.venv',
  '__pycache__',
  'coverage',
  'dist',
  'node_modules',
])
const textExtensions = new Set([
  '.css',
  '.env',
  '.example',
  '.html',
  '.ini',
  '.js',
  '.json',
  '.lock',
  '.md',
  '.mjs',
  '.py',
  '.toml',
  '.ts',
  '.txt',
  '.vue',
  '.yaml',
  '.yml',
])
const textFileNames = new Set([
  '.editorconfig',
  '.gitattributes',
  '.gitignore',
  '.nvmrc',
  '.prettierignore',
])
const mojibakePattern = /(?:锟斤拷|烫烫|屯屯|馃|鈥|浣犵殑|鍒涘缓|椤圭洰|璇锋|绠＄悊|\uFFFD)/u
const decoder = new TextDecoder('utf-8', { fatal: true })
const failures = []

async function scan(directory) {
  const entries = await readdir(directory, { withFileTypes: true })

  for (const entry of entries) {
    if (entry.isDirectory() && ignoredDirectories.has(entry.name)) continue

    const absolutePath = join(directory, entry.name)
    if (entry.isDirectory()) {
      await scan(absolutePath)
      continue
    }

    if (!entry.isFile()) continue
    if (!textExtensions.has(extname(entry.name).toLowerCase()) && !textFileNames.has(entry.name)) {
      continue
    }

    try {
      const content = decoder.decode(await readFile(absolutePath))
      const relativePath = relative(root, absolutePath)
      if (relativePath !== join('scripts', 'check-encoding.mjs') && mojibakePattern.test(content)) {
        failures.push(`${relativePath}: contains likely mojibake`)
      }
    } catch {
      failures.push(`${relative(root, absolutePath)}: is not valid UTF-8`)
    }
  }
}

await scan(root)

if (failures.length > 0) {
  console.error('Encoding check failed:')
  for (const failure of failures) console.error(`- ${failure}`)
  process.exitCode = 1
} else {
  console.log('Encoding check passed: all project text files are valid UTF-8.')
}
