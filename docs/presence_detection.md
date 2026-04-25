# Presence Detection — How It Works & How to Use It

Zones turn the Zigbee mesh itself into a passive presence sensor. You
don't need PIRs, mmWave radars, or any extra hardware — just the Zigbee
devices you already have in the room.

---

## 1. The idea in one paragraph

Every time one of your Zigbee devices talks to the coordinator, the radio
records how strong the signal was (**RSSI**, in dBm). When nothing is
moving in the room, that signal is remarkably stable — within ~1–2 dB
from minute to minute. When a human body walks into the path, it absorbs
and scatters 2.4 GHz radio waves enough to knock the RSSI around by
5–15 dB. We measure that wobble and call it presence.

This is sometimes called **Channel State Information (CSI) sensing** or
**Wi-Fi/Zigbee sensing**. Our implementation works on raw RSSI because
Zigbee radios don't expose full CSI, but the physics is the same.

---

## 2. What we actually measure

For each device in a zone we record, on every frame the coordinator
receives from it:

- **RSSI** — signal strength in dBm (typically −30 to −95)
- **LQI** — link quality indicator (0–255)
- **Timestamp**

We keep a rolling 5-sample smoothed RSSI for the live "current" value,
and we keep a longer history for debugging.

**Only RSSI drives detection.** LQI is captured for diagnostics. We
never use fabricated LQI-to-RSSI conversions or neighbor-table polling;
both are too slow and too filtered to see a person walk past.

---

## 3. Calibration — the most important step

Calibration tells the system what "empty room" looks like for each
device. Without it, there is no "normal" to deviate from.

### The golden rule

> **Leave the room completely empty for the whole calibration window.**
> No pets, no running fans directly in the RF path, no open/closing of
> doors. Stillness is what you're capturing.

### How to calibrate

1. Open the zone's details page.
2. Walk out of the room. Close the door behind you if practical.
3. Click **Calibrate (room empty)**.
4. Wait for the timer to finish (default 2 minutes). You can click
   **Finalise now** once you've seen enough samples for each device.
5. State should transition to **VACANT**.

### What happens under the hood

During the window we collect every RSSI sample from every zone device.
At the end:

- We discard the top and bottom 10% of values (outlier trim).
- Compute the mean (`μ`) and standard deviation (`σ`) of what's left.
- `σ` is floored at 1.0 dB so quiet devices don't produce runaway
  deviation numbers on tiny fluctuations.

Presence is then measured in **σ units**: a device has "triggered" when
its current smoothed RSSI is more than `N × σ` away from its baseline
mean, where `N` is the threshold you configure (default 2.5).

### When to recalibrate

- You moved furniture that sits between a device and the coordinator.
- You added or removed a zone member.
- You changed Zigbee channel.
- Seasonal change (leaves outside, heating on) — rare, but real.
- After firmware upgrades on the coordinator or major routers.

Recalibration is always safe. It takes 2 minutes.

---

## 4. Choosing devices for a zone

Not every device is equally useful. The best zone members are:

### Ideal — mains-fed routers physically in the room

Smart bulbs, smart plugs, in-wall switches, repeaters. They talk often
and are awake 24/7. These carry the signal.

### Useful but secondary — battery-powered end devices

Contact sensors, motion sensors, TRVs, remotes. They only talk when
they wake up, so the cadence is irregular. We treat their triggers
with half the weight of router triggers so a chatty sensor can't
single-handedly flip your zone.

### Don't bother — devices that barely transmit

A door sensor on a door that never opens gives you one RSSI sample a
day. It contributes nothing. Leave it out.

### Guidance

- **2–4 routers** in the room is the sweet spot. More is fine.
- **One router minimum** for reliable detection.
- More devices → more redundancy, less false-trigger sensitivity.
- Spread devices physically — corners > one corner.

---

## 5. Aggressiveness — the per-router sensitivity knob

Each mains-fed router in a zone can have its own aggressiveness value:

| Value | Behaviour |
|-------|-----------|
| `0.5` | Very sensitive — trigger on half the zone's σ threshold |
| `1.0` | Zone default |
| `2.0` | Very relaxed — ignore almost everything |

Setting aggressiveness on a router does two things:

1. Changes that router's σ threshold.
2. Opts the router into **1–5 second LQI reporting**. Before you set
   any aggressiveness, routers report on the default (looser) schedule.

**End devices can't be tuned.** Their transmit cadence is dictated by
their own wake cycle, so a tighter σ threshold just invents false
positives on their irregular wake-up RSSI spread. The UI shows a badge
instead of an input for them.

### When to tune

- **Room too sensitive (triggers on empty room)** — raise the zone
  `deviation_threshold` to 3.0 or 3.5, or bump the aggressiveness of
  the noisiest router to 1.3–1.5.
- **Room too slow to trigger** — lower the aggressiveness of the most
  stable router (a mains plug against a wall is usually best) to
  0.7–0.8.
- **Only one router jitters a lot** — set its aggressiveness to 1.5–2.0
  to quiet it down without affecting the rest.

---

## 6. Zone-level tuning

Visible in the zone's config section:

**`deviation_threshold`** (default 2.5)
Baseline σ threshold for the zone. Every router's threshold is this
number multiplied by its own aggressiveness.

