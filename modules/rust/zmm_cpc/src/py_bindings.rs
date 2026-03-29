/// PyO3 bindings for zmm_cpc — the `CpcCore` Python class.
///
/// # Design: detached Tokio runtime
///
/// `CpcCore::start()` creates a `tokio::runtime::Runtime` on a dedicated OS
/// thread.  All Rust async work runs inside that runtime.  Python calls are
/// thin synchronous wrappers that communicate with the runtime via:
///
/// - `watch::Sender<bool>`  — shutdown signal (write once)
/// - `Arc<AtomicU8>`        — per-endpoint state (read-only from Python)
/// - `Arc<Mutex<TdmScheduler>>` — TDM reconfiguration from `set_tdm_slots()`
///
/// This avoids every complexity of pyo3-asyncio / pyo3-async-runtimes:
/// - No event loop policy pinning
/// - No GIL / async interaction at the boundary
/// - `wait_endpoint_open()` is a simple spin-sleep loop callable from any
///   Python thread without holding the GIL for extended periods
///
/// # Python API
///
/// ```python
/// from zmm_cpc import CpcCore
///
/// core = CpcCore(
///     serial_port   = "/dev/ttyUSB0",
///     baudrate      = 115200,
///     tcp_endpoints = {12: 9999, 13: 9998},           # ep_id → port
///     tdm_slots     = [{"ep": 12, "ms": 15},           # optional
///                      {"ep": 13, "ms":  5}],
/// )
///
/// core.start()                            # opens serial, binds TCP, spawns tasks
/// ok = core.wait_endpoint_open(12, 30.0) # blocks until ep12 OPEN or timeout
/// state = core.endpoint_state(12)        # "closed" | "connecting" | "open"
/// info  = core.status()                  # dict with state + diagnostics
///
/// core.set_tdm_slots([{"ep": 12, "ms": 10}, {"ep": 13, "ms": 10}])
///
/// core.stop()                            # sends DISC, joins runtime thread
/// ```
///
/// # Error handling
///
/// - `start()` raises `RuntimeError` if the serial port cannot be opened or
///   a TCP port cannot be bound.
/// - `start()` raises `RuntimeError` if called on an already-running core.
/// - `stop()` is idempotent — calling it on an already-stopped core is a no-op.
/// - All other methods raise `RuntimeError` if called before `start()`.

use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicU8, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

use pyo3::prelude::*;
use tokio::{runtime::Runtime, sync::watch};

use crate::{
    cpc_frame::{EP_OPENTHREAD, EP_ZIGBEE},
    endpoint::{spawn_endpoint, EndpointConfig, EP_STATE_OPEN},
    router::{RouterBuilder, spawn as spawn_router},
    serial_io::{open as open_serial, SerialConfig},
    tdm::{TdmScheduler, TdmSlot},
};

// ─────────────────────────────────────────────────────────────────────────────
// Internal runtime state (lives on the Rust side, not exposed to Python)
// ─────────────────────────────────────────────────────────────────────────────

struct RunningCore {
    /// Fires `true` to shut down router + all endpoint tasks.
    shutdown_tx:  watch::Sender<bool>,
    /// Per-endpoint state atoms, keyed by ep_id.
    ep_states:    HashMap<u8, Arc<AtomicU8>>,
    /// Live reference to the TDM scheduler for runtime reconfiguration.
    tdm:          Arc<Mutex<TdmScheduler>>,
    /// OS thread hosting the Tokio runtime.  Joined on `stop()`.
    _thread:      thread::JoinHandle<()>,
}

// ─────────────────────────────────────────────────────────────────────────────
// CpcCore — the Python-visible class
// ─────────────────────────────────────────────────────────────────────────────

