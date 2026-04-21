//! zmm_telemetry — fast DuckDB appender for ZigBee-Matter-Manager telemetry.
//!
//! Public API (Python-facing):
//!     Appender(db_path) -> Appender
//!     Appender.append_device_state(ieee, attribute, value, numeric_val)
//!     Appender.append_packet_stats(ieee, rx_p, tx_p, rx_b, tx_b, errors, retries, lqi)
//!     Appender.append_system_metrics(metrics_dict)
//!     Appender.append_spectrum_scan(channel, energy)
//!     Appender.flush()              -> drains all buffers
//!     Appender.pending() -> dict    -> per-table buffer counts (debug)

use chrono::Utc;
use duckdb::{params, Connection};
use parking_lot::Mutex;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

const AUTO_FLUSH_THRESHOLD: usize = 1000;

// ───────────────────────── row buffers ─────────────────────────

struct DeviceStateRow {
    ts: chrono::DateTime<Utc>,
    ieee: String,
    attribute: String,
    value: Option<String>,
    numeric_val: Option<f64>,
}

struct PacketStatRow {
    ts: chrono::DateTime<Utc>,
    ieee: String,
    rx_packets: i64,
    tx_packets: i64,
    rx_bytes: i64,
    tx_bytes: i64,
    errors: i32,
    retries: i32,
    lqi: i32,
}

struct SystemMetricRow {
    ts: chrono::DateTime<Utc>,
    cpu_percent: Option<f32>,
    cpu_freq: Option<f32>,
    mem_total: Option<i64>,
    mem_used: Option<i64>,
    mem_percent: Option<f32>,
    swap_used: Option<i64>,
    swap_percent: Option<f32>,
    disk_total: Option<i64>,
    disk_used: Option<i64>,
    disk_percent: Option<f32>,
    cpu_temp: Option<f32>,
    gpu_temp: Option<f32>,
    load_1m: Option<f32>,
    load_5m: Option<f32>,
    load_15m: Option<f32>,
    uptime_secs: Option<i64>,
    process_rss: Option<i64>,
    process_threads: Option<i32>,
}

struct SpectrumRow {
    ts: chrono::DateTime<Utc>,
    channel: i32,
    energy: i32,
}

// ───────────────────────── inner state ─────────────────────────

struct Inner {
    conn: Connection,
    device_states: Vec<DeviceStateRow>,
    packet_stats: Vec<PacketStatRow>,
    system_metrics: Vec<SystemMetricRow>,
    spectrum: Vec<SpectrumRow>,
}

impl Inner {
    fn new(db_path: &str) -> duckdb::Result<Self> {
        let conn = Connection::open(db_path)?;
        Ok(Self {
            conn,
            device_states: Vec::with_capacity(AUTO_FLUSH_THRESHOLD),
            packet_stats: Vec::with_capacity(AUTO_FLUSH_THRESHOLD),
            system_metrics: Vec::with_capacity(64),
            spectrum: Vec::with_capacity(256),
        })
    }

    fn flush_device_states(&mut self) -> duckdb::Result<()> {
        if self.device_states.is_empty() {
            return Ok(());
        }
        let mut app = self.conn.appender("device_states")?;
        for r in self.device_states.drain(..) {
            app.append_row(params![r.ts, r.ieee, r.attribute, r.value, r.numeric_val])?;
        }
        app.flush()?;
        Ok(())
    }

    fn flush_packet_stats(&mut self) -> duckdb::Result<()> {
        if self.packet_stats.is_empty() {
            return Ok(());
        }
        let mut app = self.conn.appender("packet_stats")?;
        for r in self.packet_stats.drain(..) {
            app.append_row(params![
                r.ts, r.ieee,
                r.rx_packets, r.tx_packets, r.rx_bytes, r.tx_bytes,
                r.errors, r.retries, r.lqi,
            ])?;
        }
        app.flush()?;
        Ok(())
    }

    fn flush_system_metrics(&mut self) -> duckdb::Result<()> {
        if self.system_metrics.is_empty() {
            return Ok(());
        }
        let mut app = self.conn.appender("system_metrics")?;
        for r in self.system_metrics.drain(..) {
            app.append_row(params![
                r.ts,
                r.cpu_percent, r.cpu_freq,
                r.mem_total, r.mem_used, r.mem_percent,
                r.swap_used, r.swap_percent,
                r.disk_total, r.disk_used, r.disk_percent,
                r.cpu_temp, r.gpu_temp,
                r.load_1m, r.load_5m, r.load_15m,
                r.uptime_secs, r.process_rss, r.process_threads,
            ])?;
        }
        app.flush()?;
        Ok(())
    }

    fn flush_spectrum(&mut self) -> duckdb::Result<()> {
        if self.spectrum.is_empty() {
            return Ok(());
        }
        let mut app = self.conn.appender("spectrum_scans")?;
        for r in self.spectrum.drain(..) {
            app.append_row(params![r.ts, r.channel, r.energy])?;
        }
        app.flush()?;
        Ok(())
    }

    fn flush_all(&mut self) -> duckdb::Result<()> {
        self.flush_device_states()?;
        self.flush_packet_stats()?;
        self.flush_system_metrics()?;
        self.flush_spectrum()?;
        Ok(())
    }
}

