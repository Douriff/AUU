/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE: string;
  readonly VITE_SOLANA_RPC_DEVNET?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
