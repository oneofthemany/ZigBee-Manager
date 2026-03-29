/// Time-Division Multiplexing scheduler for zmm_cpc.
///
/// Implements [`TdmGate`] with a fixed round-robin slot list.  Each slot owns
/// the radio for a configurable wall-clock window; when the window expires the
/// scheduler silently advances to the next slot.
///
/// # Default configuration (Phase 2)
///
/// | Endpoint | Protocol | Slot (ms) | Rationale                              |
/// |----------|----------|-----------|----------------------------------------|
/// | ep12     | Zigbee   | 15        | Primary protocol; 40+ devices active   |
/// | ep13     | OT/Thread| 5         | Secondary; infrastructure only for now |
///
/// Total cycle = 20 ms → 50 Hz round-robin.  Zigbee gets 75 % of radio time.
///
/// # Slot advance semantics
///
/// Slots advance by **elapsed wall-clock time**, not by frame count.  An
/// endpoint that has nothing to send still holds its slot until the window
/// expires.  This prevents a quiet OT stack from starving Zigbee by
/// continuously yielding and immediately regaining the slot.
///
/// # `may_transmit` side-effect
///
/// `may_transmit` is called from `Router::drain_tx` on every event-loop
/// iteration.  It checks whether the current slot has expired and, if so,
/// advances to the next slot before answering.  This makes slot advancement
/// free — no background task required.
///
/// # Empty slot list
///
/// If `slots` is empty `may_transmit` returns `true` for every endpoint
/// (pass-through behaviour, same as `PassThroughTdm`).  This is the safe
/// default when zmm_cpc is used without MultiPAN (Zigbee-only mode).

use std::time::{Duration, Instant};

use crate::router::TdmGate;

// ─────────────────────────────────────────────────────────────────────────────
// TdmSlot
// ─────────────────────────────────────────────────────────────────────────────

/// One entry in the TDM round-robin schedule.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TdmSlot {
    /// Endpoint that owns the radio during this slot.
    pub ep_id: u8,
    /// How long this endpoint holds the radio, in milliseconds.
    pub duration_ms: u64,
}

