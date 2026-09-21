# Local-only live keypair (do not commit)

Place a Solana CLI JSON keypair **on this machine only**:

```text
secrets/live-keypair.json
```

That file is gitignored. AUU never asks you to paste a secret. Health reports
`keypairMounted` (bool) and `pubkeyShort` only — never the file bytes.

Override path with gitignored env `AUU_SOLANA_KEYPAIR_PATH`.

`liveEnabled` stays **false** until the file is mounted **and** you confirm in
Settings **and** LiveLimits are present. This tree still sends zero chain txs.
