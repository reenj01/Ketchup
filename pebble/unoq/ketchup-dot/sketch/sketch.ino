// Ketchup Dot, UNO Q microcontroller side: pins only. Linux (pebble/unoq/unoq_dot.py) does
// audio, network and the session logic, and talks to this sketch over the Bridge.
//
//   Hall KY-003  D2  INPUT_PULLUP, LOW = magnet (a tap)
//   FSR          D4  to GND, INPUT_PULLUP, LOW = pressed
//   RGB LED      D3 red, D5 green, D6 blue (common cathode, HIGH = on, 100 ohm each)
//
// Bridge functions (Linux calls them with Bridge.call):
//   set_led(r, g, b)  0..255 each, PWM (D3/D5/D6 are PWM pins). Solid colour; stops a breath.
//   breathe(ms)       white breathing while the agents talk: one breath every ms (1 -> 0.1 -> 1,
//                     a cosine, the same curve pi_pebble.breath_level uses), run here in loop()
//                     so it stays smooth without Bridge traffic. Ends on the next set_led.
// LED meaning (set by Linux): solid white = idle / a time is ready, white breathing = agents
// talking, yellow = FSR held (owner talking), green = booked (60 s, then white).
//   get_inputs()      -> taps * 2 + pressed. taps = Hall taps counted since boot (debounced, never
//                        missed between polls); pressed = 1 while the FSR is held (debounced)
// Both use provide_safe: they run in loop() context, so analogWrite/digitalRead are safe there.
// Docs: https://docs.arduino.cc/software/app-lab/bridge/bridge-api/

#include "Arduino_RouterBridge.h"

const int HALL = 2, FSR = 4, RED = 3, GREEN = 5, BLUE = 6;
const unsigned long HALL_DEBOUNCE_MS = 50, FSR_DEBOUNCE_MS = 20;

struct Debounced {
  int pin;
  unsigned long ms;     // how long a new level must hold before it counts
  bool raw, stable;     // last read level, last accepted level (true = pin LOW = active)
  unsigned long since;  // when raw last changed
};

Debounced hall = {HALL, HALL_DEBOUNCE_MS, false, false, 0};
Debounced fsr = {FSR, FSR_DEBOUNCE_MS, false, false, 0};
volatile int taps = 0;
unsigned long breath_ms = 0, breath_t0 = 0, breath_last = 0;  // 0 = not breathing

// true when the stable (debounced) state just became "active" (pin LOW)
bool update(Debounced &d) {
  bool active = digitalRead(d.pin) == LOW;
  unsigned long t = millis();
  if (active != d.raw) {
    d.raw = active;
    d.since = t;
  }
  if (d.raw != d.stable && t - d.since >= d.ms) {
    d.stable = d.raw;
    return d.stable;
  }
  return false;
}

void write_rgb(int r, int g, int b) {
  analogWrite(RED, constrain(r, 0, 255));
  analogWrite(GREEN, constrain(g, 0, 255));
  analogWrite(BLUE, constrain(b, 0, 255));
}

void set_led(int r, int g, int b) {
  breath_ms = 0;
  write_rgb(r, g, b);
}

void breathe(int ms) {
  breath_ms = ms < 500 ? 500 : ms;
  breath_t0 = millis();
  breath_last = 0;
}

void breath_step() {  // ~50 steps a second
  unsigned long t = millis();
  if (!breath_ms || t - breath_last < 20) return;
  breath_last = t;
  float x = (float)((t - breath_t0) % breath_ms) / breath_ms;
  int v = (int)(255 * (0.1f + 0.9f * (0.5f + 0.5f * cosf(2 * PI * x))));
  write_rgb(v, v, v);
}

int get_inputs() {
  return taps * 2 + (fsr.stable ? 1 : 0);
}

void setup() {
  pinMode(HALL, INPUT_PULLUP);
  pinMode(FSR, INPUT_PULLUP);
  pinMode(RED, OUTPUT);
  pinMode(GREEN, OUTPUT);
  pinMode(BLUE, OUTPUT);
  set_led(0, 0, 0);  // dark until Linux says white (so a dark Dot = Linux side not running)
  Bridge.begin();
  Bridge.provide_safe("set_led", set_led);
  Bridge.provide_safe("get_inputs", get_inputs);
  Bridge.provide_safe("breathe", breathe);
}

void loop() {
  if (update(hall)) taps = (taps + 1) % 1000000;  // falling edge = magnet arrived
  update(fsr);
  breath_step();
  delay(1);  // short: provide_safe handlers are served between loop() runs
}