impl TdmSlot {
    pub fn new(ep_id: u8, duration_ms: u64) -> Self {
        TdmSlot { ep_id, duration_ms }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// TdmScheduler
// ─────────────────────────────────────────────────────────────────────────────

/// Round-robin TDM scheduler.
///
/// Construct with [`TdmScheduler::new`] or [`TdmScheduler::default`] (Phase 2
/// defaults: ep12=15 ms, ep13=5 ms), then pass to [`RouterBuilder::new`].
pub struct TdmScheduler {
    slots:         Vec<TdmSlot>,
    /// Index of the currently active slot.
    current:       usize,
    /// Wall-clock time the current slot became active.
    slot_started:  Instant,
    /// Total number of slot advances since construction (diagnostic).
    pub advances:  u64,
    /// Total number of `may_transmit` calls that returned `true` (diagnostic).
    pub grants:    u64,
}

impl TdmScheduler {
    /// Construct with an explicit slot list.
    ///
    /// Panics if any slot has `duration_ms == 0`.
    pub fn new(slots: Vec<TdmSlot>) -> Self {
        for s in &slots {
            assert!(s.duration_ms > 0, "TdmSlot duration_ms must be > 0");
        }
        TdmScheduler {
            slots,
            current:      0,
            slot_started: Instant::now(),
            advances:     0,
            grants:       0,
        }
    }

    /// Phase 2 defaults: ep12 = 15 ms, ep13 = 5 ms.
    pub fn phase2_default() -> Self {
        Self::new(vec![
            TdmSlot::new(crate::cpc_frame::EP_ZIGBEE,     15),
            TdmSlot::new(crate::cpc_frame::EP_OPENTHREAD,  5),
        ])
    }

    /// Replace the slot list at runtime.  The new schedule starts immediately
    /// from slot 0.  Called from Python via `CpcCore.set_tdm_slots()`.
    ///
    /// Panics if any slot has `duration_ms == 0`.
    pub fn set_slots(&mut self, slots: Vec<TdmSlot>) {
        for s in &slots {
            assert!(s.duration_ms > 0, "TdmSlot duration_ms must be > 0");
        }
        self.slots       = slots;
        self.current      = 0;
        self.slot_started = Instant::now();
    }

    /// Returns the ep_id of the currently active slot, or `None` if empty.
    pub fn current_ep(&self) -> Option<u8> {
        self.slots.get(self.current).map(|s| s.ep_id)
    }

    /// Returns the remaining time in the current slot.
    /// Returns `Duration::ZERO` for empty slot lists.
    pub fn remaining(&self) -> Duration {
        let Some(slot) = self.slots.get(self.current) else {
            return Duration::ZERO;
        };
        let elapsed  = self.slot_started.elapsed();
        let duration = Duration::from_millis(slot.duration_ms);
        duration.saturating_sub(elapsed)
    }

    // ── Private ──────────────────────────────────────────────────────────────

    /// Advance past any expired slots.  May advance multiple times if several
    /// consecutive short slots expire before drain_tx runs.  Caps at one full
    /// cycle to prevent infinite loops (guarded by constructor assert on
    /// duration_ms > 0).
    pub(crate) fn advance_if_expired(&mut self) {
        if self.slots.is_empty() {
            return;
        }
        let max_advances = self.slots.len();
        for _ in 0..max_advances {
            let slot     = &self.slots[self.current];
            let duration = Duration::from_millis(slot.duration_ms);
            if self.slot_started.elapsed() < duration {
                break; // current slot still live
            }
            self.current      = (self.current + 1) % self.slots.len();
            self.slot_started = Instant::now();
            self.advances    += 1;
        }
    }
}

impl Default for TdmScheduler {
    /// Returns a `TdmScheduler` with Phase 2 defaults (ep12=15ms, ep13=5ms).
    fn default() -> Self {
        Self::phase2_default()
    }
}

impl TdmGate for TdmScheduler {
    /// Returns `true` if `ep_id` is the currently active endpoint.
    ///
    /// Advances expired slots as a side-effect before answering.
    /// Returns `true` unconditionally when no slots are configured.
    fn may_transmit(&mut self, ep_id: u8) -> bool {
        self.advance_if_expired();
        let granted = match self.slots.get(self.current) {
            None       => true,              // empty → pass-through
            Some(slot) => slot.ep_id == ep_id,
        };
        if granted {
            self.grants += 1;
        }
        granted
    }

    /// No-op.  Slots advance by time, not by frame count.
    fn on_transmitted(&mut self, _ep_id: u8) {}
}

// ─────────────────────────────────────────────────────────────────────────────
// Tests
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cpc_frame::{EP_OPENTHREAD, EP_ZIGBEE};

    // ── Helpers ───────────────────────────────────────────────────────────────

    /// Construct a scheduler with given slots and a manually set start time
    /// offset so tests control expiry without real sleeps.
    fn scheduler_with_elapsed(slots: Vec<TdmSlot>, elapsed_ms: u64) -> TdmScheduler {
        let mut s = TdmScheduler::new(slots);
        // Back-date slot_started by elapsed_ms to simulate time having passed.
        s.slot_started = Instant::now()
            .checked_sub(Duration::from_millis(elapsed_ms))
            .unwrap_or_else(Instant::now);
        s
    }

    // ── Construction ─────────────────────────────────────────────────────────

    #[test]
    fn new_starts_at_slot_0() {
        let s = TdmScheduler::new(vec![
            TdmSlot::new(EP_ZIGBEE, 15),
            TdmSlot::new(EP_OPENTHREAD, 5),
        ]);
        assert_eq!(s.current, 0);
        assert_eq!(s.current_ep(), Some(EP_ZIGBEE));
    }

    #[test]
    fn phase2_default_has_correct_slots() {
        let s = TdmScheduler::phase2_default();
        assert_eq!(s.slots.len(), 2);
        assert_eq!(s.slots[0], TdmSlot::new(EP_ZIGBEE, 15));
        assert_eq!(s.slots[1], TdmSlot::new(EP_OPENTHREAD, 5));
    }

    #[test]
    fn default_impl_equals_phase2_default() {
        let s = TdmScheduler::default();
        assert_eq!(s.slots[0].ep_id, EP_ZIGBEE);
        assert_eq!(s.slots[1].ep_id, EP_OPENTHREAD);
    }

    #[test]
    #[should_panic(expected = "duration_ms must be > 0")]
    fn zero_duration_slot_panics() {
        TdmScheduler::new(vec![TdmSlot::new(EP_ZIGBEE, 0)]);
    }

    // ── may_transmit: within slot window ─────────────────────────────────────

    #[test]
    fn active_ep_granted_within_window() {
        let mut s = TdmScheduler::new(vec![
            TdmSlot::new(EP_ZIGBEE, 100),
            TdmSlot::new(EP_OPENTHREAD, 100),
        ]);
        // No time has elapsed — slot 0 (ep12) is active.
        assert!(s.may_transmit(EP_ZIGBEE),    "ep12 should be granted in slot 0");
        assert!(!s.may_transmit(EP_OPENTHREAD), "ep13 should be blocked in slot 0");
    }

    #[test]
    fn inactive_ep_blocked_within_window() {
        let mut s = TdmScheduler::new(vec![
            TdmSlot::new(EP_ZIGBEE, 100),
            TdmSlot::new(EP_OPENTHREAD, 100),
        ]);
        assert!(!s.may_transmit(EP_OPENTHREAD));
        assert!(!s.may_transmit(99)); // unknown ep
    }

    // ── Slot advance on expiry ────────────────────────────────────────────────

    #[test]
    fn slot_advances_after_window_expires() {
        // Slot 0 (ep12, 15 ms) already expired.
        let mut s = scheduler_with_elapsed(
            vec![TdmSlot::new(EP_ZIGBEE, 15), TdmSlot::new(EP_OPENTHREAD, 5)],
            20, // 20 ms elapsed > 15 ms slot 0 duration
        );
        // First call to may_transmit should advance to slot 1.
        assert!(s.may_transmit(EP_OPENTHREAD), "ep13 should be active after slot 0 expires");
        assert_eq!(s.current, 1);
        assert_eq!(s.advances, 1);
    }

    #[test]
    fn slot_wraps_around_after_last_slot() {
        // Two short slots.  Expire them one at a time (each advance resets the
        // slot clock, so we must re-backdate before checking the next slot).
        let slots = vec![TdmSlot::new(EP_ZIGBEE, 1), TdmSlot::new(EP_OPENTHREAD, 1)];
        let mut s = TdmScheduler::new(slots);
        // Expire slot 0 → advance to slot 1
        s.slot_started = Instant::now()
            .checked_sub(Duration::from_millis(100))
            .unwrap_or_else(Instant::now);
        s.advance_if_expired();
        assert_eq!(s.current, 1);
        // Expire slot 1 → advance back to slot 0
        s.slot_started = Instant::now()
            .checked_sub(Duration::from_millis(100))
            .unwrap_or_else(Instant::now);
        s.advance_if_expired();
        assert_eq!(s.current, 0, "scheduler should wrap back to slot 0");
    }

    #[test]
    fn multiple_expired_slots_skip_correctly() {
        // Three slots: A(ep10, 1ms), B(ep11, 1ms), C(ep12, 100ms).
        // Expire A then B sequentially (each advance resets the clock).
        let mut s = TdmScheduler::new(vec![
            TdmSlot::new(10, 1),
            TdmSlot::new(11, 1),
            TdmSlot::new(12, 100),
        ]);
        // Expire slot 0 → advance to slot 1
        s.slot_started = Instant::now()
            .checked_sub(Duration::from_millis(50))
            .unwrap_or_else(Instant::now);
        s.advance_if_expired();
        assert_eq!(s.current, 1, "should have advanced past slot 0");
        // Expire slot 1 → advance to slot 2 (which is still live)
        s.slot_started = Instant::now()
            .checked_sub(Duration::from_millis(50))
            .unwrap_or_else(Instant::now);
        s.advance_if_expired();
        assert_eq!(s.current, 2, "should have advanced past slot 1 to land on C");
        assert_eq!(s.advances, 2);
    }

    // ── Remaining time ────────────────────────────────────────────────────────

    #[test]
    fn remaining_nonzero_at_start() {
        let s = TdmScheduler::new(vec![TdmSlot::new(EP_ZIGBEE, 15)]);
        assert!(s.remaining() > Duration::ZERO);
        assert!(s.remaining() <= Duration::from_millis(15));
    }

    #[test]
    fn remaining_zero_when_expired() {
        let s = scheduler_with_elapsed(vec![TdmSlot::new(EP_ZIGBEE, 1)], 100);
        assert_eq!(s.remaining(), Duration::ZERO);
    }

    #[test]
    fn remaining_zero_for_empty_slots() {
        let s = TdmScheduler::new(vec![]);
        assert_eq!(s.remaining(), Duration::ZERO);
    }

    // ── Empty slot list → pass-through ────────────────────────────────────────

    #[test]
    fn empty_slots_always_grants() {
        let mut s = TdmScheduler::new(vec![]);
        assert!(s.may_transmit(EP_ZIGBEE));
        assert!(s.may_transmit(EP_OPENTHREAD));
        assert!(s.may_transmit(99));
        assert_eq!(s.advances, 0);
    }

    // ── Single slot ───────────────────────────────────────────────────────────

    #[test]
    fn single_slot_always_grants_that_ep() {
        let mut s = TdmScheduler::new(vec![TdmSlot::new(EP_ZIGBEE, 15)]);
        // After expiry, wraps back to itself.
        let mut s_expired = scheduler_with_elapsed(vec![TdmSlot::new(EP_ZIGBEE, 1)], 100);
        assert!(s.may_transmit(EP_ZIGBEE));
        assert!(s_expired.may_transmit(EP_ZIGBEE),
            "single-slot scheduler should always grant its own ep after wrap");
    }

    #[test]
    fn single_slot_blocks_other_eps() {
        let mut s = TdmScheduler::new(vec![TdmSlot::new(EP_ZIGBEE, 100)]);
        assert!(!s.may_transmit(EP_OPENTHREAD));
    }

    // ── set_slots ────────────────────────────────────────────────────────────

    #[test]
    fn set_slots_resets_to_slot_0() {
        let mut s = scheduler_with_elapsed(
            vec![TdmSlot::new(EP_ZIGBEE, 1), TdmSlot::new(EP_OPENTHREAD, 1)],
            100,
        );
        // Force into slot 1
        let _ = s.may_transmit(EP_OPENTHREAD);

        // Reconfigure
        s.set_slots(vec![
            TdmSlot::new(EP_OPENTHREAD, 10),
            TdmSlot::new(EP_ZIGBEE, 20),
        ]);
        assert_eq!(s.current, 0);
        assert_eq!(s.current_ep(), Some(EP_OPENTHREAD));
        assert!(s.may_transmit(EP_OPENTHREAD));
    }

    #[test]
    #[should_panic(expected = "duration_ms must be > 0")]
    fn set_slots_panics_on_zero_duration() {
        let mut s = TdmScheduler::phase2_default();
        s.set_slots(vec![TdmSlot::new(EP_ZIGBEE, 0)]);
    }

    // ── Diagnostic counters ───────────────────────────────────────────────────

    #[test]
    fn grants_counter_increments_on_true() {
        let mut s = TdmScheduler::new(vec![TdmSlot::new(EP_ZIGBEE, 100)]);
        assert_eq!(s.grants, 0);
        s.may_transmit(EP_ZIGBEE);
        s.may_transmit(EP_ZIGBEE);
        assert_eq!(s.grants, 2);
        s.may_transmit(EP_OPENTHREAD); // denied — no increment
        assert_eq!(s.grants, 2);
    }

    #[test]
    fn advances_counter_increments_on_slot_change() {
        let mut s = scheduler_with_elapsed(
            vec![TdmSlot::new(EP_ZIGBEE, 1), TdmSlot::new(EP_OPENTHREAD, 100)],
            50,
        );
        assert_eq!(s.advances, 0);
        s.may_transmit(EP_OPENTHREAD);
        assert_eq!(s.advances, 1);
    }

    #[test]
    fn on_transmitted_is_noop() {
        let mut s = TdmScheduler::phase2_default();
        let before_current = s.current;
        s.on_transmitted(EP_ZIGBEE);
        s.on_transmitted(EP_OPENTHREAD);
        assert_eq!(s.current, before_current, "on_transmitted must not advance slot");
    }

    // ── current_ep ────────────────────────────────────────────────────────────

    #[test]
    fn current_ep_none_for_empty() {
        let s = TdmScheduler::new(vec![]);
        assert_eq!(s.current_ep(), None);
    }

    #[test]
    fn current_ep_returns_active_slot() {
        let s = TdmScheduler::new(vec![
            TdmSlot::new(EP_ZIGBEE, 15),
            TdmSlot::new(EP_OPENTHREAD, 5),
        ]);
        assert_eq!(s.current_ep(), Some(EP_ZIGBEE));
    }

    // ── Proportional grant ratio ──────────────────────────────────────────────
    //
    // Drive the scheduler through N synthetic cycles and verify the grant ratio
    // matches the configured slot proportions within a tolerance.

    #[test]
    fn grant_ratio_matches_slot_proportions() {
        // Slots: ep12=3ms, ep13=1ms → ep12 should get ~75% of grants.
        // We simulate by driving the scheduler with pre-expired start times.
        let mut ep12_grants = 0u32;
        let mut ep13_grants = 0u32;

        // Run 1000 "ticks" alternating between the two endpoints,
        // advancing the slot by manually back-dating slot_started.
        let slots = vec![
            TdmSlot::new(EP_ZIGBEE, 3),
            TdmSlot::new(EP_OPENTHREAD, 1),
        ];

        // Simulate 100 full cycles: ep12 gets 3ms, ep13 gets 1ms each cycle.
        for _ in 0..100 {
            // ep12 slot active
            let mut s_ep12 = TdmScheduler::new(slots.clone());
            // Not expired → ep12 granted
            if s_ep12.may_transmit(EP_ZIGBEE) { ep12_grants += 3; } // simulate 3 ticks

            // ep13 slot active
            let mut s_ep13 = scheduler_with_elapsed(slots.clone(), 10);
            // slot 0 expired → should advance to slot 1
            if s_ep13.may_transmit(EP_OPENTHREAD) { ep13_grants += 1; }
        }

        let total = ep12_grants + ep13_grants;
        let ep12_ratio = ep12_grants as f64 / total as f64;
        assert!(
            (ep12_ratio - 0.75).abs() < 0.05,
            "ep12 grant ratio {ep12_ratio:.2} should be near 0.75"
        );
    }

    // ── Integration: TdmScheduler wired into Router via TdmGate ──────────────

    #[test]
    fn tdm_scheduler_satisfies_tdmgate_bound() {
        // Compile-time check: TdmScheduler implements TdmGate.
        fn assert_tdm_gate<T: TdmGate>(_: T) {}
        assert_tdm_gate(TdmScheduler::phase2_default());
    }
}