// ───────────────────────── PyO3 wrapper ─────────────────────────

#[pyclass]
struct Appender {
    inner: Mutex<Inner>,
}

fn db_err(e: duckdb::Error) -> PyErr {
    PyRuntimeError::new_err(format!("duckdb: {e}"))
}

#[pymethods]
impl Appender {
    #[new]
    fn new(db_path: &str) -> PyResult<Self> {
        let inner = Inner::new(db_path).map_err(db_err)?;
        Ok(Self { inner: Mutex::new(inner) })
    }

    fn append_device_state(
        &self,
        ieee: String,
        attribute: String,
        value: Option<String>,
        numeric_val: Option<f64>,
    ) -> PyResult<()> {
        let mut g = self.inner.lock();
        g.device_states.push(DeviceStateRow {
            ts: Utc::now(),
            ieee,
            attribute,
            value,
            numeric_val,
        });
        if g.device_states.len() >= AUTO_FLUSH_THRESHOLD {
            g.flush_device_states().map_err(db_err)?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn append_packet_stats(
        &self,
        ieee: String,
        rx_packets: i64,
        tx_packets: i64,
        rx_bytes: i64,
        tx_bytes: i64,
        errors: i32,
        retries: i32,
        lqi: i32,
    ) -> PyResult<()> {
        let mut g = self.inner.lock();
        g.packet_stats.push(PacketStatRow {
            ts: Utc::now(),
            ieee, rx_packets, tx_packets, rx_bytes, tx_bytes, errors, retries, lqi,
        });
        if g.packet_stats.len() >= AUTO_FLUSH_THRESHOLD {
            g.flush_packet_stats().map_err(db_err)?;
        }
        Ok(())
    }

    fn append_system_metrics(&self, metrics: &Bound<'_, PyDict>) -> PyResult<()> {
        // Helpers to extract optional typed values from the dict
        fn opt_f32(d: &Bound<'_, PyDict>, k: &str) -> PyResult<Option<f32>> {
            match d.get_item(k)? { Some(v) if !v.is_none() => Ok(Some(v.extract::<f32>()?)), _ => Ok(None) }
        }
        fn opt_i64(d: &Bound<'_, PyDict>, k: &str) -> PyResult<Option<i64>> {
            match d.get_item(k)? { Some(v) if !v.is_none() => Ok(Some(v.extract::<i64>()?)), _ => Ok(None) }
        }
        fn opt_i32(d: &Bound<'_, PyDict>, k: &str) -> PyResult<Option<i32>> {
            match d.get_item(k)? { Some(v) if !v.is_none() => Ok(Some(v.extract::<i32>()?)), _ => Ok(None) }
        }

        let row = SystemMetricRow {
            ts: Utc::now(),
            cpu_percent:  opt_f32(metrics, "cpu_percent")?,
            cpu_freq:     opt_f32(metrics, "cpu_freq")?,
            mem_total:    opt_i64(metrics, "mem_total")?,
            mem_used:     opt_i64(metrics, "mem_used")?,
            mem_percent:  opt_f32(metrics, "mem_percent")?,
            swap_used:    opt_i64(metrics, "swap_used")?,
            swap_percent: opt_f32(metrics, "swap_percent")?,
            disk_total:   opt_i64(metrics, "disk_total")?,
            disk_used:    opt_i64(metrics, "disk_used")?,
            disk_percent: opt_f32(metrics, "disk_percent")?,
            cpu_temp:     opt_f32(metrics, "cpu_temp")?,
            gpu_temp:     opt_f32(metrics, "gpu_temp")?,
            load_1m:      opt_f32(metrics, "load_1m")?,
            load_5m:      opt_f32(metrics, "load_5m")?,
            load_15m:     opt_f32(metrics, "load_15m")?,
            uptime_secs:  opt_i64(metrics, "uptime_secs")?,
            process_rss:  opt_i64(metrics, "process_rss")?,
            process_threads: opt_i32(metrics, "process_threads")?,
        };

        let mut g = self.inner.lock();
        g.system_metrics.push(row);
        if g.system_metrics.len() >= 64 {
            g.flush_system_metrics().map_err(db_err)?;
        }
        Ok(())
    }

    fn append_spectrum_scan(&self, channel: i32, energy: i32) -> PyResult<()> {
        let mut g = self.inner.lock();
        g.spectrum.push(SpectrumRow { ts: Utc::now(), channel, energy });
        if g.spectrum.len() >= 256 {
            g.flush_spectrum().map_err(db_err)?;
        }
        Ok(())
    }

    fn flush(&self) -> PyResult<()> {
        self.inner.lock().flush_all().map_err(db_err)
    }

    fn pending<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let g = self.inner.lock();
        let d = PyDict::new(py);
        d.set_item("device_states", g.device_states.len())?;
        d.set_item("packet_stats", g.packet_stats.len())?;
        d.set_item("system_metrics", g.system_metrics.len())?;
        d.set_item("spectrum_scans", g.spectrum.len())?;
        Ok(d)
    }
}

#[pymodule]
fn zmm_telemetry(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Appender>()?;
    Ok(())
}