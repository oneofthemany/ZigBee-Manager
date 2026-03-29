/// CPC/HDLC framing layer for zmm_cpc.
///
/// CPC frame layout (confirmed against Sonoff MG24 MultiPAN RCP firmware
/// via on-wire capture and brute-force CRC analysis):
///
///  ┌────────┬───────┬────────┬────────┬──────┬────────────┬─────────────┬──────────────┐
///  │ Flag   │ EP_ID │ Len_lo │ Len_hi │ Ctrl │  HCS (2)   │ Payload (N) │   FCS (2)    │
///  │ 0x14   │  (1)  │   (1)  │   (1)  │  (1) │ CRC16 hdr  │             │ CRC16 payload│
///  └────────┴───────┴────────┴────────┴──────┴────────────┴─────────────┴──────────────┘
///    byte 0   byte 1  byte 2   byte 3   byte 4  bytes 5-6    bytes 7..    last 2 bytes
///
/// **LEN** is the total number of bytes following the header+HCS, i.e.
/// `payload_bytes + FCS_LEN`.  So `payload_bytes = LEN - 2`.
///
/// **Field order**: LEN comes *before* CTRL.  This was confirmed by
/// brute-force CRC matching against captured frames from the Sonoff MG24.
///
/// HCS = CRC-16/CCITT( bytes[0..5] )   — covers Flag+EP_ID+Len+Ctrl
/// FCS = CRC-16/CCITT( payload )        — covers bytes[7..7+payload_len]
///
/// CRC-16/CCITT: poly=0x1021, **init=0x0000**, no reflection.
/// Note: Silicon Labs documentation and cpcd source reference init=0xFFFF
/// (IBM-3740 variant), but the actual Sonoff MG24 MultiPAN firmware uses
/// init=0x0000.  Confirmed by brute-force matching HCS/FCS on captured
/// wire frames.
///
/// Ctrl byte frame-type encoding:
///   bit 0   == 0              → I-frame  (information, carries payload)
///   bits[1:0] == 0b01 (0x01)  → S-frame  (supervisory: RR / REJ)
///   bits[1:0] == 0b11 (0x03)  → U-frame  (unnumbered: SABM=0xEF, UA=0x63, DISC=0x43)

use bytes::{Buf, BytesMut};
use thiserror::Error;

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

pub const CPC_FLAG: u8 = 0x14;
pub const HEADER_LEN: usize = 7; // Flag + EP_ID + Len_lo + Len_hi + Ctrl + HCS(2)
pub const FCS_LEN: usize = 2;
pub const MAX_PAYLOAD_LEN: usize = 4096; // sanity cap; firmware max is ~2048

// U-frame control byte values (with P/F bit cleared)
pub const U_SABM: u8 = 0xEF;
pub const U_UA: u8   = 0x63;
pub const U_DISC: u8 = 0x43;

// ─────────────────────────────────────────────────────────────────────────────
// Error type
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Error)]
pub enum HdlcError {
    #[error("HCS mismatch: expected {expected:#06x}, got {actual:#06x}")]
    HcsMismatch { expected: u16, actual: u16 },

    #[error("FCS mismatch: expected {expected:#06x}, got {actual:#06x}")]
    FcsMismatch { expected: u16, actual: u16 },

    #[error("Payload length {0} exceeds maximum {MAX_PAYLOAD_LEN}")]
    PayloadTooLarge(usize),

    #[error("Frame flag byte is {0:#04x}, expected {CPC_FLAG:#04x}")]
    BadFlag(u8),
}

// ─────────────────────────────────────────────────────────────────────────────
// CRC-16/CCITT
// poly=0x1021, init=0x0000, no input/output reflection
// ─────────────────────────────────────────────────────────────────────────────

