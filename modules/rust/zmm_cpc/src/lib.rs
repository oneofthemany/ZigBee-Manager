pub mod hdlc;
pub mod cpc_frame;
pub mod serial_io;
pub mod router;
pub mod tdm;
pub mod endpoint;
pub mod py_bindings;

use pyo3::prelude::*;

/// zmm_cpc — CPC/HDLC core for ZigBee Matter Manager.
///
/// Replaces cpcd + zigbeed + PTYTCPBridge with a single Rust extension module.
#[pymodule]
fn zmm_cpc(m: &Bound<'_, PyModule>) -> PyResult<()> {
    py_bindings::register(m)?;
    Ok(())
}