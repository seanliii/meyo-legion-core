import { constants, openSync, closeSync, readSync, fstatSync, lstatSync } from 'node:fs'
import { dirname, isAbsolute } from 'node:path'

const MAX_BYTES = 4096

export function credentialFilePath(value) {
  if (typeof value !== 'string' || !isAbsolute(value) || value.includes('\0')
    || value.split(/[/\\]/u).includes('..')) {
    throw new Error('tokenFile must be an absolute user-configured file path without traversal')
  }
  return value
}

/** Read only an owner's private regular file, never a pipe, device or link. */
export function readCredentialFile(path) {
  let fd
  const bytes = Buffer.alloc(MAX_BYTES + 1)
  try {
    if (!process.getuid || constants.O_NOFOLLOW === undefined
      || lstatSync(dirname(path)).isSymbolicLink()) throw new Error()
    fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
    const before = fstatSync(fd, { bigint: true })
    if (!before.isFile() || before.uid !== BigInt(process.getuid()) || before.nlink !== 1n
      || (before.mode & 0o077n) !== 0n || before.size < 1n || before.size > BigInt(MAX_BYTES)) {
      throw new Error()
    }
    let size = 0
    while (size < bytes.length) {
      const count = readSync(fd, bytes, size, bytes.length - size)
      if (!count) break
      size += count
    }
    const after = fstatSync(fd, { bigint: true })
    const current = lstatSync(path, { bigint: true })
    const identity = ['dev', 'ino', 'size', 'mtimeNs', 'ctimeNs', 'uid', 'mode', 'nlink']
    if (size > MAX_BYTES || BigInt(size) !== before.size || current.isSymbolicLink()
      || identity.some(key => before[key] !== after[key] || after[key] !== current[key])) throw new Error()
    const token = new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(0, size)).replace(/\r?\n$/u, '')
    if (!/^[\x21-\x7e]+$/u.test(token)) throw new Error()
    return token
  } catch {
    // OS errors contain filenames; never expose them or credential bytes to a model.
    throw new Error('Legion credential file is unavailable or unsafe; use an owner-only regular file (0600), one token, at most 4096 bytes. No environment fallback was used.')
  } finally {
    bytes.fill(0)
    if (fd !== undefined) closeSync(fd)
  }
}
