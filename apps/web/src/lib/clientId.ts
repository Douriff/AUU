/** Random alphanumeric id for idempotent order / close requests (generated once per user intent). */
export function newClientId(prefix = "", len = 20): string {
  const a = new Uint8Array(len);
  crypto.getRandomValues(a);
  return prefix + Array.from(a, (b) => (b % 36).toString(36)).join("");
}
