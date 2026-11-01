/** Parser errors may quote private input fragments. Only expose a static source label. */
export function parseJson(raw, source) {
  try {
    return JSON.parse(raw)
  } catch {
    throw new Error(`${source} is not valid JSON`)
  }
}