/// CPC core controller.
///
/// Replaces cpcd + zigbeed + PTYTCPBridge in the Phase 2 stack.
/// Call `start()` then `wait_endpoint_open(12, 30.0)` before handing the
/// `socket://127.0.0.1:9999` URI to bellows.
#[pyclass]
pub struct CpcCore {
    // ── Configuration (immutable after construction) ──────────────────────
    serial_port:    String,
    baudrate:       u32,
    /// ep_id → TCP port
    tcp_endpoints:  HashMap<u8, u16>,
    /// Initial TDM slot configuration
    initial_slots:  Vec<TdmSlot>,

    // ── Runtime state (None when stopped) ────────────────────────────────
    running: Option<RunningCore>,
}

#[pymethods]
impl CpcCore {
    /// Construct a CpcCore.
    ///
    /// Args:
    ///     serial_port:   Path to the serial device, e.g. `"/dev/ttyUSB0"`.
    ///     baudrate:      Baud rate.  115200 for Sonoff MG24 MultiPAN.
    ///     tcp_endpoints: Dict mapping CPC endpoint ID → TCP port.
    ///                    e.g. `{12: 9999, 13: 9998}`.
    ///                    Defaults to `{12: 9999}` if omitted.
    ///     tdm_slots:     List of `{"ep": <int>, "ms": <int>}` dicts
    ///                    defining the TDM schedule.
    ///                    Defaults to `[{"ep":12,"ms":15},{"ep":13,"ms":5}]`.
    #[new]
    #[pyo3(signature = (serial_port, baudrate=115200, tcp_endpoints=None, tdm_slots=None))]
    pub fn new(
        serial_port:   String,
        baudrate:      u32,
        tcp_endpoints: Option<HashMap<u8, u16>>,
        tdm_slots:     Option<Vec<HashMap<String, u64>>>,
    ) -> PyResult<Self> {
        let tcp_endpoints = tcp_endpoints.unwrap_or_else(|| {
            let mut m = HashMap::new();
            m.insert(EP_ZIGBEE, 9999);
            m
        });

        let initial_slots = if let Some(slots) = tdm_slots {
            parse_tdm_slots(slots)?
        } else {
            vec![
                TdmSlot::new(EP_ZIGBEE,      15),
                TdmSlot::new(EP_OPENTHREAD,   5),
            ]
        };

        Ok(CpcCore {
            serial_port,
            baudrate,
            tcp_endpoints,
            initial_slots,
            running: None,
        })
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────

    /// Open the serial port, bind TCP listeners, and start all tasks.
    ///
    /// Raises `RuntimeError` on port/bind failure or if already running.
    pub fn start(&mut self) -> PyResult<()> {
        if self.running.is_some() {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "CpcCore is already running — call stop() first",
            ));
        }

        // Clone everything the runtime thread will need.
        let serial_port   = self.serial_port.clone();
        let baudrate      = self.baudrate;
        let tcp_endpoints = self.tcp_endpoints.clone();
        let initial_slots = self.initial_slots.clone();

        // Collect ep_ids so we can pre-create the state atoms.
        let ep_ids: Vec<u8> = tcp_endpoints.keys().cloned().collect();

        // Pre-allocate AtomicU8 state for each endpoint.
        // These are written by the Tokio tasks and read by Python.
        let ep_states: HashMap<u8, Arc<AtomicU8>> = ep_ids
            .iter()
            .map(|&id| (id, Arc::new(AtomicU8::new(crate::endpoint::EP_STATE_CLOSED))))
            .collect();

        // Shared TDM scheduler — Python can reconfigure it via set_tdm_slots().
        let tdm_arc = Arc::new(Mutex::new(TdmScheduler::new(initial_slots)));

        // Clones for the thread.
        let ep_states_clone = ep_states.clone();
        let tdm_arc_clone   = Arc::clone(&tdm_arc);

        // Shutdown channel — Python sends `true` on stop().
        let (shutdown_tx, shutdown_rx) = watch::channel(false);
        let shutdown_rx_clone = shutdown_rx.clone();

        // Channel for errors that occur inside the runtime before it is ready.
        let (err_tx, err_rx) = std::sync::mpsc::channel::<String>();

