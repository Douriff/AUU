import { Buffer } from "buffer";

const root = globalThis as { Buffer?: typeof Buffer; process?: { env: Record<string, string | undefined> } };

if (!root.Buffer) root.Buffer = Buffer;
if (!root.process) root.process = { env: {} };
