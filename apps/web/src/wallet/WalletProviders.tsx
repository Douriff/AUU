import { useMemo, type ComponentType, type ReactNode } from "react";
import { WalletAdapterNetwork } from "@solana/wallet-adapter-base";
import { ConnectionProvider, WalletProvider } from "@solana/wallet-adapter-react";
import { WalletModalProvider } from "@solana/wallet-adapter-react-ui";
import { PhantomWalletAdapter } from "@solana/wallet-adapter-phantom";
import { SolflareWalletAdapter } from "@solana/wallet-adapter-solflare";
import "@solana/wallet-adapter-react-ui/styles.css";

const ENDPOINT = import.meta.env.VITE_SOLANA_RPC_DEVNET || "https://api.devnet.solana.com";

const Connection = ConnectionProvider as unknown as ComponentType<{ endpoint: string; children?: ReactNode }>;
const Wallets = WalletProvider as unknown as ComponentType<{ wallets: unknown[]; autoConnect?: boolean; children?: ReactNode }>;
const Modal = WalletModalProvider as unknown as ComponentType<{ children?: ReactNode }>;

export function WalletProviders({ children }: { children: ReactNode }) {
  const wallets = useMemo(
    () => [new PhantomWalletAdapter(), new SolflareWalletAdapter({ network: WalletAdapterNetwork.Devnet })],
    [],
  );
  return (
    <Connection endpoint={ENDPOINT}>
      <Wallets wallets={wallets} autoConnect={false}>
        <Modal>{children}</Modal>
      </Wallets>
    </Connection>
  );
}
