/**
 * Server message nodes (apps/api/app/i18n_msg.py): a catalog key + params rendered per language.
 *   { k: "srv.ev.restart", p: {...} } | { s: "raw text" } | { j: [node...], sep: " " | node, trim?: true }
 * Unknown keys (older/newer server) fall back to the server's own text.
 */
import i18n from "i18next";

export type MsgNode =
  | { k: string; p?: Record<string, MsgParam> }
  | { s: string }
  | { j: MsgNode[]; sep?: string | MsgNode; trim?: boolean };
export type MsgParam = string | number | MsgNode;

class MissingKey extends Error {}

function render(node: MsgParam): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (!node || typeof node !== "object") return "";
  if ("s" in node) return String(node.s ?? "");
  if ("j" in node) {
    const sep = node.sep === undefined ? " " : typeof node.sep === "string" ? node.sep : render(node.sep);
    const out = (node.j || []).map(render).join(sep);
    return node.trim ? out.trim() : out;
  }
  if (!("k" in node) || !i18n.exists(node.k)) throw new MissingKey(String((node as { k?: string }).k));
  const params: Record<string, string> = {};
  for (const [key, v] of Object.entries(node.p || {})) params[key] = render(v);
  return i18n.t(node.k, { ...params, interpolation: { escapeValue: false } }) as string;
}

/** Rendered text of a server node; `fallback` (the server's text field) when absent or unknown. */
export function msgText(node: MsgNode | null | undefined, fallback = ""): string {
  if (!node) return fallback;
  try {
    return render(node);
  } catch {
    return fallback;
  }
}
