# Local-only live keypair (do not commit)

Place a Solana CLI JSON keypair **on this machine only**:

```text
secrets/live-keypair.json
```

Expected shape: JSON array of **64 ints** (Phantom base58 converted locally).
That file is gitignored. AUU never asks you to paste a secret. Health reports
`keypairMounted` (bool) and `pubkey` only (example `8fs58…akFi`) — never
the file bytes. `pubkeyShort` is the same shortened value.

Override path with gitignored env `AUU_SOLANA_KEYPAIR_PATH`.

`liveEnabled` stays **false** until the file is mounted **and** you confirm in
Settings **and** LiveLimits are present. This tree still sends zero chain txs.
