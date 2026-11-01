/** Remove explicit inline payloads without treating ordinary long text as base64. */
export function stripDataUrls(value, omitted = () => {}) {
  // Apostrophes and parentheses can belong to a non-base64 payload. Without
  // markup context, omit trailing URL punctuation rather than leaking its tail.
  return value.replace(/\bdata:[^,\s"<>]*,[^\s"<>]*/gi, () => {
    omitted()
    return '[data URL omitted]'
  })
}
