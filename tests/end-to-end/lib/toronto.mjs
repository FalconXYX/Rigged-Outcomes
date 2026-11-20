// Toronto time for the scenarios, as the Worker keeps it (worker/src/contract.ts): the Toronto day and minute of
// epoch seconds, a Toronto wall-clock time on a day as epoch seconds (daylight saving read from the zone), and
// calendar steps between days.

const PARTS = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/Toronto",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/** {day: "YYYY-MM-DD", minutes since Toronto midnight} of epoch seconds. */
export function toronto(epoch) {
  const p = Object.fromEntries(PARTS.formatToParts(new Date(epoch * 1000)).map((x) => [x.type, x.value]));
  return { day: `${p.year}-${p.month}-${p.day}`, minutes: Number(p.hour) * 60 + Number(p.minute) };
}

/** The day n days after a "YYYY-MM-DD" day (n < 0: before). */
export function dayShift(day, n) {
  const d = new Date(`${day}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

export const dayBefore = (day) => dayShift(day, -1);

/** Epoch seconds of "HH:MM" Toronto time on a Toronto day. */
export function torontoAt(day, time) {
  const guess = Date.parse(`${day}T${time}:00Z`) / 1000;
  let t = guess;
  for (let i = 0; i < 2; i++) {
    const p = toronto(t);
    const hh = String(Math.floor(p.minutes / 60)).padStart(2, "0");
    const mm = String(p.minutes % 60).padStart(2, "0");
    t += guess - Date.parse(`${p.day}T${hh}:${mm}:00Z`) / 1000;
  }
  return t;
}
