import { useEffect, useMemo, useRef, type CSSProperties, type MouseEvent } from "react";
import type { ClosedData, ClosedDay, ClosedPick } from "../types";
import { loadClosed, loadIndex, loadOpen, useAsync } from "../lib/data";
import { fmtDay, fmtInt, fmtUSD } from "../lib/format";
import { href, setQuery, useQuery } from "../lib/router";
import { ErrorNote, Loading, Select } from "../components/ui";
import { OpenNow, isOpenKey } from "./watchlist/OpenNow";
import { Sheet } from "./watchlist/Sheet";
import { Chevron, DAY_MS, MoreButton, NewsList, colsFor, dateOnly, dayOf, ms, plural } from "./watchlist/shared";
import "./watchlist.css";

// The watchlist. On top, "Open now" (watchlist/OpenNow.tsx): open markets
// where a few people will know the answer first and the news shows them
// moving toward it. Below, the recently closed ones (watchlist/upsets.py):
// one day at a time, that day's 3-5 markets worth a look as possible insider
// cases, each a card under a rule in its verdict's colour. "Details" on a
// card in either section opens the rest in a side sheet (watchlist/Sheet.tsx);
// a market a person likes can go to the main page (once the database
// exists). Older days are a pick away.
// `?day=` and `?open=` hold the state (one card open on the whole page). All times are UTC.

const shortId = (p: ClosedPick) => p.condition_id.slice(0, 12);
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
const dayShort = (day: string) =>
  new Date(`${day}T00:00:00Z`).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });

/** The group's title, when it says something the question doesn't (ladder placeholders "… by...?", "by ___?" never do). */
const groupOf = (p: ClosedPick) =>
  p.event_title && p.event_title !== p.question && !/\.\.\.|…|___/.test(p.event_title) ? p.event_title : null;

/**
 * The model's summaries end with the report's clock time ("… on November 11,
 * 2026 at 03:17:06Z", "…, as first reported at 03:17 UTC", "(WSJ,
 * 2026-10-04 07:00:00Z)", "on 2026-10-29T07:00:00Z"). The cards show no clock
 * times, and it is often the made-up midnight above, so drop it: a bracketed
 * stamp goes, a bare one becomes its date. (The prompt in judge.py should stop asking.)
 */
const CLOCK = /,?\s+(?:(?:as\s+)?(?:first\s+)?(?:reported|published)(?:\s+in\s+headlines(?:\s+published)?)?\s+)?at\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:Z|UTC)\b/g;
const STAMP_IN_BRACKETS = /\s*\((?:[^()]*?,\s*)?\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?(?:\.\d+)?(?:Z|\+00:00)?\)/g;
const STAMP = /\b(\d{4}-\d\d-\d\d)[T ]\d\d:\d\d(?::\d\d)?(?:\.\d+)?(?:Z|\+00:00)?/g;
const longDate = (day: string) =>
  new Date(`${day}T00:00:00Z`).toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });
const clean = (s: string) =>
  s.replace(STAMP_IN_BRACKETS, "").replace(CLOCK, "").replace(STAMP, (_, day: string) => longDate(day));

/**
 * When the news broke against the close, in words: "news broke 17 h before
 * the market closed", "3 days before", "the same hour". A report dated but
 * not timed is counted in days. Null with no first report.
 */
function broke(p: ClosedPick): string | null {
  if (!p.public_at) return null;
  const tail = "the market closed";
  if (dateOnly(p.public_at)) {
    const d = Math.round((dayOf(ms(p.closed)) - dayOf(ms(p.public_at))) / DAY_MS);
    if (d === 0) return `news broke the same day ${tail}`;
    if (Math.abs(d) === 1) return `news broke the day ${d > 0 ? "before" : "after"} ${tail}`;
    return `news broke ${plural(Math.abs(d), "day")} ${d > 0 ? "before" : "after"} ${tail}`;
  }
  const diff = ms(p.closed) - ms(p.public_at);
  const h = Math.abs(diff) / 36e5;
  const side = diff > 0 ? "before" : "after";
  if (h < 1) return `news broke the same hour ${tail}`;
  if (h < 47.5) return `news broke ${fmtInt(Math.round(h))}\u00a0h ${side} ${tail}`;
  return `news broke ${plural(Math.round(h / 24), "day")} ${side} ${tail}`;
}

const VERDICTS: ClosedPick["verdict"][] = ["potential case", "weak", "not a case"];
const VERDICT: Record<ClosedPick["verdict"], [one: string, many: string]> = {
  "potential case": ["Potential case", "potential cases"],
  weak: ["Weaker case", "weaker cases"],
  "not a case": ["Unlikely case", "unlikely cases"],
};

