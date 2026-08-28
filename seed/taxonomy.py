"""The FitForge product and fault taxonomy.

This module is the single source of truth shared by every generator. The point
is that the manuals, the parts catalog, the error-code tables and the symptom
tags are all derived from the same definitions — so when the agent retrieves a
troubleshooting section and then looks up a part for the fault it found, the two
actually agree. A corpus where they don't agree makes the retrieval layer look
better or worse than it is, and the demo stops meaning anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PartTemplate:
    """A part that exists on every model in a category."""

    slug: str
    name: str
    part_class: str          # frame | mechanical | electronics | consumable
    base_price_cents: int
    symptom_tags: tuple[str, ...]
    customer_replaceable: bool = True
    safety_class: str = "standard"


@dataclass(frozen=True)
class ErrorCodeTemplate:
    code: str
    title: str
    meaning: str
    first_actions: str
    likely_part_slugs: tuple[str, ...] = ()


@dataclass(frozen=True)
class FaultTemplate:
    """One troubleshooting entry: symptom -> ordered diagnostic steps.

    The steps are written the way a real service manual writes them — cheapest,
    safest, most likely check first — because the agent's diagnostic loop is
    only as good as the ordering it can retrieve.
    """

    symptom: str
    aliases: tuple[str, ...]
    steps: tuple[str, ...]
    resolves_without_part: bool
    likely_part_slugs: tuple[str, ...] = ()
    safety_note: str | None = None


@dataclass(frozen=True)
class CategoryTemplate:
    id: str
    name: str
    safety_class: str
    families: tuple[tuple[str, str], ...]     # (family_id, display name)
    serial_letter: str
    parts: tuple[PartTemplate, ...]
    error_codes: tuple[ErrorCodeTemplate, ...]
    faults: tuple[FaultTemplate, ...]
    feature_axes: dict[str, tuple[str, ...]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# TREADMILLS — high voltage. The category where refusing to help is often the
# correct answer, which makes it the best category for testing safety gating.
# ---------------------------------------------------------------------------

TREADMILL = CategoryTemplate(
    id="treadmill",
    name="Treadmill",
    safety_class="high_voltage",
    serial_letter="T",
    families=(
        ("pacer", "Pacer"),
        ("summit", "Summit"),
        ("trailblazer", "Trailblazer"),
        ("velocity", "Velocity"),
        ("endurance", "Endurance"),
    ),
    feature_axes={
        "console": ("5in LCD", "7in colour touch", "10in HD touch", "22in HD touch"),
        "deck": ("black", "graphite", "charcoal"),
        "folding": ("folding", "fixed"),
        "incline": ("0-12%", "-3 to 15%", "0-15%"),
    },
    parts=(
        PartTemplate("belt", "Running Belt", "consumable", 8900,
                     ("belt slipping", "belt frayed", "belt off centre", "belt worn")),
        PartTemplate("deck", "Running Deck", "mechanical", 21900,
                     ("deck worn", "deck cracked", "loud thud when running")),
        PartTemplate("motor", "Drive Motor", "electronics", 34900,
                     ("no power to belt", "burning smell", "motor stalls", "belt stops under load"),
                     customer_replaceable=False, safety_class="restricted"),
        PartTemplate("motor-controller", "Motor Control Board", "electronics", 27900,
                     ("intermittent power", "speed fluctuates", "error e1", "error e7"),
                     customer_replaceable=False, safety_class="restricted"),
        PartTemplate("console", "Console Assembly", "electronics", 19900,
                     ("blank display", "console unresponsive", "buttons not working")),
        PartTemplate("speed-sensor", "Speed Sensor", "electronics", 4900,
                     ("speed reads zero", "speed jumps", "error e2")),
        PartTemplate("incline-motor", "Incline Motor", "mechanical", 15900,
                     ("incline stuck", "incline grinding", "error e5")),
        PartTemplate("front-roller", "Front Roller", "mechanical", 11900,
                     ("squeaking", "belt tracking off", "roller noise")),
        PartTemplate("rear-roller", "Rear Roller", "mechanical", 9900,
                     ("squeaking at rear", "belt tracking off")),
        PartTemplate("safety-key", "Safety Key", "consumable", 1900,
                     ("will not start", "stops immediately", "safety key error")),
        PartTemplate("power-cord", "Power Cord", "consumable", 2900,
                     ("no power", "dead unit", "intermittent power")),
        PartTemplate("belt-lube", "Silicone Belt Lubricant", "consumable", 1600,
                     ("belt squeaking", "belt stiff", "high friction")),
    ),
    error_codes=(
        ErrorCodeTemplate("E1", "Motor controller communication fault",
                          "The console has lost communication with the motor control board.",
                          "Power the unit off at the wall for 60 seconds, then restart. "
                          "If the code returns, the console-to-controller data cable or the "
                          "controller itself has failed.",
                          ("motor-controller", "console")),
        ErrorCodeTemplate("E2", "Speed sensor signal lost",
                          "No pulse detected from the speed sensor while the motor is driven.",
                          "Check that the sensor is seated against the front roller pulley and "
                          "that its connector is fully home on the controller.",
                          ("speed-sensor",)),
        ErrorCodeTemplate("E5", "Incline calibration failure",
                          "The incline motor did not reach its home position within the "
                          "expected travel time.",
                          "Run the incline calibration routine from the service menu. If it "
                          "fails again the incline motor or its limit switch has failed.",
                          ("incline-motor",)),
        ErrorCodeTemplate("E7", "Motor over-current",
                          "The controller shut the motor down because current exceeded the "
                          "safe limit — most often excessive belt friction, not a failed motor.",
                          "STOP USE. Check belt tension and lubrication first; an over-tight "
                          "or dry belt is the usual cause. Do not restart repeatedly.",
                          ("belt-lube", "belt", "motor-controller")),
        ErrorCodeTemplate("E9", "Safety key not detected",
                          "The safety key magnet is not registering.",
                          "Reseat the safety key. If the unit still will not start, the key "
                          "magnet has weakened and the key needs replacing.",
                          ("safety-key",)),
    ),
    faults=(
        FaultTemplate(
            symptom="belt slipping or hesitating under foot",
            aliases=("belt slips", "belt hesitates", "belt catches", "jerky belt"),
            steps=(
                "Confirm the treadmill is on a level, hard surface and not on carpet or a mat that bunches.",
                "With the unit OFF and unplugged, lift the belt at the centre of the deck. "
                "You should be able to raise it 50-75 mm (2-3 in). More than that means the belt is loose.",
                "If the belt is loose, tighten both rear roller bolts by a quarter turn each, "
                "alternating sides, then re-check. Never exceed one full turn total.",
                "Walk on the belt at 3 km/h and confirm the slipping has stopped.",
                "If the belt is correctly tensioned but still slips, check the drive belt under "
                "the motor hood for glazing or cracking.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("belt",),
        ),
        FaultTemplate(
            symptom="belt drifting to one side",
            aliases=("belt off centre", "belt tracking", "belt moves left", "belt moves right"),
            steps=(
                "Run the belt at 5 km/h with nobody on it and note which side it drifts toward.",
                "If it drifts RIGHT, turn the right rear roller bolt clockwise a quarter turn.",
                "If it drifts LEFT, turn the left rear roller bolt clockwise a quarter turn.",
                "Let the belt run for two minutes after each adjustment before judging the result.",
                "If more than two full turns are needed to centre the belt, the rear roller is "
                "bent or the frame is out of square.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("rear-roller",),
        ),
        FaultTemplate(
            symptom="console is blank or will not power on",
            aliases=("no display", "dead console", "screen black", "unit will not turn on"),
            steps=(
                "Confirm the unit is plugged directly into a wall outlet, not an extension lead "
                "or surge protector.",
                "Check the reset breaker beside the power inlet — press it fully in if it has tripped.",
                "Confirm the safety key is seated in its holder.",
                "Try a different wall outlet that you know works.",
                "If the breaker trips again immediately after resetting, stop and do not retry — "
                "this indicates a short circuit inside the unit.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("power-cord", "safety-key", "console"),
            safety_note="If the breaker trips repeatedly, or you smell burning, unplug the unit "
                        "and stop. Do not open the motor hood.",
        ),
        FaultTemplate(
            symptom="loud squeaking or grinding noise while running",
            aliases=("squeak", "grinding", "screeching", "rubbing noise"),
            steps=(
                "Determine whether the noise follows belt speed (roller or belt) or is constant "
                "(motor or fan).",
                "With the unit off and unplugged, check the deck surface under the belt for a "
                "dry, dusty feel — a properly lubricated deck feels slightly slick.",
                "If the deck is dry, apply silicone lubricant per the maintenance section.",
                "If the noise is a rhythmic squeak once per belt revolution, inspect the belt "
                "seam for lifting.",
                "If the noise is metallic and constant, the roller bearings have failed.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("belt-lube", "front-roller", "rear-roller"),
        ),
        FaultTemplate(
            symptom="burning smell or smoke from the unit",
            aliases=("burning", "smoke", "smells hot", "electrical smell"),
            steps=(
                "STOP. Switch the unit off at the wall and unplug it immediately.",
                "Do not restart the unit to reproduce the symptom.",
                "Do not remove the motor hood.",
            ),
            resolves_without_part=False,
            likely_part_slugs=("motor", "motor-controller"),
            safety_note="Mains-voltage fault. This must be handled by an authorised technician; "
                        "no customer-guided diagnosis is permitted.",
        ),
        FaultTemplate(
            symptom="incline will not move or is stuck",
            aliases=("incline stuck", "incline not working", "wont incline"),
            steps=(
                "Run the incline calibration routine: hold the STOP and SPEED-UP keys for five "
                "seconds with the safety key inserted.",
                "Watch whether the incline motor attempts to move at all — listen for a hum.",
                "If there is a hum but no movement, the incline actuator is jammed or its nut "
                "has stripped.",
                "If there is no hum, the controller is not driving the actuator.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("incline-motor", "motor-controller"),
        ),
    ),
)


# ---------------------------------------------------------------------------
# SMART BIKES
# ---------------------------------------------------------------------------

BIKE = CategoryTemplate(
    id="bike",
    name="Smart Bike",
    safety_class="standard",
    serial_letter="B",
    families=(
        ("velodrome", "Velodrome"),
        ("peloton-x", "Circuit"),
        ("ascent", "Ascent"),
        ("criterium", "Criterium"),
    ),
    feature_axes={
        "console": ("no screen", "10in touch", "14in touch", "22in touch"),
        "resistance": ("magnetic", "electromagnetic", "direct drive"),
        "pedals": ("dual SPD/cage", "delta cleat", "flat"),
    },
    parts=(
        PartTemplate("display", "Touchscreen Display", "electronics", 32900,
                     ("blank screen", "display flickering", "touch not responding", "error b3")),
        PartTemplate("resistance-motor", "Resistance Actuator", "electronics", 18900,
                     ("resistance stuck", "no resistance change", "error b1")),
        PartTemplate("crank-arm", "Crank Arm", "mechanical", 7900,
                     ("clicking when pedalling", "crank loose", "wobble")),
        PartTemplate("pedal-set", "Pedal Set", "consumable", 5900,
                     ("pedal loose", "pedal clicking", "cleat wont engage")),
        PartTemplate("belt-drive", "Drive Belt", "mechanical", 6900,
                     ("slipping under load", "squealing", "belt worn")),
        PartTemplate("flywheel-bearing", "Flywheel Bearing Set", "mechanical", 8900,
                     ("rumbling", "grinding", "flywheel noise")),
        PartTemplate("power-meter", "Power Meter Sensor", "electronics", 12900,
                     ("power reads zero", "cadence missing", "erratic watts", "error b2")),
        PartTemplate("seat-post", "Seat Post Assembly", "mechanical", 6900,
                     ("seat slips down", "seat wobbles", "post seized")),
        PartTemplate("handlebar-post", "Handlebar Post", "mechanical", 7900,
                     ("handlebar wobble", "post seized")),
        PartTemplate("psu", "Power Supply Unit", "electronics", 4900,
                     ("no power", "intermittent power", "wont charge")),
    ),
    error_codes=(
        ErrorCodeTemplate("B1", "Resistance actuator out of range",
                          "The actuator did not reach the commanded position.",
                          "Run resistance calibration from Settings > Device > Calibrate. "
                          "If it fails twice the actuator has failed.",
                          ("resistance-motor",)),
        ErrorCodeTemplate("B2", "Power meter signal invalid",
                          "Cadence or torque readings are outside plausible bounds.",
                          "Check the sensor cable at the bottom bracket is seated. Re-run the "
                          "zero-offset calibration with the cranks stationary.",
                          ("power-meter",)),
        ErrorCodeTemplate("B3", "Display panel not detected",
                          "The mainboard cannot enumerate the display.",
                          "Power cycle by unplugging for 60 seconds. Reseat the display ribbon "
                          "cable behind the handlebar post cover.",
                          ("display",)),
        ErrorCodeTemplate("B6", "Firmware update failed",
                          "The last over-the-air update did not complete.",
                          "Reconnect to Wi-Fi and retry from Settings > System > Update. The "
                          "bike will fall back to the previous firmware automatically.",
                          ()),
    ),
    faults=(
        FaultTemplate(
            symptom="screen is blank or stuck on the logo",
            aliases=("blank screen", "wont boot", "stuck on startup", "display dead"),
            steps=(
                "Confirm the power brick LED is lit and the barrel connector is fully seated "
                "at the frame.",
                "Hold the power button for 20 seconds to force a hard reset.",
                "If the screen shows the logo then goes dark, the display is receiving power but "
                "failing to boot — try the update-recovery combination in the manual.",
                "Reseat the display ribbon cable behind the handlebar post cover.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("display", "psu"),
        ),
        FaultTemplate(
            symptom="resistance does not change when adjusted",
            aliases=("no resistance", "resistance stuck", "always easy", "always hard"),
            steps=(
                "Confirm the resistance value on screen actually changes when you adjust it — "
                "if the number is frozen, this is a display or software fault, not mechanical.",
                "Run resistance calibration from Settings > Device > Calibrate.",
                "Listen for the actuator motor during calibration; a healthy actuator makes a "
                "clear whirring sweep.",
                "If there is no actuator sound at all, check the actuator connector at the mainboard.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("resistance-motor",),
        ),
        FaultTemplate(
            symptom="clicking or knocking noise when pedalling",
            aliases=("clicking", "knocking", "creaking", "tick each rotation"),
            steps=(
                "Pedal slowly and note whether the noise happens once per crank revolution.",
                "Check both pedals are torqued to 35 Nm — the left pedal is a LEFT-HAND thread.",
                "If tightening the pedals does not fix it, remove them and apply a thin film of "
                "grease to the threads.",
                "Check the crank arm bolts at the bottom bracket.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("pedal-set", "crank-arm"),
        ),
        FaultTemplate(
            symptom="power or cadence readings are zero or erratic",
            aliases=("no watts", "no cadence", "power drops out", "erratic readings"),
            steps=(
                "Confirm the ride screen is showing a live cadence figure while you pedal.",
                "Perform the zero-offset calibration with the cranks stationary and no weight "
                "on the pedals.",
                "Check the sensor cable at the bottom bracket for a loose or damaged connector.",
                "If readings drop out only under high load, the sensor mount has flexed and "
                "needs re-shimming.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("power-meter",),
        ),
        FaultTemplate(
            symptom="seat slips down during a ride",
            aliases=("seat sinks", "saddle drops", "post slips"),
            steps=(
                "Confirm the seat post clamp lever is closed to a firm, not merely snug, position.",
                "Wipe the seat post and the inside of the frame collar — grease on the post is "
                "the most common cause.",
                "Check the clamp adjustment nut; the lever should require real force in the last "
                "20 degrees of travel.",
                "Inspect the post for scoring, which prevents the clamp gripping.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("seat-post",),
        ),
    ),
)


# ---------------------------------------------------------------------------
# ROWERS
# ---------------------------------------------------------------------------

ROWER = CategoryTemplate(
    id="rower",
    name="Rower",
    safety_class="standard",
    serial_letter="R",
    families=(
        ("tidal", "Tidal"),
        ("regatta", "Regatta"),
        ("current", "Current"),
    ),
    feature_axes={
        "resistance": ("air", "water", "magnetic", "air+magnetic"),
        "console": ("mono LCD", "8in touch", "16in touch"),
        "rail": ("aluminium", "steel", "folding aluminium"),
    },
    parts=(
        PartTemplate("handle", "Rowing Handle", "consumable", 4900,
                     ("handle worn", "grip peeling", "handle loose")),
        PartTemplate("strap", "Pull Strap", "consumable", 3900,
                     ("strap fraying", "strap wont retract", "strap slack", "strap broken")),
        PartTemplate("bungee", "Return Bungee", "consumable", 2400,
                     ("strap wont retract", "slow return", "handle hangs")),
        PartTemplate("seat-roller", "Seat Roller Set", "mechanical", 5900,
                     ("seat rough", "seat noisy", "seat sticks", "rumbling on rail")),
        PartTemplate("monitor", "Performance Monitor", "electronics", 15900,
                     ("no readings", "monitor blank", "erratic stroke rate", "error r2")),
        PartTemplate("chain", "Drive Chain", "mechanical", 6900,
                     ("chain noisy", "chain stiff", "chain rusted")),
        PartTemplate("damper", "Damper Assembly", "mechanical", 7900,
                     ("damper stuck", "resistance wont change")),
        PartTemplate("battery-tray", "Monitor Battery Tray", "consumable", 1400,
                     ("monitor wont power", "batteries loose")),
    ),
    error_codes=(
        ErrorCodeTemplate("R1", "No stroke detected",
                          "The monitor is powered but seeing no flywheel rotation.",
                          "Check the sensor gap at the flywheel and confirm the sensor cable is "
                          "plugged into the monitor arm.",
                          ("monitor",)),
        ErrorCodeTemplate("R2", "Monitor low battery",
                          "Supply voltage has dropped below the operating threshold.",
                          "Replace both cells with fresh alkaline D cells. Rechargeables often "
                          "sit below the threshold even when full.",
                          ("battery-tray",)),
    ),
    faults=(
        FaultTemplate(
            symptom="pull strap will not retract fully",
            aliases=("strap slack", "handle hangs", "strap wont return", "slow return"),
            steps=(
                "Pull the handle out fully and release it slowly, watching whether the strap "
                "retracts at a constant rate or stalls at a particular point.",
                "Check the bungee tension adjuster at the front of the rail.",
                "If the strap stalls at one point, inspect the strap at that point for fraying "
                "or a twist in the pulley.",
                "Tighten the bungee one notch and re-test.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("bungee", "strap"),
        ),
        FaultTemplate(
            symptom="seat is rough or noisy on the rail",
            aliases=("seat noisy", "seat sticks", "bumpy seat", "rumbling"),
            steps=(
                "Wipe the full length of the rail with a dry cloth — grit on the rail is the "
                "usual cause.",
                "Inspect each seat roller for flat spots by spinning it with a finger.",
                "Check the rail for dents, particularly if the machine has been stored upright "
                "and dropped.",
                "Do NOT lubricate the rail; it attracts grit and makes the problem worse.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("seat-roller",),
        ),
        FaultTemplate(
            symptom="monitor shows no readings while rowing",
            aliases=("no readings", "monitor blank", "no stroke rate", "zero split"),
            steps=(
                "Confirm the monitor wakes when you press a key — if not, replace the batteries.",
                "Spin the flywheel by hand and watch whether the monitor registers anything.",
                "Check the sensor cable where it enters the monitor arm; this cable flexes every "
                "time the machine is folded and is the usual failure point.",
                "Confirm the sensor gap at the flywheel is roughly 2 mm.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("monitor", "battery-tray"),
        ),
    ),
)


# ---------------------------------------------------------------------------
# CABLE / STRENGTH SYSTEMS — high tension, its own safety class.
# ---------------------------------------------------------------------------

CABLE = CategoryTemplate(
    id="cable",
    name="Cable System",
    safety_class="high_tension",
    serial_letter="C",
    families=(
        ("forge", "Forge"),
        ("anvil", "Anvil"),
        ("keystone", "Keystone"),
    ),
    feature_axes={
        "stack": ("2x75kg", "2x100kg", "digital 0-90kg"),
        "attachments": ("standard", "pro", "studio"),
        "footprint": ("wall mount", "free standing"),
    },
    parts=(
        PartTemplate("cable-assembly", "Steel Cable Assembly", "mechanical", 12900,
                     ("cable frayed", "cable jumping", "cable noisy"),
                     customer_replaceable=False, safety_class="restricted"),
        PartTemplate("pulley", "Pulley Wheel", "mechanical", 3900,
                     ("pulley noisy", "cable jumping", "grinding")),
        PartTemplate("weight-pin", "Weight Selector Pin", "consumable", 1900,
                     ("pin wont insert", "pin sticks", "weight wont select")),
        PartTemplate("handle-set", "Handle Attachment Set", "consumable", 6900,
                     ("handle worn", "clip broken")),
        PartTemplate("digital-actuator", "Digital Resistance Actuator", "electronics", 39900,
                     ("no resistance", "resistance jumps", "error c1"),
                     customer_replaceable=False, safety_class="restricted"),
        PartTemplate("controller", "System Controller", "electronics", 24900,
                     ("wont power on", "app wont connect", "error c4"),
                     customer_replaceable=False, safety_class="restricted"),
        PartTemplate("guide-rod", "Weight Stack Guide Rod", "mechanical", 8900,
                     ("stack noisy", "stack binds", "rough travel")),
    ),
    error_codes=(
        ErrorCodeTemplate("C1", "Actuator position fault",
                          "The digital resistance actuator reported a position outside its "
                          "calibrated range.",
                          "Remove all load from the cables and power cycle. Run the calibration "
                          "routine with the handles docked.",
                          ("digital-actuator",)),
        ErrorCodeTemplate("C4", "Controller self-test failed",
                          "The controller failed its power-on self test.",
                          "Power cycle at the wall. If the code persists the controller must be "
                          "replaced by a technician.",
                          ("controller",)),
    ),
    faults=(
        FaultTemplate(
            symptom="cable is frayed or has visible broken strands",
            aliases=("frayed cable", "broken strand", "cable damaged", "wire sticking out"),
            steps=(
                "STOP USING THE MACHINE IMMEDIATELY.",
                "Do not attempt to replace the cable yourself.",
            ),
            resolves_without_part=False,
            likely_part_slugs=("cable-assembly",),
            safety_note="A frayed cable under a loaded weight stack can fail catastrophically. "
                        "This is a technician-only repair and the machine must be taken out of "
                        "service until it is done.",
        ),
        FaultTemplate(
            symptom="weight stack is noisy or binds during travel",
            aliases=("stack noisy", "stack sticks", "rough travel", "clunking stack"),
            steps=(
                "With no weight selected, move the top plate by hand through its full travel and "
                "note where it binds.",
                "Wipe both guide rods with a clean dry cloth, then apply a light silicone spray — "
                "never oil or grease, which collects grit.",
                "Check that the machine is level; a twisted frame binds the stack.",
                "Inspect the guide rods for scoring at the point where the binding occurs.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("guide-rod",),
        ),
        FaultTemplate(
            symptom="weight selector pin will not insert",
            aliases=("pin wont go in", "pin stuck", "cant select weight"),
            steps=(
                "Confirm the stack is fully at rest at the bottom of its travel.",
                "Check the pin itself for a bend by rolling it on a flat surface.",
                "Look into the plate hole for debris or a burr.",
                "If the holes look misaligned, the stack is not seated — lift the top plate "
                "slightly and let it settle.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("weight-pin",),
        ),
    ),
)


# ---------------------------------------------------------------------------
# ELLIPTICALS
# ---------------------------------------------------------------------------

ELLIPTICAL = CategoryTemplate(
    id="elliptical",
    name="Elliptical",
    safety_class="standard",
    serial_letter="E",
    families=(
        ("glide", "Glide"),
        ("horizon", "Horizon"),
        ("stride", "Stride"),
    ),
    feature_axes={
        "stride": ("18in", "20in", "22in adjustable"),
        "console": ("mono LCD", "7in colour", "10in touch"),
        "drive": ("rear drive", "front drive", "centre drive"),
    },
    parts=(
        PartTemplate("pedal-arm", "Pedal Arm", "mechanical", 9900,
                     ("pedal wobble", "clunking", "pedal loose")),
        PartTemplate("roller-wheel", "Roller Wheel Set", "consumable", 4900,
                     ("rumbling", "flat spot", "noisy ramp")),
        PartTemplate("console-ell", "Console Assembly", "electronics", 16900,
                     ("blank display", "console unresponsive")),
        PartTemplate("resistance-motor-ell", "Resistance Motor", "electronics", 13900,
                     ("resistance stuck", "no resistance change")),
        PartTemplate("crank-bearing", "Crank Bearing Set", "mechanical", 7900,
                     ("creaking", "knocking", "play in pedals")),
        PartTemplate("ramp", "Incline Ramp", "mechanical", 18900,
                     ("ramp worn", "noisy ramp", "uneven stride")),
    ),
    error_codes=(
        ErrorCodeTemplate("L2", "Resistance motor not homing",
                          "The resistance motor did not find its home position.",
                          "Unplug for 60 seconds; the motor homes on the next power-up. If it "
                          "fails twice, the motor or its position sensor has failed.",
                          ("resistance-motor-ell",)),
        ErrorCodeTemplate("L4", "No RPM signal",
                          "The console is not receiving a speed pulse.",
                          "Check the reed sensor gap at the flywheel and the cable run up the "
                          "console mast.",
                          ("console-ell",)),
    ),
    faults=(
        FaultTemplate(
            symptom="clunking or knocking through the pedals",
            aliases=("clunk", "knocking", "pedal noise", "play in pedals"),
            steps=(
                "Stand still on the pedals and rock side to side — note whether the play is at "
                "the pedal arm or at the crank.",
                "Check the pedal arm bolts; these loosen in the first month of use and are the "
                "most common cause.",
                "Torque the crank bolts to 40 Nm.",
                "If play remains at the crank after torquing, the crank bearings have worn.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("pedal-arm", "crank-bearing"),
        ),
        FaultTemplate(
            symptom="rumbling noise from the rear ramp",
            aliases=("rumbling", "roller noise", "ramp noise"),
            steps=(
                "Inspect each roller wheel for a flat spot by spinning it against the ramp.",
                "Wipe the ramp surface clean; grit embedded in the ramp destroys rollers.",
                "Check whether the noise changes with resistance level — if it does not, it is "
                "purely mechanical.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("roller-wheel", "ramp"),
        ),
    ),
)


# ---------------------------------------------------------------------------
# SMART MIRRORS / STUDIO
# ---------------------------------------------------------------------------

MIRROR = CategoryTemplate(
    id="mirror",
    name="Studio Mirror",
    safety_class="standard",
    serial_letter="M",
    families=(
        ("reflect", "Reflect"),
        ("studio", "Studio"),
    ),
    feature_axes={
        "size": ("43in", "50in", "55in"),
        "mount": ("floor stand", "wall mount"),
        "camera": ("none", "1080p", "4K"),
    },
    parts=(
        PartTemplate("panel", "Display Panel", "electronics", 89900,
                     ("cracked screen", "dead pixels", "no image"),
                     customer_replaceable=False, safety_class="restricted"),
        PartTemplate("mainboard", "Main Board", "electronics", 29900,
                     ("wont boot", "no wifi", "error m1")),
        PartTemplate("psu-mirror", "Power Supply", "electronics", 5900,
                     ("no power", "clicking then off")),
        PartTemplate("camera-module", "Camera Module", "electronics", 8900,
                     ("camera not detected", "black camera feed")),
        PartTemplate("wall-bracket", "Wall Mount Bracket", "frame", 7900,
                     ("bracket bent", "mirror leaning")),
        PartTemplate("speaker-pair", "Speaker Pair", "electronics", 6900,
                     ("no sound", "distorted audio", "one speaker dead")),
    ),
    error_codes=(
        ErrorCodeTemplate("M1", "Boot partition failure",
                          "The device failed to mount its system partition.",
                          "Hold the power button for 30 seconds to force recovery mode, then "
                          "select 'Restore'. This preserves workout history.",
                          ("mainboard",)),
        ErrorCodeTemplate("M3", "Network unreachable",
                          "The mirror cannot reach the content service.",
                          "Confirm the router is on 2.4 GHz or dual-band; re-enter the Wi-Fi "
                          "password from Settings > Network.",
                          ()),
    ),
    faults=(
        FaultTemplate(
            symptom="mirror will not power on",
            aliases=("no power", "wont turn on", "black screen", "dead"),
            steps=(
                "Confirm the power brick LED is lit.",
                "Check the barrel connector at the back of the mirror is fully seated — it backs "
                "out slightly if the unit has been moved.",
                "Try the power brick in a different outlet.",
                "If you hear a repeated click from the brick, the supply is going into protection "
                "and needs replacing.",
            ),
            resolves_without_part=True,
            likely_part_slugs=("psu-mirror", "mainboard"),
        ),
        FaultTemplate(
            symptom="classes buffer or will not load",
            aliases=("buffering", "wont stream", "class wont start", "spinning"),
            steps=(
                "Run the built-in network test from Settings > Network > Diagnostics.",
                "Confirm the measured downlink is above 15 Mbps at the mirror's location.",
                "Move the router or add a mesh node if signal strength is below two bars.",
                "Reboot the mirror after any network change.",
            ),
            resolves_without_part=True,
            likely_part_slugs=(),
        ),
    ),
)


CATEGORIES: tuple[CategoryTemplate, ...] = (
    TREADMILL, BIKE, ROWER, CABLE, ELLIPTICAL, MIRROR,
)

CATEGORY_BY_ID = {c.id: c for c in CATEGORIES}

# Warranty profiles by category. The policy engine reads the per-model row that
# gets written from these; they are not hardcoded anywhere in the agent.
WARRANTY_PROFILES = {
    "treadmill":  dict(frame=120, parts=36, electronics=24, labor=12, consumables=False),
    "bike":       dict(frame=120, parts=36, electronics=24, labor=12, consumables=False),
    "rower":      dict(frame=60,  parts=24, electronics=24, labor=12, consumables=False),
    "cable":      dict(frame=120, parts=60, electronics=36, labor=24, consumables=False),
    "elliptical": dict(frame=120, parts=36, electronics=24, labor=12, consumables=False),
    "mirror":     dict(frame=36,  parts=24, electronics=24, labor=12, consumables=False),
}

# Words that force an immediate safety escalation regardless of what the model
# thinks. Deliberately checked in plain Python before the LLM ever runs.
SAFETY_KEYWORDS = (
    "smoke", "smoking", "burning smell", "burnt", "fire", "sparks", "sparking",
    "electric shock", "shocked me", "shock", "frayed cable", "broken strand",
    "injured", "injury", "bleeding", "hurt myself", "fell off", "trapped",
    "child", "toddler", "smells like burning", "melting", "hot to touch",
)