**`min_devices_triggered`** (default 1.5)
The weighted sum of triggering devices that flips the zone to OCCUPIED.
Routers count as 1.0, end devices count as 0.5. So 1.5 means "one
router and one end device", "two end devices", or "1.5 routers worth".
Raise to 2.0 if you have many devices and want to wait for
corroboration. Drop to 1.0 in a small room where a single router is
enough.

**`clear_delay`** (default 15 s)
How long the room must stay quiet before VACANT. Raise this if you
frequently see a quick OCCUPIED→VACANT→OCCUPIED during brief stillness
(e.g. sitting down at a desk). 30–60 s is reasonable for rooms where
you're often stationary.

**`calibration_time`** (default 120 s)
Longer calibration = more samples = tighter baseline. For a room with
slow-talking devices, raise this to 300 s.

**`end_device_weight`** (default 0.5)
Drop to 0.2 if your end devices are triggering nonsense. Raise to 0.8
if you have no routers and must rely on them.

---

## 7. Improving accuracy — a playbook

### Step 1: Get at least one solid router in the room

A mains-fed plug or bulb that's awake 24/7. This is the foundation. If
you don't have one, buy a cheap plug and pair it into the room.

### Step 2: Calibrate once properly

Take it seriously. Leave the room, shut the door, wait 2 minutes. The
difference between a hurried calibration and a clean one is night and
day.

### Step 3: Turn on aggressive reporting for your best router

In the Live RSSI tab, set that router's aggressiveness to `1.0`. This
opts it into 1–5 s reporting. You'll see the sample count climb much
faster after this.

### Step 4: Watch during a known-empty period

With the room empty, watch the Live RSSI tab for 10 minutes. No device
should stay triggered for long. If one is constantly glowing red, it
has a noisy baseline — raise its aggressiveness to 1.3 or recalibrate.

### Step 5: Walk in and watch

Walk in. Observe which devices trigger. If none do, your RF path isn't
crossing you — try a router on the opposite wall from the coordinator
so your body is between them. If all of them trigger together, great —
you're done.

### Step 6: Tune `min_devices_triggered` to taste

If you want snappy detection with occasional false positives: 1.0.
If you want conservative detection with near-zero false positives: 2.0.

---

## 8. Troubleshooting

### "Zone never becomes OCCUPIED"

- Is it calibrated? `UNCALIBRATED` and `CALIBRATING` states never
  trigger.
- Do any devices show recent RSSI (`last_seen` within 60 s for a
  router)? If not, the device isn't talking often enough — turn on
  aggressiveness for at least one router.
- Is `min_devices_triggered` too high? Drop it to 1.0 for testing.
- Is the coordinator sitting in the same room as all zone members?
  If so, your body can't block any paths. Move one member.

### "Zone constantly flickers between OCCUPIED and VACANT"

- Raise `clear_delay` to 30–60 s.
- One noisy device is probably doing it — check which device has the
  highest `σ` in the Live RSSI tab and raise its aggressiveness.

### "Zone is OCCUPIED when nobody's home"

- Recalibrate with the room genuinely empty. Did the original
  calibration include a pet, a fan, an open window?
- Raise `deviation_threshold` from 2.5 to 3.0.
- Check for external interference: a new Wi-Fi AP on the same channel,
  a new USB3 device next to the coordinator, a running microwave.

### "Baseline didn't compute for some devices"

At least 20 samples per device are required during the calibration
window. Sleepy end devices often can't produce that many in 2 minutes.
Options:
- Raise `calibration_time` to 300 s.
- Wake the device a few times during calibration (press a button,
  open a door).
- Accept that it just won't contribute — router triggers alone are
  usually enough.

### "Calibration finishes but state stays UNCALIBRATED"

Means zero devices produced baselines. Either no device talked at all
during the window (check the Live RSSI tab — are the sample counts
zero?), or every device produced fewer than 20 samples. Same remedies
as above.

---

## 9. Limits and honest caveats

- **It's coarse.** We see "something big changed in the room", not
  "there's a person at position X". If you need per-square-metre
  occupancy, use mmWave radar.
- **Pets count.** A medium-to-large dog or cat moving around is a
  detectable RF perturbation. If this matters, raise the threshold or
  accept it.
- **Stillness reads as empty.** Someone sitting motionless (sleeping,
  reading, working at a desk) barely perturbs the RF field. Use this
  in combination with other signals if that's a problem.
- **Doors and windows matter.** A closed door is opaque to 2.4 GHz;
  opening it changes the multipath environment even with no one in the
  room. If a zone is on a door that opens often, be prepared to see a
  trigger.
- **Calibration is room-specific.** Do not clone a zone's tuning
  values between rooms and expect identical behaviour.

---

## 10. Integration with Home Assistant

Each zone publishes to MQTT as a `binary_sensor` with device class
`occupancy`. HA auto-discovers it via MQTT Discovery. The entity name
is `{Zone Name} Occupancy`. Use it in automations exactly like you
would a motion sensor — except this one doesn't go `VACANT` the moment
you stop moving, which for most "lights on while I'm in the room" use
cases is exactly what you want.