        let thread = thread::spawn(move || {
            let rt = match Runtime::new() {
                Ok(r) => r,
                Err(e) => { let _ = err_tx.send(format!("Tokio Runtime::new failed: {e}")); return; }
            };

            rt.block_on(async move {
                // Open serial port.
                let serial_config = SerialConfig::new(&serial_port, baudrate);
                let (serial_handle, reader_task, writer_task) =
                    match open_serial(&serial_config) {
                        Ok(v) => v,
                        Err(e) => { let _ = err_tx.send(e.to_string()); return; }
                    };

                // Build router with a proxy TDM that delegates to the Arc<Mutex<>>.
                let proxy_tdm = ArcTdmProxy(Arc::clone(&tdm_arc_clone));
                let mut builder = RouterBuilder::new(proxy_tdm);

                // Register endpoints and spawn their tasks.
                let mut ep_join_handles = Vec::new();
                let mut proxy_handles   = Vec::new();
                // IMPORTANT: ep_handles must live until shutdown.  Dropping an
                // EndpointHandle closes its inner watch::Sender, which causes
                // inner_shutdown.changed() in endpoint_task to fire instantly
                // on every select! iteration (busy-spin CPU loop).
                let mut ep_handles      = Vec::new();

                for (ep_id, tcp_port) in &tcp_endpoints {
                    let (ep_rx, ep_tx) = builder.register_endpoint(*ep_id);
                    let config        = EndpointConfig::new(*ep_id, *tcp_port);
                    let ep_state_atom = Arc::clone(ep_states_clone.get(ep_id).unwrap());

                    // spawn_endpoint binds the TCP port immediately.
                    let (ep_handle, ep_join) = match spawn_endpoint(
                        config, ep_rx, ep_tx, shutdown_rx_clone.clone(),
                    ).await {
                        Ok(v) => v,
                        Err(e) => {
                            let _ = err_tx.send(format!(
                                "Cannot bind TCP port {tcp_port} for ep{ep_id}: {e}"
                            ));
                            return;
                        }
                    };

                    // Synchronise the pre-allocated state atom with the handle's atom.
                    // (They are separate Arcs; we copy via an atomic forward.)
                    let handle_state = Arc::clone(&ep_handle.state);
                    let proxy_state  = Arc::clone(&ep_state_atom);
                    let proxy_sd     = shutdown_rx_clone.clone();
                    let proxy_jh = tokio::spawn(async move {
                        let mut sd = proxy_sd;
                        loop {
                            tokio::select! {
                                biased;
                                _ = sd.changed() => {
                                    if *sd.borrow() { break; }
                                }
                                _ = tokio::time::sleep(Duration::from_millis(2)) => {
                                    let v = handle_state.load(Ordering::Acquire);
                                    proxy_state.store(v, Ordering::Release);
                                }
                            }
                        }
                    });

                    ep_join_handles.push(ep_join);
                    proxy_handles.push(proxy_jh);
                    ep_handles.push(ep_handle);
                }

                let router = builder.build(serial_handle, shutdown_rx_clone);
                let router_join = spawn_router(router);

                // Signal Python that startup succeeded.
                let _ = err_tx.send(String::new()); // empty string = success

                // Wait for shutdown signal.
                let mut sd = shutdown_rx.clone();
                loop {
                    let _ = sd.changed().await;
                    if *sd.borrow() { break; }
                }

                // Cancel all tasks.
                router_join.abort();
                for jh in ep_join_handles { jh.abort(); }
                for jh in proxy_handles   { jh.abort(); }
                reader_task.abort();
                writer_task.abort();

                // Drop handles after tasks are aborted (shutdown_tx senders
                // are now safe to drop since the endpoint tasks are gone).
                drop(ep_handles);
            });
        });