type CheckState = "met" | "part" | "unknown" | "no";
const STATE_WORD: Record<CheckState, string> = { met: "Yes", part: "Partly", unknown: "Can't tell", no: "No" };

/** The three things the check asks, each yes / partly / can't tell / no. */
function checks(p: ClosedPick): { state: CheckState; short: string; long: string; detail: string }[] {
  const who = p.who_knew_first.trim().toLowerCase();
  const nobody = who === "" || who === "nobody";
  const unsure = who === "unknown"; // the prompt's word where the news can't tell (judge.py reads it the same way)
  const surprise: Record<ClosedPick["surprise"], [CheckState, string]> = {
    yes: ["met", "Few expected it"],
    somewhat: ["part", "Real doubt either way"],
    unknown: ["unknown", "The news doesn't say"],
    no: ["no", "It was expected"],
  };
  const quiet: Record<ClosedPick["public_signals"], [CheckState, string]> = {
    none: ["met", "No public signs found"],
    some: ["part", "Some signs beforehand"],
    unknown: ["unknown", "The news doesn't say"],
    clear: ["no", "Clear signs beforehand"],
  };
  return [
    {
      state: nobody ? "no" : unsure ? "unknown" : "met",
      short: "Few knew first",
      long: "A small group could know first",
      detail: nobody ? "Nobody" : unsure ? "The news doesn't say" : cap(p.who_knew_first.trim()),
    },
    { state: surprise[p.surprise][0], short: "Surprise", long: "The outcome was a surprise", detail: surprise[p.surprise][1] },
    { state: quiet[p.public_signals][0], short: "Kept quiet", long: "Kept quiet until it came out", detail: quiet[p.public_signals][1] },
  ];
}

/** The first report: the headline the check named, if it is one of them. */
const firstReport = (p: ClosedPick) => (p.public_at ? p.headlines.find((h) => h.published === p.public_at) ?? null : null);

/** The three checks on the card face: each name with its answer in words. */
function Checks({ p }: { p: ClosedPick }) {
  return (
    <dl className="cl-checks">
      {checks(p).map((c) => (
        <div key={c.short} data-state={c.state} title={`${c.long}: ${STATE_WORD[c.state]} · ${c.detail}`}>
          <dt>{c.short}</dt>
          <dd>
            <span className="cl-glyph" aria-hidden />
            {STATE_WORD[c.state]}
          </dd>
        </div>
      ))}
    </dl>
  );
}

const moreId = (p: ClosedPick) => `cl-more-${shortId(p)}`;

/**
 * A closed card, top to bottom: the verdict in its colour (the card's top
 * rule is the same colour), the question, how it resolved and when the news
 * broke against the close, what happened (two lines), the three checks with
 * their marks in the verdict's colour, "Details".
 */
function Card({ p, open }: { p: ClosedPick; open: boolean }) {
  const show = () => setQuery({ open: shortId(p) });
  const when = broke(p);
  return (
    <article
      className="card cl-card"
      data-verdict={p.verdict}
      // opens from anywhere on it, unless the click ended a text selection
      onClick={() => !window.getSelection()?.toString() && show()}
    >
      <p className="cl-verdict label">{VERDICT[p.verdict][0]}</p>
      <h3 className="cl-card-title">{p.question}</h3>
      <p className="cl-outcome">
        Resolved <b>{p.winner}</b>
        {when && <> · {when}</>}
      </p>
      <p className="cl-what">{clean(p.what_happened)}</p>
      {/* the wrapper is the size container (on the card it would switch the subgrid off) */}
      <div className="cl-checks-box">
        <Checks p={p} />
      </div>
      <MoreButton id={moreId(p)} open={open} onOpen={show} />
    </article>
  );
}

const Plus = () => (
  <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden>
    <path d="M6 2v8M2 6h8" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
  </svg>
);

/** The opening words of the note's "why", naming the verdict. */
const RUN_IN: Record<ClosedPick["verdict"], string> = {
  "potential case": "Why it could be a case.",
  weak: "Why it's a weaker case.",
  "not a case": "Why it's unlikely.",
};

/**
 * The closed card's sheet: what happened, large; why (its opening words
 * naming the verdict); the three checks with their answers and reasons; the
 * open list's "who would know first" when the market was on it; the markets
 * the same news settled; the headlines as a plain list, newest last, the
 * first report marked in the verdict's colour; then the actions.
 */
