/// Serial I/O layer for zmm_cpc.
///
/// Owns the serial port for the lifetime of the Rust runtime.  Splits it
/// into independent async read and write halves, each running as a Tokio
/// task, and exposes them to the router via mpsc channels.
///
/// # Port configuration (MG24 MultiPAN RCP v4.6.0, confirmed Phase 1)
///
/// | Parameter    | Value              | Reason                                   |
/// |--------------|--------------------|-----------------------------------------|
/// | Baudrate     | 115200             | Sonoff MG24 MultiPAN firmware default   |
/// | Data bits    | 8                  | Standard                                |
/// | Stop bits    | 1                  | Standard                                |
/// | Parity       | None               | Standard                                |
/// | Flow control | **None**           | RTS/DTR assert → Gecko Bootloader entry |
///
/// Hardware flow control MUST NOT be enabled.  During Phase 1 diagnosis,
/// asserting RTS/DTR caused the MG24 to enter Gecko Bootloader instead of
/// the CPC firmware, requiring a manual `reset_sequence` workaround.
///
/// # Architecture
///
/// ```text
///  serial port
///   │ read bytes
///   ▼
///  reader task ──(Bytes)──▶ rx_chan ──▶ router (Framer)
///
///  router ──(Bytes)──▶ tx_chan ──▶ writer task ──▶ serial port
/// ```
///
/// Both tasks exit when their channel closes or a shutdown signal fires.
/// The router drives the channels; tasks are transparent pipes.

use std::time::Duration;

use bytes::Bytes;
use thiserror::Error;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::{mpsc, watch};
use tokio::task::JoinHandle;
use tokio_serial::{DataBits, FlowControl, Parity, SerialPortBuilderExt, StopBits};

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

/// Read buffer size in bytes.  Sized to hold several max-length CPC frames
/// without blocking the read loop.
const READ_BUF: usize = 512;

/// Bound on the inbound (serial → router) channel.
/// 256 × 512 bytes max = 128 KiB in-flight; sufficient for burst absorption.
const RX_CHAN_BOUND: usize = 256;

/// Bound on the outbound (router → serial) channel.
/// TX traffic is much lighter (RR + UA frames are 9 bytes each).
const TX_CHAN_BOUND: usize = 256;

// ─────────────────────────────────────────────────────────────────────────────
// Error type
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Error)]
pub enum SerialIoError {
    #[error("Failed to open serial port {path}: {source}")]
    Open {
        path:   String,
        source: tokio_serial::Error,
    },

    #[error("Serial read error: {0}")]
    Read(#[from] std::io::Error),
}

// ─────────────────────────────────────────────────────────────────────────────
// Configuration
// ─────────────────────────────────────────────────────────────────────────────

/// Serial port configuration.
#[derive(Debug, Clone)]
pub struct SerialConfig {
    /// Device path, e.g. `/dev/ttyUSB0` or `/dev/ttyACM0`.
    pub path: String,
    /// Baud rate.  115200 for Sonoff MG24 MultiPAN firmware.
    pub baudrate: u32,
}

impl SerialConfig {
    pub fn new(path: impl Into<String>, baudrate: u32) -> Self {
        SerialConfig { path: path.into(), baudrate }
    }

    /// Convenience constructor with the confirmed MG24 MultiPAN defaults.
    pub fn mg24_default(path: impl Into<String>) -> Self {
        Self::new(path, 115200)
    }