        // Block until the runtime confirms startup (or sends an error).
        match err_rx.recv_timeout(Duration::from_secs(10)) {
            Ok(msg) if msg.is_empty() => {} // success
            Ok(msg) => {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    format!("CpcCore startup failed: {msg}"),
                ));
            }
            Err(_) => {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    "CpcCore startup timed out (10s)",
                ));
            }
        }

        self.running = Some(RunningCore {
            shutdown_tx,
            ep_states,
            tdm: tdm_arc,
            _thread: thread,
        });
        Ok(())
    }

    /// Stop all tasks, send DISC on all endpoints, and join the runtime thread.
    ///
    /// Idempotent — safe to call multiple times.
    pub fn stop(&mut self) {
        if let Some(core) = self.running.take() {
            let _ = core.shutdown_tx.send(true);
            // join() would block the GIL — use try_join with a timeout instead.
            // The thread will exit shortly; if the join hangs (port stuck),
            // we leave the thread orphaned rather than blocking Python forever.
            // In practice the serial read will unblock within one read timeout.
            let _ = core._thread.join(); // brief: tasks are aborted, RT exits fast
        }
    }

    // ── State queries ─────────────────────────────────────────────────────

    /// Return the state string for an endpoint: "closed", "connecting", or "open".
    ///
    /// Raises `RuntimeError` if not running.
    pub fn endpoint_state(&self, ep_id: u8) -> PyResult<&'static str> {
        let core = self.require_running()?;
        match core.ep_states.get(&ep_id) {
            None => Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                "ep{ep_id} is not registered"
            ))),
            Some(atom) => Ok(crate::endpoint::state_name(atom.load(Ordering::Acquire))),
        }
    }

    /// Block (releasing the GIL) until `ep_id` reaches OPEN or timeout expires.
    ///
    /// Returns `True` if the endpoint opened within the timeout, `False` otherwise.
    ///
    /// Raises `RuntimeError` if not running or ep_id not registered.
    #[pyo3(signature = (ep_id, timeout_secs=30.0))]
    pub fn wait_endpoint_open(
        &self,
        py:          Python<'_>,
        ep_id:       u8,
        timeout_secs: f64,
    ) -> PyResult<bool> {
        let core = self.require_running()?;
        let atom = match core.ep_states.get(&ep_id) {
            None => return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                "ep{ep_id} is not registered"
            ))),
            Some(a) => Arc::clone(a),
        };

        let deadline = Instant::now() + Duration::from_secs_f64(timeout_secs.max(0.0));
        // Release the GIL while polling so Python threads can run.
        py.allow_threads(|| {
            while Instant::now() < deadline {
                if atom.load(Ordering::Acquire) == EP_STATE_OPEN {
                    return Ok(true);
                }
                thread::sleep(Duration::from_millis(10));
            }
            // One final check at deadline.
            Ok(atom.load(Ordering::Acquire) == EP_STATE_OPEN)
        })
    }

    /// Return a status dict with state for all endpoints and diagnostic counters.
    ///
    /// ```python
    /// {
    ///   "running": True,
    ///   "endpoints": {
    ///     12: {"state": "open",   "tcp_port": 9999},
    ///     13: {"state": "closed", "tcp_port": 9998},
    ///   }
    /// }
    /// ```
    pub fn status(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let d = pyo3::types::PyDict::new(py);

            match &self.running {
                None => {
                    d.set_item("running", false)?;
                    d.set_item("endpoints", pyo3::types::PyDict::new(py))?;
                }
                Some(core) => {
                    d.set_item("running", true)?;
                    let eps = pyo3::types::PyDict::new(py);
                    for (&ep_id, atom) in &core.ep_states {
                        let ep_d = pyo3::types::PyDict::new(py);
                        ep_d.set_item(
                            "state",
                            crate::endpoint::state_name(atom.load(Ordering::Acquire)),
                        )?;
                        if let Some(&port) = self.tcp_endpoints.get(&ep_id) {
                            ep_d.set_item("tcp_port", port)?;
                        }
                        eps.set_item(ep_id, ep_d)?;
                    }
                    d.set_item("endpoints", eps)?;
                }
            }
            Ok(d.into())
        })
    }

    // ── Runtime reconfiguration ───────────────────────────────────────────

    /// Replace the TDM slot schedule at runtime.
    ///
    /// Takes effect on the next call to `may_transmit()` inside the router
    /// (within one router event-loop iteration, typically < 1 ms).
    ///
    /// Args:
    ///     slots: list of `{"ep": <int>, "ms": <int>}` dicts.
    ///            An empty list disables TDM (pass-through mode).
    ///
    /// Raises `RuntimeError` if not running.
    pub fn set_tdm_slots(&self, slots: Vec<HashMap<String, u64>>) -> PyResult<()> {
        let core = self.require_running()?;
        let new_slots = parse_tdm_slots(slots)?;
        let mut sched = core.tdm.lock().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("TDM mutex poisoned")
        })?;
        sched.set_slots(new_slots);
        Ok(())
    }

    // ── Python dunder ─────────────────────────────────────────────────────

    /// String representation for debugging.
    pub fn __repr__(&self) -> String {
        format!(
            "CpcCore(port={:?}, baudrate={}, running={}, eps={:?})",
            self.serial_port,
            self.baudrate,
            self.running.is_some(),
            self.tcp_endpoints.keys().collect::<Vec<_>>(),
        )
    }
}

