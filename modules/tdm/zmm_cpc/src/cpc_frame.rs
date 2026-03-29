/// CPC semantic frame layer.
///
/// Converts raw HDLC [`RawFrame`]s into typed [`CpcFrame`]s and provides
/// encoding helpers for TX.  This module is **stateless** — sequence
/// counters and window tracking live in `endpoint.rs`.
///
/// # Handshake sequence (confirmed Phase 1, MG24 MultiPAN RCP v4.6.0)
///
/// ```text
///  RCP                          zmm_cpc (us)
///   │── SABM  (ep0)  ──────────────▶│   RCP announces it is alive
///   │◀── UA   (ep0)  ───────────────│   we accept system connection
///   │                               │
///   │── SABM  (ep12) ──────────────▶│   RCP opens Zigbee SPINEL endpoint
///   │◀── UA   (ep12) ───────────────│   we accept → endpoint OPEN
///   │                               │
///   │── SABM  (ep13) ──────────────▶│   RCP opens OT SPINEL endpoint
///   │◀── UA   (ep13) ───────────────│   we accept → endpoint OPEN
///   │                               │
///   │── I-frame (ep12, SPINEL) ────▶│   Zigbee traffic begins
///   │◀── RR (ep12, N(R)=1) ─────────│   we acknowledge
///   │    …
/// ```
///
/// # Endpoint IDs
///
/// | EP  | Purpose             | Handled by         |
/// |-----|---------------------|--------------------|
/// |  0  | System / handshake  | router (internal)  |
/// | 12  | Zigbee SPINEL       | TCP :9999 → bellows|
/// | 13  | OT SPINEL           | TCP :9998 → OT     |
///
/// # I-frame ctrl encoding (3-bit sequence, basic mode)
///
/// ```text
///  Bit:  7  6  5 | 4 |  3  2  1  | 0
///        N(R)    | P |  N(S)     | 0
/// ```
///
/// # S-frame ctrl encoding
///
/// ```text
///  Bit:  7  6  5 | 4 |  3  2 |  1  0
///        N(R)    | P |  type |  0  1
///  type: 0x00 = RR (receive ready), 0x01 = REJ (reject)
/// ```

use crate::hdlc::{FrameType, RawFrame, U_DISC, U_SABM, U_UA};

// ─────────────────────────────────────────────────────────────────────────────
// Endpoint IDs
// ─────────────────────────────────────────────────────────────────────────────

pub const EP_SYSTEM:  u8 = 0;
pub const EP_ZIGBEE:  u8 = 12;
pub const EP_OPENTHREAD: u8 = 13;

// ─────────────────────────────────────────────────────────────────────────────
// Semantic frame type
// ─────────────────────────────────────────────────────────────────────────────

/// High-level CPC frame — output of [`parse`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CpcFrame {
    /// RCP requests connection on `ep_id` (U-SABM received).
    /// We must respond with [`encode_ua`].
    Connect { ep_id: u8 },

    /// RCP confirms our connection (U-UA received — rare in RCP-initiates model).
    Accepted { ep_id: u8 },

    /// RCP is disconnecting `ep_id` (U-DISC received).
    Disconnect { ep_id: u8 },

    /// SPINEL payload from RCP on `ep_id` (I-frame).
    /// `seq_num` = N(S) from RCP, `nr` = N(R) — the seq the RCP expects next from us.
    Data {
        ep_id:   u8,
        seq_num: u8,
        nr:      u8,
        payload: Vec<u8>,
    },

    /// RCP acknowledges our I-frames up to `nr - 1` (S-frame RR).
    ReceiveReady { ep_id: u8, nr: u8 },

    /// RCP requests retransmit from `nr` (S-frame REJ).
    Reject { ep_id: u8, nr: u8 },
}

