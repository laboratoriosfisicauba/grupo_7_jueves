#!/usr/bin/env python3
import sys
import re
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import Button

def parse_events(path):
    events = []
    current = {"t": [], "u1": [], "u2": [], "u3": []}
    in_block = False

    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()

            # New event header
            if line.startswith("Event #"):
                # store previous if it has data
                if in_block and current["t"]:
                    events.append({k: np.array(v, dtype=float) for k, v in current.items()})
                # start new
                current = {"t": [], "u1": [], "u2": [], "u3": []}
                in_block = True
                continue

            # Skip empty lines and the header row with units
            if not line or line.startswith("t["):
                continue

            # Expect 4 numeric columns: t  u1  u2  u3 (whitespace separated)
            # Be tolerant to extra spaces/brackets in header elsewhere.
            parts = line.split()
            if len(parts) >= 4:
                try:
                    t, u1, u2, u3 = map(float, parts[:4])
                    current["t"].append(t)
                    current["u1"].append(u1)
                    current["u2"].append(u2)
                    current["u3"].append(u3)
                except ValueError:
                    # Ignore non-data lines gracefully
                    pass

    # Push last event
    if in_block and current["t"]:
        events.append({k: np.array(v, dtype=float) for k, v in current.items()})

    if not events:
        raise ValueError("No events found in file. Check the format.")
    return events

class EventBrowser:
    def __init__(self, events, filename):
        self.events = events
        self.i = 0
        self.filename = filename

        self.fig, self.ax = plt.subplots()
        self.fig.canvas.manager.set_window_title("Waveform events")
        self.ax.set_xlabel("t [ns]")
        self.ax.set_ylabel("u [mV]")

        # Prepare 3 lines; update their data on navigation
        (self.l1,) = self.ax.plot([], [], label="u1")
        (self.l2,) = self.ax.plot([], [], label="u2")
        (self.l3,) = self.ax.plot([], [], label="u3")
        self.ax.legend(loc="best")

        # Buttons
        btn_ax_prev = self.fig.add_axes([0.12, 0.02, 0.1, 0.05])
        btn_ax_next = self.fig.add_axes([0.24, 0.02, 0.1, 0.05])
        self.btn_prev = Button(btn_ax_prev, "Prev")
        self.btn_next = Button(btn_ax_next, "Next")
        self.btn_prev.on_clicked(lambda event: self.prev())
        self.btn_next.on_clicked(lambda event: self.next())

        # Keybindings
        self.fig.canvas.mpl_connect("key_press_event", self.on_key)

        self.update()

    def on_key(self, ev):
        if ev.key in ("right", " "):
            self.next()
        elif ev.key in ("left", "backspace"):
            self.prev()
        elif ev.key in ("q", "escape"):
            plt.close(self.fig)

    def update(self):
        e = self.events[self.i]
        t = e["t"]; u1 = e["u1"]; u2 = e["u2"]; u3 = e["u3"]

        self.l1.set_data(t, u1)
        self.l2.set_data(t, u2)
        self.l3.set_data(t, u3)

        # Nice autoscale per event
        self.ax.relim()
        self.ax.autoscale_view()

        self.ax.set_title(f"{self.filename} — Event {self.i+1}/{len(self.events)}")
        self.fig.canvas.draw_idle()

    def next(self):
        if self.i < len(self.events) - 1:
            self.i += 1
            self.update()

    def prev(self):
        if self.i > 0:
            self.i -= 1
            self.update()

def main():
    if len(sys.argv) < 2:
        print("Usage: python plot_events.py <data.txt>")
        sys.exit(1)

    filename = sys.argv[1]
    events = parse_events(filename)
    EventBrowser(events, filename)
    plt.show()

if __name__ == "__main__":
    main()
