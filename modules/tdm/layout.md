**Crate layout:**
```
zmm_cpc/
├── Cargo.toml
├── pyproject.toml          # maturin
├── src/
│   ├── lib.rs              # #[pymodule] CpcCore
│   ├── serial_io.rs        # tokio-serial read/write split
│   ├── hdlc.rs             # frame sync, CRC-16/CCITT HCS+FCS
│   ├── cpc_frame.rs        # I/S/U frame decode, ep0 UA/SABM/DISC
│   ├── router.rs           # serial ↔ endpoint dispatch + TDM gating
│   ├── endpoint.rs         # per-ep state machine + TCP listener
│   ├── tdm.rs              # fixed-slot round-robin scheduler
│   └── py_bindings.rs      # PyO3 CpcCore, detached Runtime
└── cross/
    └── aarch64.toml        # cross linker config
```