// Power (L-080 item 3, Erik: "On my phone it eats my battery so try to figure out how to optimize it"). One place that
// says how fast the animations may run: active (touched in the last IDLE_MS), idle, battery saver, or hidden (nothing
// runs). Battery saver switches on by itself on a phone-sized touch screen or when the Battery API says unplugged; the
// Layers list has a toggle that overrides it (remembered). No imports, so the node tests can load it.
export const IDLE_MS = 30000;
/** Frames (wind, river particles) and sprite-clock ticks per second, by state. */
export const RATES = {
  active: { wind: 30, river: 30, clock: 4 },
  idle: { wind: 15, river: 15, clock: 1 },
  saver: { wind: 10, river: 10, clock: 1 },   // 1 Hz, not frozen: a turbine's spin says it's generating (Reviewer, #306)
  hidden: { wind: 0, river: 0, clock: 0 },
};

/** The state from its inputs: hidden wins, then saver, then idle. Pure, for the tests. */
export function powerState({ hidden, saver, sinceInput }) {
  return hidden ? "hidden" : saver ? "saver" : sinceInput > IDLE_MS ? "idle" : "active";
}
/** Saver: the user's choice ("1" / "0") if any, else automatic on small touch screens or on battery. */
export const saverOn = (pref, smallTouch, onBattery) => (pref === "1" ? true : pref === "0" ? false : !!(smallTouch || onBattery));

export class Power {
  constructor(map) {
    this.last = performance.now(); this.fns = new Set(); this.onBattery = false;
    try { this.pref = localStorage.getItem("ms.saver"); } catch { this.pref = null; }
    this.smallTouch = !!globalThis.matchMedia?.("(pointer: coarse) and (max-width: 900px), (pointer: coarse) and (max-height: 500px)").matches;
    const poke = () => this.poke();
    for (const ev of ["pointerdown", "wheel", "keydown", "touchstart"]) addEventListener(ev, poke, { passive: true, capture: true });
    map?.on("movestart", poke);
    document.addEventListener("visibilitychange", () => this.emit());
    navigator.getBattery?.().then((b) => { const f = () => { this.onBattery = !b.charging; this.emit(); }; b.addEventListener("chargingchange", f); f(); })
      .catch(() => {});
    this.was = this.state;
    setInterval(() => { if (this.state !== this.was) this.emit(); }, 2000);   // active → idle
  }
  get saver() { return saverOn(this.pref, this.smallTouch, this.onBattery); }
  get auto() { return this.pref == null; }
  get state() { return powerState({ hidden: document.hidden, saver: this.saver, sinceInput: performance.now() - this.last }); }
  rate(k) { return RATES[this.state][k]; }
  poke() { this.last = performance.now(); if (this.state !== this.was) this.emit(); }
  /** true / false: the user's choice; null: automatic again. */
  setSaver(on) {
    this.pref = on == null ? null : on ? "1" : "0";
    try { if (on == null) localStorage.removeItem("ms.saver"); else localStorage.setItem("ms.saver", this.pref); } catch { /* private mode */ }
    this.emit();
  }
  onChange(fn) { this.fns.add(fn); }
  emit() { this.was = this.state; for (const fn of this.fns) fn(this.was); }
}