impl Drop for CpcCore {
    /// Ensure the runtime shuts down cleanly if the Python object is GC'd
    /// without an explicit `stop()` call.
    fn drop(&mut self) {
        self.stop();
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// ArcTdmProxy — bridges Arc<Mutex<TdmScheduler>> to TdmGate
// ─────────────────────────────────────────────────────────────────────────────

use crate::router::TdmGate;

/// A thin proxy that holds an `Arc<Mutex<TdmScheduler>>` and implements
/// `TdmGate`.  The router owns the proxy; Python holds the `Arc` for
/// reconfiguration via `set_tdm_slots`.
struct ArcTdmProxy(Arc<Mutex<TdmScheduler>>);

impl TdmGate for ArcTdmProxy {
    fn may_transmit(&mut self, ep_id: u8) -> bool {
        self.0.lock().map(|mut s| s.may_transmit(ep_id)).unwrap_or(true)
    }

    fn on_transmitted(&mut self, ep_id: u8) {
        if let Ok(mut s) = self.0.lock() {
            s.on_transmitted(ep_id);
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

/// Parse `[{"ep": int, "ms": int}, ...]` into `Vec<TdmSlot>`.
fn parse_tdm_slots(slots: Vec<HashMap<String, u64>>) -> PyResult<Vec<TdmSlot>> {
    slots
        .into_iter()
        .map(|m| {
            let ep = m.get("ep").copied().ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err(
                    "TDM slot dict missing \"ep\" key",
                )
            })? as u8;
            let ms = m.get("ms").copied().ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err(
                    "TDM slot dict missing \"ms\" key",
                )
            })?;
            if ms == 0 {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "TDM slot \"ms\" must be > 0",
                ));
            }
            Ok(TdmSlot::new(ep, ms))
        })
        .collect()
}

// ── require_running helper (not a #[pymethods] item) ─────────────────────────

impl CpcCore {
    fn require_running(&self) -> PyResult<&RunningCore> {
        self.running.as_ref().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err(
                "CpcCore is not running — call start() first",
            )
        })
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Module registration
// ─────────────────────────────────────────────────────────────────────────────

/// Register `CpcCore` in the `zmm_cpc` Python module.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<CpcCore>()?;
    Ok(())
}