/// Compute CRC-16/CCITT over `data`.
///
/// Init value is **0x0000** — confirmed by on-wire analysis against the
/// Sonoff MG24 MultiPAN RCP firmware.
///
/// Table-driven for throughput; no unsafe.
pub fn crc16(data: &[u8]) -> u16 {
    // Pre-computed table for poly 0x1021
    const TABLE: [u16; 256] = make_crc_table();
    let mut crc: u16 = 0x0000;
    for &byte in data {
        let idx = ((crc >> 8) as u8 ^ byte) as usize;
        crc = (crc << 8) ^ TABLE[idx];
    }
    crc
}

const fn make_crc_table() -> [u16; 256] {
    let mut table = [0u16; 256];
    let mut i = 0usize;
    while i < 256 {
        let mut crc = (i as u16) << 8;
        let mut j = 0;
        while j < 8 {
            if crc & 0x8000 != 0 {
                crc = (crc << 1) ^ 0x1021;
            } else {
                crc <<= 1;
            }
            j += 1;
        }
        table[i] = crc;
        i += 1;
    }
    table
}

// ─────────────────────────────────────────────────────────────────────────────
// Frame types
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FrameType {
    /// Information frame — carries SPINEL payload.
    /// seq_num = send-sequence N(S), nr = receive-ready N(R)
    IFrame { seq_num: u8, nr: u8 },
    /// Supervisory frame — RR (0x00) or REJ (0x01)
    SFrame { stype: u8, nr: u8 },
    /// Unnumbered frame — SABM / UA / DISC
    UFrame { utype: u8 },
}

impl FrameType {
    /// HDLC frame-type decode per the SiLabs CPC control byte encoding:
    ///   bit 0 == 0                → I-frame  (information)
    ///   bits[1:0] == 0b01 (0x01)  → S-frame  (supervisory: RR / REJ)
    ///   bits[1:0] == 0b11 (0x03)  → U-frame  (unnumbered: SABM / UA / DISC)
    pub fn decode(ctrl: u8) -> Self {
        if ctrl & 0x01 == 0 {
            FrameType::IFrame {
                seq_num: (ctrl >> 1) & 0x07,
                nr:      (ctrl >> 5) & 0x07,
            }
        } else if ctrl & 0x03 == 0x01 {
            FrameType::SFrame {
                stype: (ctrl >> 2) & 0x03,
                nr:    (ctrl >> 5) & 0x07,
            }
        } else {
            // ctrl & 0x03 == 0x03
            FrameType::UFrame {
                utype: ctrl & 0xEF, // mask P/F bit (bit 4)
            }
        }
    }

    pub fn is_iframe(&self) -> bool { matches!(self, FrameType::IFrame { .. }) }
    pub fn is_uframe(&self) -> bool { matches!(self, FrameType::UFrame { .. }) }
    pub fn is_sabm(&self)   -> bool { *self == FrameType::UFrame { utype: U_SABM } }
    pub fn is_ua(&self)     -> bool { *self == FrameType::UFrame { utype: U_UA   } }
    pub fn is_disc(&self)   -> bool { *self == FrameType::UFrame { utype: U_DISC } }
}

/// A fully validated CPC frame.
#[derive(Debug, Clone)]
pub struct RawFrame {
    pub ep_id:      u8,
    pub ctrl:       u8,
    pub frame_type: FrameType,
    pub payload:    Vec<u8>,
}

impl RawFrame {
    /// Build a U-frame for transmission (SABM / UA / DISC, empty payload).
    pub fn u_frame(ep_id: u8, utype: u8) -> Self {
        RawFrame {
            ep_id,
            ctrl: utype,
            frame_type: FrameType::UFrame { utype },
            payload: Vec::new(),
        }
    }

