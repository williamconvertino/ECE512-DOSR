# DOSRRegistry contract

`src/DOSRRegistry.sol` keeps, per repository, the genesis commit, the policy hash, the trusted
attestor keys and the canonical HEAD.

| Function | Effect |
|---|---|
| `registerRepository` | registers a repository's genesis, name, policy hash and attestor keys |
| `acceptTransition` | moves HEAD from parent to candidate if the attestation is approved, signed (EIP-712) by a listed attestor, not expired, for the registered policy, and its parent is the current HEAD (otherwise `StaleParent`) |
| `publishSnapshot` | records the IPFS share code (CID) of the current HEAD so others can fetch it |

The compiled artifact (ABI, bytecode and the source's sha256) is committed at
`packages/chain/dosr_chain/contract/DOSRRegistry.json`, so nothing needs to be compiled to run DOSR.

```bash
python contracts/build.py           # download solc (sha256-checked) and rebuild the artifact
python contracts/build.py --check   # fail if the artifact does not match the source
```

The demo deploys the contract on a local anvil chain (`python demo/demo.py chain`) at
`0x5FbDB2315678afecb367f032d93F642f64180aa3`.