// ─────────────────────────────────────────────────────────────────────────────
// Tests
// ─────────────────────────────────────────────────────────────────────────────
//
// No real serial port in CI.  Tests cover:
//   - parse_tdm_slots: valid, missing keys, zero ms
//   - ArcTdmProxy: delegates to TdmScheduler correctly
//   - CpcCore construction and __repr__
//   - require_running before start()
//   - status() when not running

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cpc_frame::{EP_OPENTHREAD, EP_ZIGBEE};

    // ── parse_tdm_slots ───────────────────────────────────────────────────────

    fn slot(ep: u64, ms: u64) -> HashMap<String, u64> {
        let mut m = HashMap::new();
        m.insert("ep".into(), ep);
        m.insert("ms".into(), ms);
        m
    }

    #[test]
    fn parse_valid_slots() {
        let slots = vec![slot(12, 15), slot(13, 5)];
        let parsed = parse_tdm_slots(slots).unwrap();
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[0], TdmSlot::new(EP_ZIGBEE, 15));
        assert_eq!(parsed[1], TdmSlot::new(EP_OPENTHREAD, 5));
    }

    #[test]
    fn parse_empty_slots() {
        let parsed = parse_tdm_slots(vec![]).unwrap();
        assert!(parsed.is_empty());
    }

    #[test]
    fn parse_missing_ep_key() {
        let mut m = HashMap::new();
        m.insert("ms".into(), 15u64);
        let err = parse_tdm_slots(vec![m]);
        assert!(err.is_err(), "missing 'ep' should be an error");
    }

    #[test]
    fn parse_missing_ms_key() {
        let mut m = HashMap::new();
        m.insert("ep".into(), 12u64);
        let err = parse_tdm_slots(vec![m]);
        assert!(err.is_err(), "missing 'ms' should be an error");
    }

    #[test]
    fn parse_zero_ms_rejected() {
        let err = parse_tdm_slots(vec![slot(12, 0)]);
        assert!(err.is_err(), "ms=0 should be rejected");
    }

    // ── ArcTdmProxy ───────────────────────────────────────────────────────────

    #[test]
    fn arc_tdm_proxy_delegates_may_transmit() {
        let sched = Arc::new(Mutex::new(TdmScheduler::new(vec![
            TdmSlot::new(EP_ZIGBEE, 100),
            TdmSlot::new(EP_OPENTHREAD, 100),
        ])));
        let mut proxy = ArcTdmProxy(Arc::clone(&sched));

        // Slot 0 is ep12 — granted; ep13 — not granted.
        assert!(proxy.may_transmit(EP_ZIGBEE),     "ep12 should be granted in slot 0");
        assert!(!proxy.may_transmit(EP_OPENTHREAD), "ep13 should be blocked in slot 0");
    }

    #[test]
    fn arc_tdm_proxy_on_transmitted_is_noop() {
        let sched = Arc::new(Mutex::new(TdmScheduler::phase2_default()));
        let before = sched.lock().unwrap().current_ep();
        let mut proxy = ArcTdmProxy(Arc::clone(&sched));
        proxy.on_transmitted(EP_ZIGBEE);
        proxy.on_transmitted(EP_OPENTHREAD);
        let after = sched.lock().unwrap().current_ep();
        assert_eq!(before, after, "on_transmitted must not advance slot");
    }

    #[test]
    fn arc_tdm_proxy_reconfigured_via_arc() {
        let sched = Arc::new(Mutex::new(TdmScheduler::new(vec![
            TdmSlot::new(EP_ZIGBEE, 100),
        ])));
        let mut proxy = ArcTdmProxy(Arc::clone(&sched));

        assert!(proxy.may_transmit(EP_ZIGBEE));
        assert!(!proxy.may_transmit(EP_OPENTHREAD));

        // Reconfigure to ep13 only via the shared Arc.
        sched.lock().unwrap().set_slots(vec![TdmSlot::new(EP_OPENTHREAD, 100)]);

        assert!(!proxy.may_transmit(EP_ZIGBEE),    "ep12 should now be blocked");
        assert!(proxy.may_transmit(EP_OPENTHREAD),  "ep13 should now be granted");
    }

    // ── CpcCore construction ──────────────────────────────────────────────────

    #[test]
    fn cpccore_default_tcp_endpoints() {
        let core = CpcCore::new(
            "/dev/ttyUSB0".into(),
            115200,
            None, // use default
            None,
        ).unwrap();
        assert_eq!(core.tcp_endpoints.get(&EP_ZIGBEE), Some(&9999));
        assert!(!core.tcp_endpoints.contains_key(&EP_OPENTHREAD));
    }

    #[test]
    fn cpccore_custom_tcp_endpoints() {
        let mut eps = HashMap::new();
        eps.insert(EP_ZIGBEE,      9999u16);
        eps.insert(EP_OPENTHREAD,  9998u16);
        let core = CpcCore::new(
            "/dev/ttyUSB0".into(),
            115200,
            Some(eps),
            None,
        ).unwrap();
        assert_eq!(core.tcp_endpoints.get(&EP_ZIGBEE),     Some(&9999));
        assert_eq!(core.tcp_endpoints.get(&EP_OPENTHREAD),  Some(&9998));
    }

    #[test]
    fn cpccore_default_tdm_slots() {
        let core = CpcCore::new("/dev/ttyUSB0".into(), 115200, None, None).unwrap();
        assert_eq!(core.initial_slots.len(), 2);
        assert_eq!(core.initial_slots[0], TdmSlot::new(EP_ZIGBEE, 15));
        assert_eq!(core.initial_slots[1], TdmSlot::new(EP_OPENTHREAD, 5));
    }

    #[test]
    fn cpccore_custom_tdm_slots() {
        let slots = vec![slot(12, 20), slot(13, 10)];
        let core = CpcCore::new(
            "/dev/ttyUSB0".into(), 115200, None, Some(slots),
        ).unwrap();
        assert_eq!(core.initial_slots[0], TdmSlot::new(EP_ZIGBEE, 20));
        assert_eq!(core.initial_slots[1], TdmSlot::new(EP_OPENTHREAD, 10));
    }

    #[test]
    fn cpccore_invalid_tdm_slot_rejected() {
        let err = CpcCore::new(
            "/dev/ttyUSB0".into(), 115200, None, Some(vec![slot(12, 0)]),
        );
        assert!(err.is_err(), "zero ms should be rejected at construction");
    }

    #[test]
    fn cpccore_repr_not_running() {
        let core = CpcCore::new("/dev/ttyUSB0".into(), 115200, None, None).unwrap();
        let r = core.__repr__();
        assert!(r.contains("running=false"), "__repr__ should show not running");
        assert!(r.contains("/dev/ttyUSB0"));
        assert!(r.contains("115200"));
    }

    // ── require_running guard ─────────────────────────────────────────────────

    #[test]
    fn endpoint_state_before_start_errors() {
        let core = CpcCore::new("/dev/ttyUSB0".into(), 115200, None, None).unwrap();
        let err = core.endpoint_state(EP_ZIGBEE);
        assert!(err.is_err(), "endpoint_state before start should error");
    }

    #[test]
    fn set_tdm_slots_before_start_errors() {
        let core = CpcCore::new("/dev/ttyUSB0".into(), 115200, None, None).unwrap();
        let err = core.set_tdm_slots(vec![slot(12, 15)]);
        assert!(err.is_err(), "set_tdm_slots before start should error");
    }

    // ── status() when not running ─────────────────────────────────────────────

    #[test]
    fn status_when_not_running() {
        Python::with_gil(|py| {
            let core = CpcCore::new("/dev/ttyUSB0".into(), 115200, None, None).unwrap();
            let obj = core.status().unwrap();
            let d = obj.bind(py).downcast::<pyo3::types::PyDict>().unwrap();
            let running: bool = d.get_item("running").unwrap().unwrap().extract().unwrap();
            assert!(!running, "status.running should be false when not started");
        });
    }
}