    /// Encode this frame to wire bytes.
    ///
    /// Wire layout: FLAG EP LEN_LO LEN_HI CTRL HCS(2) PAYLOAD FCS(2)
    ///
    /// LEN = payload.len() + FCS_LEN (the length field includes the
    /// trailing FCS bytes).
    pub fn encode(&self) -> Vec<u8> {
        // LEN includes FCS — the RCP expects this
        let len = (self.payload.len() + FCS_LEN) as u16;
        let hdr: [u8; 5] = [
            CPC_FLAG,
            self.ep_id,
            (len & 0xFF) as u8,
            (len >> 8)   as u8,
            self.ctrl,
        ];
        let hcs = crc16(&hdr);
        let fcs = crc16(&self.payload);

        let mut out = Vec::with_capacity(HEADER_LEN + self.payload.len() + FCS_LEN);
        out.extend_from_slice(&hdr);
        out.push((hcs & 0xFF) as u8);
        out.push((hcs >> 8)   as u8);
        out.extend_from_slice(&self.payload);
        out.push((fcs & 0xFF) as u8);
        out.push((fcs >> 8)   as u8);
        out
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Streaming framer
// ─────────────────────────────────────────────────────────────────────────────

/// Accumulates raw serial bytes and yields validated [`RawFrame`]s.
///
/// Call [`Framer::push`] with each chunk received from the serial reader.
/// Call [`Framer::next_frame`] to drain one complete frame at a time.
///
/// Resynchronisation: on any header error (bad flag, HCS mismatch, oversized
/// payload) the framer discards the offending byte and continues scanning,
/// preserving partial valid frames already buffered behind it.
pub struct Framer {
    buf: BytesMut,
}

impl Framer {
    pub fn new() -> Self {
        Framer { buf: BytesMut::with_capacity(512) }
    }

    /// Feed raw bytes from the serial read loop.
    pub fn push(&mut self, data: &[u8]) {
        self.buf.extend_from_slice(data);
    }

    /// Try to extract one complete, validated frame from the internal buffer.
    ///
    /// Returns:
    /// - `Ok(Some(frame))` — a valid frame was decoded and consumed.
    /// - `Ok(None)`        — not enough data yet; call `push` and retry.
    /// - `Err(e)`          — frame was malformed; offending byte discarded,
    ///                       call again to continue scanning.
    pub fn next_frame(&mut self) -> Result<Option<RawFrame>, HdlcError> {
        // Advance to next 0x14 flag byte
        self.sync_to_flag();

        // Need at least a full header
        if self.buf.len() < HEADER_LEN {
            return Ok(None);
        }

        // Peek at header without consuming
        let flag = self.buf[0];
        if flag != CPC_FLAG {
            // sync_to_flag should have handled this, but guard anyway
            self.buf.advance(1);
            return Err(HdlcError::BadFlag(flag));
        }

        // Wire layout: FLAG(0) EP(1) LEN_LO(2) LEN_HI(3) CTRL(4) HCS(5-6)
        let ep_id = self.buf[1];
        let len   = u16::from_le_bytes([self.buf[2], self.buf[3]]) as usize;
        let ctrl  = self.buf[4];

        // LEN includes FCS — payload_len = len - FCS_LEN
        // For U-frames (SABM/UA/DISC) len == FCS_LEN (payload is empty)
        if len < FCS_LEN {
            // A length smaller than FCS_LEN is invalid
            self.buf.advance(1);
            return Err(HdlcError::BadFlag(flag)); // reuse error to resync
        }

        let payload_len = len - FCS_LEN;

        if payload_len > MAX_PAYLOAD_LEN {
            self.buf.advance(1); // discard flag byte, resync
            return Err(HdlcError::PayloadTooLarge(payload_len));
        }

        // Validate HCS
        let expected_hcs = crc16(&self.buf[0..5]);
        let actual_hcs   = u16::from_le_bytes([self.buf[5], self.buf[6]]);
        if expected_hcs != actual_hcs {
            self.buf.advance(1);
            return Err(HdlcError::HcsMismatch {
                expected: expected_hcs,
                actual:   actual_hcs,
            });
        }

        // Wait for full payload + FCS
        let total = HEADER_LEN + payload_len + FCS_LEN;
        if self.buf.len() < total {
            return Ok(None);
        }

        // Validate FCS
        let payload_start = HEADER_LEN;
        let payload_end   = HEADER_LEN + payload_len;
        let payload       = self.buf[payload_start..payload_end].to_vec();
        let expected_fcs  = crc16(&payload);
        let actual_fcs    = u16::from_le_bytes([
            self.buf[payload_end],
            self.buf[payload_end + 1],
        ]);
        if expected_fcs != actual_fcs {
            self.buf.advance(1);
            return Err(HdlcError::FcsMismatch {
                expected: expected_fcs,
                actual:   actual_fcs,
            });
        }

        // Consume the complete frame
        self.buf.advance(total);

        Ok(Some(RawFrame {
            ep_id,
            ctrl,
            frame_type: FrameType::decode(ctrl),
            payload,
        }))
    }

    /// Scan forward until `buf[0] == CPC_FLAG` or buffer is empty.
    fn sync_to_flag(&mut self) {
        while !self.buf.is_empty() && self.buf[0] != CPC_FLAG {
            self.buf.advance(1);
        }
    }
}

impl Default for Framer {
    fn default() -> Self { Self::new() }
}

// ─────────────────────────────────────────────────────────────────────────────
// Tests
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // Helper: build a wire frame from components, computing CRCs correctly.
    // Uses the confirmed wire layout: FLAG EP LEN_LO LEN_HI CTRL HCS(2) PAYLOAD FCS(2)
    // LEN = payload.len() + FCS_LEN
    fn make_frame(ep_id: u8, ctrl: u8, payload: &[u8]) -> Vec<u8> {
        let len = (payload.len() + FCS_LEN) as u16;
        let hdr: [u8; 5] = [
            CPC_FLAG,
            ep_id,
            (len & 0xFF) as u8,
            (len >> 8)   as u8,
            ctrl,
        ];
        let hcs = crc16(&hdr);
        let fcs = crc16(payload);
        let mut v = Vec::new();
        v.extend_from_slice(&hdr);
        v.push((hcs & 0xFF) as u8);
        v.push((hcs >> 8)   as u8);
        v.extend_from_slice(payload);
        v.push((fcs & 0xFF) as u8);
        v.push((fcs >> 8)   as u8);
        v
    }

    // ── CRC ───────────────────────────────────────────────────────────────────

    #[test]
    fn crc_empty() {
        // CRC of empty slice with init 0x0000 is 0x0000
        assert_eq!(crc16(b""), 0x0000);
    }

    #[test]
    fn crc_known_vector() {
        // CRC-16/XMODEM ("123456789") with init=0x0000, poly=0x1021 = 0x31C3
        assert_eq!(crc16(b"123456789"), 0x31C3);
    }

    #[test]
    fn crc_matches_captured_frame() {
        // Verified against captured MG24 frame:
        // Header: 14 00 0E 00 C0 → HCS should be 0x4F11
        let hdr: [u8; 5] = [0x14, 0x00, 0x0E, 0x00, 0xC0];
        assert_eq!(crc16(&hdr), 0x4F11);

        // Payload: 06 00 08 00 00 00 00 00 77 00 00 00 → FCS should be 0x522D
        let payload: [u8; 12] = [0x06, 0x00, 0x08, 0x00, 0x00, 0x00,
                                  0x00, 0x00, 0x77, 0x00, 0x00, 0x00];
        assert_eq!(crc16(&payload), 0x522D);
    }

    // ── Encode / decode round-trip ────────────────────────────────────────────

    #[test]
    fn roundtrip_iframe_empty_payload() {
        let frame = RawFrame {
            ep_id: 12,
            ctrl:  0x00, // I-frame, N(S)=0, N(R)=0
            frame_type: FrameType::IFrame { seq_num: 0, nr: 0 },
            payload: vec![],
        };
        let wire = frame.encode();
        // HEADER_LEN(7) + 0 payload + FCS_LEN(2) = 9
        assert_eq!(wire.len(), HEADER_LEN + FCS_LEN);
        assert_eq!(wire[0], CPC_FLAG);
        assert_eq!(wire[1], 12); // ep_id
        // LEN = 0 + FCS_LEN = 2
        assert_eq!(wire[2], 0x02); // len_lo
        assert_eq!(wire[3], 0x00); // len_hi
        assert_eq!(wire[4], 0x00); // ctrl
    }

    #[test]
    fn roundtrip_uframe_sabm() {
        let frame = RawFrame::u_frame(0, U_SABM);
        let wire  = frame.encode();
        let mut framer = Framer::new();
        framer.push(&wire);
        let decoded = framer.next_frame().unwrap().unwrap();
        assert!(decoded.frame_type.is_sabm());
        assert_eq!(decoded.ep_id, 0);
    }

    #[test]
    fn roundtrip_uframe_ua() {
        let frame = RawFrame::u_frame(12, U_UA);
        let wire  = frame.encode();
        let mut framer = Framer::new();
        framer.push(&wire);
        let decoded = framer.next_frame().unwrap().unwrap();
        assert!(decoded.frame_type.is_ua());
        assert_eq!(decoded.ep_id, 12);
    }

    #[test]
    fn roundtrip_uframe_disc() {
        let frame = RawFrame::u_frame(13, U_DISC);
        let wire  = frame.encode();
        let mut framer = Framer::new();
        framer.push(&wire);
        let decoded = framer.next_frame().unwrap().unwrap();
        assert!(decoded.frame_type.is_disc());
        assert_eq!(decoded.ep_id, 13);
    }

    #[test]
    fn roundtrip_with_payload() {
        let payload = vec![0xAB, 0xCD, 0x01, 0x02, 0x03];
        let wire    = make_frame(12, 0x00, &payload);
        let mut framer = Framer::new();
        framer.push(&wire);
        let frame = framer.next_frame().unwrap().unwrap();
        assert_eq!(frame.ep_id, 12);
        assert_eq!(frame.payload, payload);
    }

    #[test]
    fn decode_captured_mg24_frame() {
        // Real captured frame from Sonoff MG24 MultiPAN RCP:
        // 14 00 0E 00 C0 11 4F 06 00 08 00 00 00 00 00 77 00 00 00 2D 52
        let wire = vec![
            0x14, 0x00, 0x0E, 0x00, 0xC0, 0x11, 0x4F,
            0x06, 0x00, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x77, 0x00, 0x00, 0x00,
            0x2D, 0x52,
        ];
        let mut framer = Framer::new();
        framer.push(&wire);
        let frame = framer.next_frame().unwrap().unwrap();
        assert_eq!(frame.ep_id, 0);
        // ctrl = 0xC0: I-frame N(S)=0 N(R)=6
        assert_eq!(frame.ctrl, 0xC0);
        assert!(matches!(
            frame.frame_type,
            FrameType::IFrame { seq_num: 0, nr: 6 }
        ));
        assert_eq!(frame.payload.len(), 12);
        assert_eq!(
            frame.payload,
            vec![0x06, 0x00, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x77, 0x00, 0x00, 0x00]
        );
    }

    // ── Framer: chunked delivery ──────────────────────────────────────────────

    #[test]
    fn framer_chunked_delivery() {
        let wire = make_frame(12, 0x00, b"hello");
        let mut framer = Framer::new();
        // Feed one byte at a time
        for byte in &wire[..wire.len() - 1] {
            framer.push(std::slice::from_ref(byte));
            assert!(framer.next_frame().unwrap().is_none());
        }
        framer.push(&wire[wire.len() - 1..]);
        let frame = framer.next_frame().unwrap().unwrap();
        assert_eq!(frame.payload, b"hello");
    }

    #[test]
    fn framer_two_frames_back_to_back() {
        let w1 = make_frame(12, 0x00, b"frame1");
        let w2 = make_frame(12, 0x02, b"frame2");
        let mut combined = w1.clone();
        combined.extend_from_slice(&w2);

        let mut framer = Framer::new();
        framer.push(&combined);
        let f1 = framer.next_frame().unwrap().unwrap();
        let f2 = framer.next_frame().unwrap().unwrap();
        assert_eq!(f1.payload, b"frame1");
        assert_eq!(f2.payload, b"frame2");
    }

    // ── Error recovery ────────────────────────────────────────────────────────

    #[test]
    fn framer_recovers_from_bad_flag() {
        let garbage = vec![0xFF, 0x00, 0x00];
        let good    = make_frame(12, 0x00, b"ok");
        let mut data = garbage.clone();
        data.extend_from_slice(&good);

        let mut framer = Framer::new();
        framer.push(&data);
        // Should skip garbage and yield the good frame
        let frame = framer.next_frame().unwrap().unwrap();
        assert_eq!(frame.payload, b"ok");
    }

    #[test]
    fn framer_bad_hcs_discards_one_byte_and_resyncs() {
        let mut wire = make_frame(12, 0x00, b"data");
        wire[5] ^= 0xFF; // corrupt HCS byte 0
        let good = make_frame(12, 0x00, b"good");
        let mut data = wire.clone();
        data.extend_from_slice(&good);

        let mut framer = Framer::new();
        framer.push(&data);
        let err = framer.next_frame();
        assert!(matches!(err, Err(HdlcError::HcsMismatch { .. })));
        // Continue — should eventually yield the good frame
        let frame = framer.next_frame().unwrap().unwrap();
        assert_eq!(frame.payload, b"good");
    }

    #[test]
    fn framer_bad_fcs_discards_and_resyncs() {
        let mut wire = make_frame(12, 0x00, b"bad_fcs");
        let last = wire.len() - 1;
        wire[last] ^= 0xFF; // corrupt FCS
        let good = make_frame(12, 0x00, b"still_ok");
        let mut data = wire.clone();
        data.extend_from_slice(&good);

        let mut framer = Framer::new();
        framer.push(&data);
        let err = framer.next_frame();
        assert!(matches!(err, Err(HdlcError::FcsMismatch { .. })));
        let frame = framer.next_frame().unwrap().unwrap();
        assert_eq!(frame.payload, b"still_ok");
    }

    #[test]
    fn framer_oversized_payload_discards_and_resyncs() {
        // Craft a header claiming 8000 bytes (> MAX_PAYLOAD_LEN)
        // LEN includes FCS, so declared LEN = 8000 + 2 = 8002
        let len: u16 = 8002;
        let hdr: [u8; 5] = [CPC_FLAG, 12, (len & 0xFF) as u8, (len >> 8) as u8, 0x00];
        let hcs = crc16(&hdr);
        let mut wire = hdr.to_vec();
        wire.push((hcs & 0xFF) as u8);
        wire.push((hcs >> 8)   as u8);
        let good = make_frame(12, 0x00, b"recovered");
        wire.extend_from_slice(&good);

        let mut framer = Framer::new();
        framer.push(&wire);
        let err = framer.next_frame();
        assert!(matches!(err, Err(HdlcError::PayloadTooLarge(_))));
        let frame = framer.next_frame().unwrap().unwrap();
        assert_eq!(frame.payload, b"recovered");
    }

    // ── FrameType decode ──────────────────────────────────────────────────────

    #[test]
    fn frame_type_decode_iframe() {
        // ctrl = 0b00000000: I-frame, N(S)=0, N(R)=0
        assert!(matches!(FrameType::decode(0x00), FrameType::IFrame { seq_num: 0, nr: 0 }));
    }

    #[test]
    fn frame_type_decode_sframe() {
        // ctrl = 0b00000001: S-frame, stype=0 (RR), N(R)=0
        assert!(matches!(FrameType::decode(0x01), FrameType::SFrame { stype: 0, nr: 0 }));
    }

    #[test]
    fn frame_type_decode_uframe_sabm() {
        let ft = FrameType::decode(U_SABM);
        assert!(ft.is_sabm(), "{ft:?}");
    }

    #[test]
    fn frame_type_decode_uframe_ua() {
        let ft = FrameType::decode(U_UA);
        assert!(ft.is_ua(), "{ft:?}");
    }

    #[test]
    fn frame_type_decode_uframe_disc() {
        let ft = FrameType::decode(U_DISC);
        assert!(ft.is_disc(), "{ft:?}");
    }
}