    /// Build and open the serial port.
    ///
    /// Flow control is explicitly forced to `None` regardless of any system
    /// default to prevent accidental RTS/DTR assertion on the MG24.
    pub(crate) fn open_port(&self) -> Result<tokio_serial::SerialStream, SerialIoError> {
        tokio_serial::new(&self.path, self.baudrate)
            .data_bits(DataBits::Eight)
            .stop_bits(StopBits::One)
            .parity(Parity::None)
            .flow_control(FlowControl::None) // CRITICAL — see module docs
            .timeout(Duration::from_millis(0))
            .open_native_async()
            .map_err(|e| SerialIoError::Open {
                path:   self.path.clone(),
                source: e,
            })
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// SerialHandle — what the router holds
// ─────────────────────────────────────────────────────────────────────────────

/// Channels and task handles returned by [`open`].
///
/// The router:
/// - Polls `rx` to receive byte chunks arriving from the serial port.
/// - Sends encoded frames to `tx` for transmission.
/// - Calls `shutdown()` on clean stop; tasks exit within one poll cycle.
pub struct SerialHandle {
    /// Inbound: byte chunks read from the serial port.
    pub rx: mpsc::Receiver<Bytes>,
    /// Outbound: byte chunks to write to the serial port.
    pub tx: mpsc::Sender<Bytes>,
    pub(crate) shutdown_tx: watch::Sender<bool>,
}

impl SerialHandle {
    /// Signal both tasks to exit.  Does not wait for completion; the caller
    /// should `await` the `JoinHandle`s returned from [`open`] if it needs
    /// a clean drain.
    pub fn shutdown(&self) {
        let _ = self.shutdown_tx.send(true);
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// open — entry point
// ─────────────────────────────────────────────────────────────────────────────

/// Open the serial port and spawn reader + writer tasks.
///
/// Returns `(SerialHandle, reader_join, writer_join)`.
///
/// Tasks are cheap background loops; they exit when either:
/// - `handle.shutdown()` is called, or
/// - their mpsc channel closes (handle dropped), or
/// - the serial port returns an unrecoverable I/O error.
pub fn open(
    config: &SerialConfig,
) -> Result<(SerialHandle, JoinHandle<()>, JoinHandle<()>), SerialIoError> {
    let port = config.open_port()?;

    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    let (rx_tx, rx_rx) = mpsc::channel::<Bytes>(RX_CHAN_BOUND);
    let (tx_tx, tx_rx) = mpsc::channel::<Bytes>(TX_CHAN_BOUND);

    let (read_half, write_half) = tokio::io::split(port);

    let reader = tokio::spawn(reader_task(read_half, rx_tx,  shutdown_rx.clone()));
    let writer = tokio::spawn(writer_task(write_half, tx_rx, shutdown_rx));

    let handle = SerialHandle {
        rx: rx_rx,
        tx: tx_tx,
        shutdown_tx,
    };

    Ok((handle, reader, writer))
}

// ─────────────────────────────────────────────────────────────────────────────
// Tasks
// ─────────────────────────────────────────────────────────────────────────────

async fn reader_task(
    mut port: tokio::io::ReadHalf<tokio_serial::SerialStream>,
    tx:       mpsc::Sender<Bytes>,
    mut shutdown: watch::Receiver<bool>,
) {
    let mut buf = vec![0u8; READ_BUF];
    loop {
        tokio::select! {
            biased;

            // Shutdown takes priority over a pending read.
            _ = shutdown.changed() => {
                if *shutdown.borrow() { break; }
            }

            result = port.read(&mut buf) => {
                match result {
                    Ok(0) => break, // EOF / port closed
                    Ok(n) => {
                        let chunk = Bytes::copy_from_slice(&buf[..n]);
                        if tx.send(chunk).await.is_err() {
                            // Router dropped rx — stop reading.
                            break;
                        }
                    }
                    Err(_) => break, // I/O error — let router detect via channel close
                }
            }
        }
    }
}

async fn writer_task(
    mut port: tokio::io::WriteHalf<tokio_serial::SerialStream>,
    mut rx:   mpsc::Receiver<Bytes>,
    mut shutdown: watch::Receiver<bool>,
) {
    loop {
        tokio::select! {
            biased;

            _ = shutdown.changed() => {
                if *shutdown.borrow() { break; }
            }

            msg = rx.recv() => {
                match msg {
                    None => break, // router dropped tx — nothing more to send
                    Some(data) => {
                        if port.write_all(&data).await.is_err() {
                            break;
                        }
                    }
                }
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Tests
// ─────────────────────────────────────────────────────────────────────────────
//
// No real serial hardware in CI.  Tests cover:
//   - SerialConfig construction and defaults
//   - Channel round-trip using tokio::io::duplex() as a mock serial port
//   - Shutdown signal terminates both tasks

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::AsyncWriteExt as _;

    // ── Config ────────────────────────────────────────────────────────────────

    #[test]
    fn config_new() {
        let c = SerialConfig::new("/dev/ttyUSB0", 115200);
        assert_eq!(c.path, "/dev/ttyUSB0");
        assert_eq!(c.baudrate, 115200);
    }

    #[test]
    fn config_mg24_default() {
        let c = SerialConfig::mg24_default("/dev/ttyUSB0");
        assert_eq!(c.baudrate, 115200);
    }

    #[test]
    fn config_clone() {
        let c = SerialConfig::mg24_default("/dev/ttyUSB0");
        let c2 = c.clone();
        assert_eq!(c.path, c2.path);
        assert_eq!(c.baudrate, c2.baudrate);
    }

    // ── Channel behaviour using duplex mock ───────────────────────────────────
    //
    // Instead of opening a real serial port we wire the reader/writer tasks
    // directly to tokio::io::duplex(), which gives us an in-memory
    // AsyncRead+AsyncWrite pair with the same interface as SerialStream.

    async fn mock_handle() -> (SerialHandle, JoinHandle<()>, JoinHandle<()>,
                               tokio::io::DuplexStream) {
        // duplex(cap) → (server_end, client_end)
        // We give the mock's server end to our tasks; tests write to client_end.
        let (server, client) = tokio::io::duplex(4096);

        let (shutdown_tx, shutdown_rx) = watch::channel(false);
        let (rx_tx, rx_rx) = mpsc::channel::<Bytes>(RX_CHAN_BOUND);
        let (tx_tx, tx_rx) = mpsc::channel::<Bytes>(TX_CHAN_BOUND);

        let (read_half, write_half) = tokio::io::split(server);
        let reader = tokio::spawn(reader_task(read_half, rx_tx,  shutdown_rx.clone()));
        let writer = tokio::spawn(writer_task(write_half, tx_rx, shutdown_rx));

        let handle = SerialHandle { rx: rx_rx, tx: tx_tx, shutdown_tx };
        (handle, reader, writer, client)
    }

    #[tokio::test]
    async fn reader_task_forwards_bytes() {
        let (mut handle, reader, writer, mut mock) = mock_handle().await;

        // Write bytes into the mock (simulates RCP sending data)
        let data = b"hello CPC";
        mock.write_all(data).await.unwrap();

        // Reader task should forward them to rx
        let chunk = handle.rx.recv().await.unwrap();
        assert_eq!(chunk.as_ref(), data);

        handle.shutdown();
        let _ = tokio::join!(reader, writer);
    }

    #[tokio::test]
    async fn writer_task_sends_bytes() {
        let (handle, reader, writer, mut mock) = mock_handle().await;

        // Push bytes through the writer task
        let data = Bytes::from_static(b"UA frame bytes");
        handle.tx.send(data.clone()).await.unwrap();

        // Read them back from the mock
        let mut buf = vec![0u8; 64];
        let n = mock.read(&mut buf).await.unwrap();
        assert_eq!(&buf[..n], data.as_ref());

        handle.shutdown();
        let _ = tokio::join!(reader, writer);
    }

    #[tokio::test]
    async fn chunked_read_reassembly() {
        let (mut handle, reader, writer, mut mock) = mock_handle().await;

        // Send two separate writes — reader task may deliver them as
        // separate chunks or merged depending on OS scheduling.
        // Either way, concatenated content must match.
        mock.write_all(b"part1").await.unwrap();
        mock.write_all(b"part2").await.unwrap();

        let mut received = Vec::new();
        // Collect until we have at least 10 bytes
        while received.len() < 10 {
            if let Some(chunk) = handle.rx.recv().await {
                received.extend_from_slice(&chunk);
            }
        }
        assert_eq!(&received[..10], b"part1part2");

        handle.shutdown();
        let _ = tokio::join!(reader, writer);
    }

    #[tokio::test]
    async fn shutdown_terminates_reader_task() {
        let (handle, reader, _writer, _mock) = mock_handle().await;
        handle.shutdown();
        // reader task must exit promptly after shutdown signal
        tokio::time::timeout(std::time::Duration::from_secs(1), reader)
            .await
            .expect("reader task did not exit within 1s")
            .unwrap();
    }

    #[tokio::test]
    async fn shutdown_terminates_writer_task() {
        let (handle, _reader, writer, _mock) = mock_handle().await;
        handle.shutdown();
        tokio::time::timeout(std::time::Duration::from_secs(1), writer)
            .await
            .expect("writer task did not exit within 1s")
            .unwrap();
    }

    #[tokio::test]
    async fn dropping_rx_stops_reader_task() {
        let (handle, reader, writer, mut mock) = mock_handle().await;

        // Drop the rx end — reader task should notice on next send attempt
        drop(handle.rx);

        // Tickle the reader with a byte so it tries to send
        mock.write_all(b"x").await.unwrap();

        tokio::time::timeout(std::time::Duration::from_secs(1), reader)
            .await
            .expect("reader task did not exit within 1s")
            .unwrap();

        handle.shutdown();
        let _ = writer.await;
    }

    #[tokio::test]
    async fn dropping_tx_stops_writer_task() {
        let (handle, reader, writer, _mock) = mock_handle().await;

        // Drop the tx sender — writer task sees channel close on next recv
        drop(handle.tx);

        tokio::time::timeout(std::time::Duration::from_secs(1), writer)
            .await
            .expect("writer task did not exit within 1s")
            .unwrap();

        handle.shutdown();
        let _ = reader.await;
    }

    #[tokio::test]
    async fn multiple_frames_ordered() {
        let (mut handle, reader, writer, mut mock) = mock_handle().await;

        // Send 5 distinct chunks; they must arrive in order
        for i in 0u8..5 {
            mock.write_all(&[i; 8]).await.unwrap();
        }

        let mut collected: Vec<Vec<u8>> = Vec::new();
        while collected.len() < 5 {
            let chunk = tokio::time::timeout(
                std::time::Duration::from_millis(200),
                handle.rx.recv(),
            )
            .await
            .expect("timeout waiting for chunk")
            .unwrap();
            collected.push(chunk.to_vec());
        }

        // All bytes for value i should equal i
        let flat: Vec<u8> = collected.into_iter().flatten().collect();
        assert_eq!(flat.len(), 40);
        // bytes 0..8 = 0, bytes 8..16 = 1, etc.
        for i in 0u8..5 {
            for j in 0..8usize {
                assert_eq!(flat[i as usize * 8 + j], i,
                    "ordering violation at byte {}", i as usize * 8 + j);
            }
        }

        handle.shutdown();
        let _ = tokio::join!(reader, writer);
    }
}