impl CpcFrame {
    /// Returns the endpoint ID this frame belongs to.
    pub fn ep_id(&self) -> u8 {
        match self {
            CpcFrame::Connect      { ep_id, .. } => *ep_id,
            CpcFrame::Accepted     { ep_id }     => *ep_id,
            CpcFrame::Disconnect   { ep_id }     => *ep_id,
            CpcFrame::Data         { ep_id, .. } => *ep_id,
            CpcFrame::ReceiveReady { ep_id, .. } => *ep_id,
            CpcFrame::Reject       { ep_id, .. } => *ep_id,
        }
    }

    /// True if this frame carries SPINEL payload destined for a TCP listener.
    pub fn is_data(&self) -> bool { matches!(self, CpcFrame::Data { .. }) }

    /// True if this is an ep0 system frame handled internally by the router.
    pub fn is_system(&self) -> bool { self.ep_id() == EP_SYSTEM }
}

// ─────────────────────────────────────────────────────────────────────────────
// Parse: RawFrame → CpcFrame
// ─────────────────────────────────────────────────────────────────────────────

/// Convert a validated [`RawFrame`] into a [`CpcFrame`].
///
/// Called by the router after `Framer::next_frame` succeeds.
pub fn parse(raw: RawFrame) -> CpcFrame {
    match raw.frame_type {
        FrameType::UFrame { utype } if utype == U_SABM => {
            CpcFrame::Connect { ep_id: raw.ep_id }
        }
        FrameType::UFrame { utype } if utype == U_UA => {
            CpcFrame::Accepted { ep_id: raw.ep_id }
        }
        FrameType::UFrame { utype } if utype == U_DISC => {
            CpcFrame::Disconnect { ep_id: raw.ep_id }
        }
        FrameType::UFrame { .. } => {
            // Unknown U-frame subtype — treat as system noise, map to ep0 Disconnect
            // so the router can log and ignore it safely.
            CpcFrame::Disconnect { ep_id: raw.ep_id }
        }
        FrameType::IFrame { seq_num, nr } => {
            CpcFrame::Data {
                ep_id: raw.ep_id,
                seq_num,
                nr,
                payload: raw.payload,
            }
        }
        FrameType::SFrame { stype, nr } => {
            if stype == 0x00 {
                CpcFrame::ReceiveReady { ep_id: raw.ep_id, nr }
            } else {
                // stype == 0x01 or higher → REJ
                CpcFrame::Reject { ep_id: raw.ep_id, nr }
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Encode: build TX wire frames
// ─────────────────────────────────────────────────────────────────────────────

/// Encode a UA response frame for `ep_id`.
///
/// Called by the router when a `Connect` frame is received on any endpoint
/// (ep0, ep12, ep13).
pub fn encode_ua(ep_id: u8) -> Vec<u8> {
    RawFrame::u_frame(ep_id, U_UA).encode()
}

/// Encode a DISC frame for `ep_id`.
///
/// Used during clean shutdown to signal the RCP that we are closing.
pub fn encode_disc(ep_id: u8) -> Vec<u8> {
    RawFrame::u_frame(ep_id, U_DISC).encode()
}

/// Encode a SABM (Set Asynchronous Balanced Mode) frame for ep_id.
///
/// Used by the router to proactively initiate a CPC connection when the
/// RCP has not sent its own SABM within the startup grace period.
/// This mirrors cpcd's behaviour: if the RCP doesn't initiate, we do.
///
/// The RCP will respond with UA if it accepts, or ignore it if it's
/// already in application mode and waiting for us to respond to its
/// SABM (in which case the normal reactive path handles it).
pub fn encode_sabm(ep_id: u8) -> Vec<u8> {
RawFrame::u_frame(ep_id, U_SABM).encode()
}

/// Encode a Receive Ready (RR) supervisory frame.
///
/// `nr` = next sequence number we expect from the RCP (acknowledges up to N(R)-1).
/// The P/F bit is always clear in our RR frames.
///
/// S-frame ctrl = 0x01 | (stype=RR << 2) | (nr << 5)
///              = 0x01 | 0x00 | (nr << 5)
pub fn encode_rr(ep_id: u8, nr: u8) -> Vec<u8> {
    let ctrl: u8 = 0x01 | ((nr & 0x07) << 5);
    RawFrame {
        ep_id,
        ctrl,
        frame_type: FrameType::SFrame { stype: 0, nr },
        payload: Vec::new(),
    }
    .encode()
}

/// Encode an I-frame carrying a SPINEL payload toward the RCP.
///
/// `seq_num` = our N(S), `nr` = N(R) (piggybacked acknowledgement).
///
/// I-frame ctrl = (seq_num << 1) | (nr << 5)   [bit 0 = 0]
pub fn encode_i_frame(ep_id: u8, seq_num: u8, nr: u8, payload: Vec<u8>) -> Vec<u8> {
    let ctrl: u8 = ((seq_num & 0x07) << 1) | ((nr & 0x07) << 5);
    RawFrame {
        ep_id,
        ctrl,
        frame_type: FrameType::IFrame { seq_num, nr },
        payload,
    }
    .encode()
}

// ─────────────────────────────────────────────────────────────────────────────
// Tests
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hdlc::{Framer, crc16, CPC_FLAG, HEADER_LEN, FCS_LEN};

    // ── Helpers ───────────────────────────────────────────────────────────────

    /// Build a wire frame and round-trip it through Framer → parse.
    fn wire_round_trip(ep_id: u8, ctrl: u8, payload: &[u8]) -> CpcFrame {
        let len = payload.len() as u16;
        let hdr = [CPC_FLAG, ep_id, ctrl, (len & 0xFF) as u8, (len >> 8) as u8];
        let hcs = crc16(&hdr);
        let fcs = crc16(payload);
        let mut wire = hdr.to_vec();
        wire.push((hcs & 0xFF) as u8);
        wire.push((hcs >> 8)   as u8);
        wire.extend_from_slice(payload);
        wire.push((fcs & 0xFF) as u8);
        wire.push((fcs >> 8)   as u8);

        let mut framer = Framer::new();
        framer.push(&wire);
        let raw = framer.next_frame().unwrap().unwrap();
        parse(raw)
    }

    // ── parse: U-frames ───────────────────────────────────────────────────────

    #[test]
    fn parse_sabm_ep0() {
        let frame = wire_round_trip(EP_SYSTEM, U_SABM, b"");
        assert_eq!(frame, CpcFrame::Connect { ep_id: EP_SYSTEM });
    }

    #[test]
    fn parse_sabm_ep12() {
        let frame = wire_round_trip(EP_ZIGBEE, U_SABM, b"");
        assert_eq!(frame, CpcFrame::Connect { ep_id: EP_ZIGBEE });
    }

    #[test]
    fn parse_sabm_ep13() {
        let frame = wire_round_trip(EP_OPENTHREAD, U_SABM, b"");
        assert_eq!(frame, CpcFrame::Connect { ep_id: EP_OPENTHREAD });
    }

    #[test]
    fn parse_ua_ep0() {
        let frame = wire_round_trip(EP_SYSTEM, U_UA, b"");
        assert_eq!(frame, CpcFrame::Accepted { ep_id: EP_SYSTEM });
    }

    #[test]
    fn parse_disc_ep12() {
        let frame = wire_round_trip(EP_ZIGBEE, U_DISC, b"");
        assert_eq!(frame, CpcFrame::Disconnect { ep_id: EP_ZIGBEE });
    }

    // ── parse: I-frames ───────────────────────────────────────────────────────

    #[test]
    fn parse_iframe_seq0_nr0() {
        // ctrl = 0x00: N(S)=0, N(R)=0
        let payload = vec![0x01, 0x02, 0x03];
        let frame = wire_round_trip(EP_ZIGBEE, 0x00, &payload);
        assert_eq!(
            frame,
            CpcFrame::Data { ep_id: EP_ZIGBEE, seq_num: 0, nr: 0, payload }
        );
    }

    #[test]
    fn parse_iframe_seq3_nr2() {
        // ctrl = (3<<1) | (2<<5) = 0x06 | 0x40 = 0x46
        let ctrl: u8 = (3 << 1) | (2 << 5);
        let payload = vec![0xAB, 0xCD];
        let frame = wire_round_trip(EP_ZIGBEE, ctrl, &payload);
        assert_eq!(
            frame,
            CpcFrame::Data { ep_id: EP_ZIGBEE, seq_num: 3, nr: 2, payload }
        );
    }

    #[test]
    fn iframe_is_data() {
        let frame = wire_round_trip(EP_ZIGBEE, 0x00, b"spinel");
        assert!(frame.is_data());
        assert!(!frame.is_system());
    }

    // ── parse: S-frames ───────────────────────────────────────────────────────

    #[test]
    fn parse_rr_nr3() {
        // S-frame ctrl = 0x01 | (nr<<5): nr=3 → 0x01 | 0x60 = 0x61
        let ctrl: u8 = 0x01 | (3u8 << 5);
        let frame = wire_round_trip(EP_ZIGBEE, ctrl, b"");
        assert_eq!(frame, CpcFrame::ReceiveReady { ep_id: EP_ZIGBEE, nr: 3 });
    }

    #[test]
    fn parse_rej_nr1() {
        // S-frame REJ: stype=1, nr=1 → ctrl = 0x01 | (1<<2) | (1<<5) = 0x01|0x04|0x20 = 0x25
        let ctrl: u8 = 0x01 | (1u8 << 2) | (1u8 << 5);
        let frame = wire_round_trip(EP_ZIGBEE, ctrl, b"");
        assert_eq!(frame, CpcFrame::Reject { ep_id: EP_ZIGBEE, nr: 1 });
    }

    // ── encode helpers ────────────────────────────────────────────────────────

    fn decode_wire(wire: &[u8]) -> CpcFrame {
        let mut framer = Framer::new();
        framer.push(wire);
        let raw = framer.next_frame().unwrap().unwrap();
        parse(raw)
    }

    #[test]
    fn encode_ua_decodes_to_accepted() {
        let wire = encode_ua(EP_ZIGBEE);
        let frame = decode_wire(&wire);
        assert_eq!(frame, CpcFrame::Accepted { ep_id: EP_ZIGBEE });
    }

    #[test]
    fn encode_disc_decodes_to_disconnect() {
        let wire = encode_disc(EP_ZIGBEE);
        let frame = decode_wire(&wire);
        assert_eq!(frame, CpcFrame::Disconnect { ep_id: EP_ZIGBEE });
    }

    #[test]
    fn encode_sabm_decodes_to_connect() {
        let wire = encode_sabm(EP_ZIGBEE);
        let frame = decode_wire(&wire);
        assert_eq!(frame, CpcFrame::Connect { ep_id: EP_ZIGBEE });
    }

    #[test]
    fn encode_sabm_ep0() {
        let wire = encode_sabm(EP_SYSTEM);
        let frame = decode_wire(&wire);
        assert_eq!(frame, CpcFrame::Connect { ep_id: EP_SYSTEM });
    }

    #[test]
    fn encode_rr_roundtrip() {
        let wire = encode_rr(EP_ZIGBEE, 5);
        let frame = decode_wire(&wire);
        assert_eq!(frame, CpcFrame::ReceiveReady { ep_id: EP_ZIGBEE, nr: 5 });
    }

    #[test]
    fn encode_rr_nr_wraps_at_8() {
        // N(R) is 3 bits — value 8 wraps to 0
        let wire = encode_rr(EP_ZIGBEE, 8);
        let frame = decode_wire(&wire);
        assert_eq!(frame, CpcFrame::ReceiveReady { ep_id: EP_ZIGBEE, nr: 0 });
    }

    #[test]
    fn encode_i_frame_roundtrip() {
        let payload = vec![0xDE, 0xAD, 0xBE, 0xEF];
        let wire = encode_i_frame(EP_ZIGBEE, 2, 1, payload.clone());
        let frame = decode_wire(&wire);
        assert_eq!(
            frame,
            CpcFrame::Data { ep_id: EP_ZIGBEE, seq_num: 2, nr: 1, payload }
        );
    }

    #[test]
    fn encode_i_frame_seq_wraps_at_8() {
        // N(S) is 3 bits — value 8 wraps to 0
        let wire = encode_i_frame(EP_ZIGBEE, 8, 0, vec![0x01]);
        let frame = decode_wire(&wire);
        assert_eq!(
            frame,
            CpcFrame::Data { ep_id: EP_ZIGBEE, seq_num: 0, nr: 0, payload: vec![0x01] }
        );
    }

    #[test]
    fn encode_i_frame_empty_payload() {
        let wire = encode_i_frame(EP_ZIGBEE, 0, 0, vec![]);
        assert_eq!(wire.len(), HEADER_LEN + FCS_LEN);
        let frame = decode_wire(&wire);
        assert!(frame.is_data());
    }

    // ── ep_id accessors ───────────────────────────────────────────────────────

    #[test]
    fn ep_id_accessor_all_variants() {
        assert_eq!(CpcFrame::Connect      { ep_id: 12 }.ep_id(), 12);
        assert_eq!(CpcFrame::Accepted     { ep_id: 13 }.ep_id(), 13);
        assert_eq!(CpcFrame::Disconnect   { ep_id: 0  }.ep_id(), 0);
        assert_eq!(CpcFrame::ReceiveReady { ep_id: 12, nr: 0 }.ep_id(), 12);
        assert_eq!(CpcFrame::Reject       { ep_id: 13, nr: 1 }.ep_id(), 13);
        assert_eq!(
            CpcFrame::Data { ep_id: 12, seq_num: 0, nr: 0, payload: vec![] }.ep_id(),
            12
        );
    }

    #[test]
    fn is_system_flag() {
        assert!(CpcFrame::Connect { ep_id: EP_SYSTEM }.is_system());
        assert!(!CpcFrame::Connect { ep_id: EP_ZIGBEE }.is_system());
        assert!(!CpcFrame::Connect { ep_id: EP_OPENTHREAD }.is_system());
    }

    // ── full handshake sequence ───────────────────────────────────────────────

    #[test]
    fn handshake_ep0_then_ep12() {
        // Simulate: RCP sends SABM ep0 → we send UA ep0
        //           RCP sends SABM ep12 → we send UA ep12
        //           RCP sends I-frame ep12 with SPINEL data

        let sabm_ep0  = RawFrame::u_frame(EP_SYSTEM,  U_SABM).encode();
        let sabm_ep12 = RawFrame::u_frame(EP_ZIGBEE,  U_SABM).encode();
        let spinel    = encode_i_frame(EP_ZIGBEE, 0, 0, vec![0x81, 0x00, 0x02]);

        let ua_ep0  = encode_ua(EP_SYSTEM);
        let ua_ep12 = encode_ua(EP_ZIGBEE);

        // Decode each in order
        let f0  = decode_wire(&sabm_ep0);
        let f12 = decode_wire(&sabm_ep12);
        let fd  = decode_wire(&spinel);

        assert_eq!(f0,  CpcFrame::Connect { ep_id: EP_SYSTEM });
        assert_eq!(f12, CpcFrame::Connect { ep_id: EP_ZIGBEE });
        assert!(fd.is_data());

        // Our responses decode correctly
        let ua0  = decode_wire(&ua_ep0);
        let ua12 = decode_wire(&ua_ep12);
        assert_eq!(ua0,  CpcFrame::Accepted { ep_id: EP_SYSTEM });
        assert_eq!(ua12, CpcFrame::Accepted { ep_id: EP_ZIGBEE });
    }
}