function Details({ p, onMain, onClose }: { p: ClosedPick; onMain: string | null; onClose: () => void }) {
  const id = shortId(p);
  const group = groupOf(p);
  const first = firstReport(p);
  const all = checks(p);
  const yes = all.filter((c) => c.state === "met").length;
  return (
    <Sheet label={p.question} returnTo={moreId(p)} onClose={onClose} verdict={p.verdict}>
      <section className="wl-sheet-sec">
        <h4 className="visually-hidden">What happened</h4>
        <p className="wl-sheet-lead wl-sheet-first">{clean(p.what_happened)}</p>
        {group && (
          <p className="cl-event">
            Part of <span>{group}</span>
          </p>
        )}
        <p className="wl-sheet-text">
          <b>{RUN_IN[p.verdict]}</b> {clean(p.reason)}
        </p>
      </section>

      {/* the three checks: a mark, the check and its answer, what the news said under them */}
      <section className="wl-sheet-sec" aria-labelledby={`cl-checks-${id}`}>
        <h4 className="cl-h" id={`cl-checks-${id}`}>
          The three checks{" "}
          <span className="cl-h-aside tabular">
            <span aria-hidden="true">· </span>yes on {fmtInt(yes)} of 3
          </span>
        </h4>
        <ul className="cl-ledger">
          {all.map((c) => (
            <li key={c.short} data-state={c.state}>
              <span className="cl-glyph" aria-hidden />
              <b title={c.long}>{c.short}</b>
              <span className="cl-ledger-ans">
                <span className="visually-hidden">: </span>
                {STATE_WORD[c.state]}
              </span>
              <span className="cl-ledger-why">{c.detail}</span>
            </li>
          ))}
        </ul>
      </section>

      {(p.who_knows || p.on_open_list_from) && (
        <section className="wl-sheet-sec">
          {p.who_knows && <p className="wl-sheet-text">{p.who_knows}</p>}
          {p.on_open_list_from && (
            <p className="cl-onlist">On the open list from {fmtDay(new Date(`${p.on_open_list_from}T00:00:00Z`))}</p>
          )}
        </section>
      )}

      {p.same_event.length > 0 && (
        <section className="wl-sheet-sec" aria-labelledby={`cl-same-${id}`}>
          <h4 className="cl-h" id={`cl-same-${id}`}>
            The same news settled {plural(p.same_event.length, "other market")}
          </h4>
          <ul className="wl-plain">
            {p.same_event.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </section>
      )}

      <section className="wl-sheet-sec" aria-labelledby={`cl-news-${id}`}>
        <h4 className="cl-h" id={`cl-news-${id}`}>
          {p.headlines.length ? `${plural(p.headlines.length, "headline")} checked` : "Headlines checked"}
        </h4>
        {p.headlines.length ? (
          <NewsList items={p.headlines} mark={(h) => (h === first ? "First report" : null)} />
        ) : (
          <p className="cl-empty">None found.</p>
        )}
      </section>

      <div className="wl-sheet-foot">
        {onMain ? (
          <a className="btn btn-solid btn-sm" href={href.market(onMain)}>
            On the main page →
          </a>
        ) : (
          <span className="cl-add">
            {/* aria-disabled, not disabled: it stays reachable so its note is read out */}
            <button
              type="button"
              className="cl-link cl-add-btn"
              aria-disabled="true"
              aria-describedby={`cl-note-${id}`}
              onClick={(e) => e.preventDefault()}
            >
              <Plus /> Add to main page
            </button>
            <span aria-hidden="true">·</span>
            <span className="cl-note" id={`cl-note-${id}`}>
              Not available yet
            </span>
          </span>
        )}
        <a className="cl-link cl-ext" href={p.url} target="_blank" rel="noopener noreferrer">
          Polymarket <span aria-hidden>↗</span>
        </a>
      </div>
    </Sheet>
  );
}

function DayPicker({ days, day, partial }: { days: ClosedDay[]; day: ClosedDay; partial: string }) {
  const i = days.indexOf(day);
  // a new day is a new history step, so Back returns to the day before; it
  // closes this section's open card, never the open section's
  const keep = isOpenKey(useQuery().open);
  const go = (d: ClosedDay | undefined) => d && setQuery({ day: d.day, ...(keep ? {} : { open: null }) }, { push: true });
  // an arrow that steps to the first or last day disables itself, which would drop the focus to the page:
  // from the keyboard (a click with no pointer, detail 0) it moves to the other arrow instead
  const before = useRef<HTMLButtonElement>(null);
  const after = useRef<HTMLButtonElement>(null);
  const stepped = useRef<"before" | "after" | null>(null);
  useEffect(() => {
    const s = stepped.current;
    stepped.current = null;
    if (s === "before" && i >= days.length - 1) after.current?.focus();
    if (s === "after" && i <= 0) before.current?.focus();
  }, [i, days.length]);
  const step = (s: "before" | "after", e: MouseEvent<HTMLButtonElement>) => {
    stepped.current = e.detail === 0 ? s : null;
    go(days[s === "before" ? i + 1 : i - 1]);
  };
  return (
    <div className="cl-daypick" role="group" aria-label="Day (UTC)">
      <button
        type="button"
        ref={before}
        className="cl-step"
        onClick={(e) => step("before", e)}
        disabled={i >= days.length - 1}
        aria-label="Day before"
      >
        <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden>
          <path d="M7.5 3 4.5 6l3 3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      <Select
        value={day.day}
        label="Day (UTC)"
        options={days.map((d) => {
          const so = d.day === partial ? ", so far" : "";
          // the chosen day shows just its date (the header has its counts); the others say if they hold a potential case
          if (d === day) return { value: d.day, label: `${dayShort(d.day)} (UTC${so})` };
          const n = d.picks.filter((p) => p.verdict === "potential case").length;
          return { value: d.day, label: `${dayShort(d.day)}${so ? " (so far)" : ""}${n ? ` · ${plural(n, "potential case")}` : ""}` };
        })}
        onChange={(v) => go(days.find((d) => d.day === v))}
      />
      <button type="button" ref={after} className="cl-step" onClick={(e) => step("after", e)} disabled={i <= 0} aria-label="Day after">
        <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden>
          <path d="M4.5 3 7.5 6l-3 3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
    </div>
  );
}

/** What the filters took out, in words that read after a count. */
const DROPPED: Record<string, [one: string, many: string]> = {
  "game or sport": ["game or sport", "games or sports"],
  "price market": ["price market", "price markets"],
  "election or vote": ["election or vote", "elections or votes"],
  weather: ["weather market", "weather markets"],
  "nothing happened by the deadline": ["where nothing happened by the deadline", "where nothing happened by the deadline"],
  "no single winner": ["with no single winner", "with no single winner"],
  "later date of the same event": ["later date of an event already in", "later dates of an event already in"],
  "same news as another market": ["settled by the same news as another", "settled by the same news as another"],
  "counted or charted in public (views, box office, charts, ...)": ["counted or ranked in public", "counted or ranked in public"],
  "words said in public (mention market)": ["mention market (words said in public)", "mention markets (words said in public)"],
  "embargoed release (official statistic, rate decision, company results or share offering)": [
    "release kept secret until a set time",
    "releases kept secret until a set time",
  ],
};

function HowPicked({ data, day, today }: { data: ClosedData; day: ClosedDay; today: boolean }) {
  const dropped = Object.entries(day.dropped ?? {});
  // what is left once the kinds below are taken out ("could not be checked" is one of them): the markets checked
  const left = day.checked;
  return (
    <details className="cl-more cl-dayinfo">
      <summary>
        How these were picked
        <Chevron />
      </summary>
      <p>
        {plural(day.closed_markets, "market")} closed on this day{today ? " so far" : ""} (a UTC day; markets with less than{" "}
        {fmtUSD(data.rules.min_volume)} traded aren't counted). {fmtInt(left)} {left === 1 ? "was" : "were"} left after taking
        out the kinds below. Official statistics, central bank decisions, scheduled company results and share-offering prices
        are always left out: they come out at a set time under rules built to keep them secret until then. A model read each
        of those with the news about it and answered three questions: could a small group know first, was the outcome a
        surprise, and was it kept quiet until it came out. It sees no prices or trading data. Every potential or weaker case
        is shown (up to {fmtInt(data.rules.per_day)}), topped up to {fmtInt(data.rules.at_least)} with the closest of the rest
        when the day has that many. A market that was on the Open now list is always shown, whatever its verdict.
      </p>
      {dropped.length > 0 && (
        <ul>
          {dropped.map(([why, n]) => {
            const [one, many] = DROPPED[why] ?? [why, why];
            return (
              <li key={why}>
                <span className="tabular">{fmtInt(n)}</span> {n === 1 ? one : many}
              </li>
            );
          })}
        </ul>
      )}
      <p className="cl-glyph-key">
        Check answers:{" "}
        {(Object.keys(STATE_WORD) as CheckState[]).map((s) => (
          <span key={s} data-state={s}>
            <span className="cl-glyph" aria-hidden />
            {STATE_WORD[s]}
          </span>
        ))}
      </p>
    </details>
  );
}

const ClockIcon = () => (
  <svg className="wl-sec-icon" width="16" height="16" viewBox="0 0 16 16" aria-hidden>
    <circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" strokeWidth="1.4" />
    <path d="M8 4.6V8l2.3 1.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

const ClosedTitle = () => (
  <h2 className="wl-sec-title" id="wl-closed-title">
    <ClockIcon />
    Recently closed
  </h2>
);

/**
 * The day shown: `?day=`; else the day of the closed card `?open=` names (a
 * shared link made before a newer day came in still opens its card); else by
 * default the newest day, unless that is today's partial day with fewer
 * picks than a full one: then the day before (today is one step away).
 */
function chosenDay(data: ClosedData, day: string | undefined, open?: string): ClosedDay | undefined {
  const fallback =
    data.days.find((d) => !(d.day === data.partial_day && d.picks.length < data.rules.at_least)) ?? data.days[0];
  return (
    data.days.find((d) => d.day === day) ??
    (open ? data.days.find((d) => d.picks.some((p) => shortId(p) === open)) : undefined) ??
    fallback
  );
}

function ClosedSection({ data, mainSlugs }: { data: ClosedData; mainSlugs: Map<string, string> }) {
  const query = useQuery();
  const day = chosenDay(data, query.day, query.open);
  if (!day)
    return (
      <>
        <ClosedTitle />
        <p className="cl-empty">Nothing checked yet.</p>
      </>
    );
  const today = day.day === data.partial_day;
  const n = day.picks.length;
  const many = n >= 4;
  const sheet = day.picks.find((p) => query.open === shortId(p));
  // a card opened from a link that names no day, on a day other than the default: closing it stays on its day
  const keepDay = sheet && !data.days.some((d) => d.day === query.day) && day !== chosenDay(data, undefined) ? day.day : null;
  const closeSheet = () => setQuery({ open: null, ...(keepDay ? { day: keepDay } : {}) });
  return (
    <>
      <header className="cl-top">
        <div className="cl-heading">
          <ClosedTitle />
          <ul className="meta-line label cl-meta tabular">
            {VERDICTS.map((v) => {
              const k = day.picks.filter((p) => p.verdict === v).length;
              return k ? (
                <li key={v}>
                  <b>{fmtInt(k)}</b> {k === 1 ? VERDICT[v][0].toLowerCase() : VERDICT[v][1]}
                </li>
              ) : null;
            })}
            <li>
              <b>{fmtInt(day.closed_markets)}</b> markets closed{today ? " so far" : ""}
            </li>
            <li>Times UTC</li>
          </ul>
        </div>
        <DayPicker days={data.days} day={day} partial={data.partial_day} />
      </header>

      {n ? (
        <div className="cl-cards" data-many={many || undefined} style={{ "--cols": colsFor(n) } as CSSProperties}>
          {day.picks.map((p) => (
            <Card key={p.condition_id} p={p} open={query.open === shortId(p)} />
          ))}
        </div>
      ) : (
        <p className="cl-empty">
          {day.candidates > 0 ? "The markets left on this day could not be checked yet." : "Nothing left to check on this day."}
        </p>
      )}

      <HowPicked data={data} day={day} today={today} />
      {sheet && (
        <Details key={sheet.condition_id} p={sheet} onMain={mainSlugs.get(sheet.slug) ?? null} onClose={closeSheet} />
      )}
    </>
  );
}

export function WatchlistPage() {
  const data = useAsync(loadClosed, "closed");
  const open = useAsync(loadOpen, "open");
  const index = useAsync(loadIndex, "index");
  // markets already on the main page, by slug (markets.json keeps the slug)
  const mainSlugs = useMemo(
    () => new Map((index.data?.markets ?? []).map((m) => [m.slug.split("/").pop()!.split("#")[0], m.name] as const)),
    [index.data],
  );
  // with no open list, its placeholder's ghosts take the closed cards' columns, so the two rows line up
  const query = useQuery();
  const cols = colsFor((data.data ? chosenDay(data.data, query.day, query.open)?.picks.length : 0) ?? 0);
  return (
    <div className="container wl-page">
      <h1 className="wl-title">Watchlist</h1>
      <OpenNow data={open.status === "ready" ? open.data : undefined} cols={cols} />
      <section className="wl-closed" aria-labelledby="wl-closed-title">
        {data.status !== "ready" && <ClosedTitle />}
        {data.status === "loading" && <Loading />}
        {data.status === "error" && <ErrorNote error={data.error} />}
        {data.status === "ready" && <ClosedSection data={data.data} mainSlugs={mainSlugs} />}
      </section>
    </div>
  );
}
