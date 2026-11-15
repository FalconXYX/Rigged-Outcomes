import { Fragment, useEffect, useMemo, useRef, type CSSProperties, type MouseEvent } from "react";
import type { ClosedData, ClosedDay, ClosedPick } from "../types";
import { loadClosed, loadIndex, loadOpen, useAsync } from "../lib/data";
import { fmtDay, fmtInt, fmtUSD } from "../lib/format";
import { closeSheet, setQuery, useQuery } from "../lib/router";
import { ErrorNote, Loading, Select } from "../components/ui";
import { OpenNow, isOpenKey } from "./watchlist/OpenNow";
import { AddToMain, CardAdd } from "./watchlist/controls";
import {
  Chevron,
  DAY_MS,
  MoreButton,
  NewsList,
  Panel,
  colsFor,
  dateOnly,
  dayOf,
  hhmm,
  ms,
  openPanel,
  panelId,
  plural,
} from "./watchlist/shared";
import "./watchlist.css";

// The watchlist. On top, "Open now" (watchlist/OpenNow.tsx): open markets
// where a few people will know the answer first and the news shows them
// moving toward it. Below, the recently closed ones (watchlist/upsets.py):
// one day at a time, that day's 3-5 markets worth a look as possible insider
// cases, each a card under a rule in its verdict's colour. "Details" on a
// card in either section opens the rest in a panel of its own under the
// card's row (watchlist/shared.tsx), as a history step, so Back closes it. On
// each card, beside "Details", and again in its panel, a visitor can add a
// closed market to the main page, or put an open one on the site's watchlist
// (watchlist/controls.tsx, through the site's API). Older days are a pick away.
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

/** The same, and a bare ISO date ("first reported on 2026-11-11") as the page writes dates in a sentence. */
const BARE_DAY = /\b(\d{4}-\d\d-\d\d)\b/g;
const words = (s: string) => clean(s).replace(BARE_DAY, (_, day: string) => longDate(day));

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

/** a list of the markets counted under one this long or longer starts folded */
const FOLD = 4;

/**
 * A closed card, top to bottom: the verdict in its colour (the card's top
 * rule is the same colour), the question, how it resolved and when the news
 * broke against the close, what happened (two lines), the three checks with
 * their marks in the verdict's colour; at the foot "Details" and "+ Add to
 * main page" (watchlist/controls.tsx).
 */
function Card({ p, open, onMain, onClose }: { p: ClosedPick; open: boolean; onMain: string | null; onClose: () => void }) {
  const id = shortId(p);
  const when = broke(p);
  return (
    <article
      className="card cl-card"
      data-verdict={p.verdict}
      data-open={open || undefined}
      // while shut it opens from anywhere on it, unless the click ended a text selection
      onClick={() => !open && !window.getSelection()?.toString() && openPanel(id)}
    >
      <p className="cl-verdict label">{VERDICT[p.verdict][0]}</p>
      <h3 className="cl-card-title">{p.question}</h3>
      <p className="cl-outcome">
        Resolved <b>{p.winner}</b>
        {when && <> · {when}</>}
      </p>
      <p className="cl-what">{words(p.what_happened)}</p>
      {/* the wrapper is the size container (on the card it would switch the subgrid off) */}
      <div className="cl-checks-box">
        <Checks p={p} />
      </div>
      <div className="cl-card-foot">
        <MoreButton id={moreId(p)} open={open} panel={panelId(id)} onToggle={() => (open ? onClose() : openPanel(id))} />
        <CardAdd p={p} onMain={onMain} />
      </div>
    </article>
  );
}

/** The opening words of the note's "why", naming the verdict. */
const RUN_IN: Record<ClosedPick["verdict"], string> = {
  "potential case": "Why it could be a case.",
  weak: "Why it's a weaker case.",
  "not a case": "Why it's unlikely.",
};

/**
 * The closed card's panel. On the left the story: what happened, large;
 * why (its opening words naming the verdict); the three checks, a line each
 * (mark, check, answer, what the news said); the open list's "who would
 * know first" when the market was on it; under them the actions: "Add to
 * main page" (watchlist/controls.tsx) and Polymarket. On the right the
 * news: the headlines as a plain list, newest last, folded when long, the
 * first report marked in the verdict's colour and always shown; under them
 * the markets counted under this one (settled by the same news), folded
 * when there are many. With fewer than 3 headlines the checks head the
 * right column, so the two columns end near each other.
 */
function Details({ p, onMain, onClose }: { p: ClosedPick; onMain: string | null; onClose: () => void }) {
  const id = shortId(p);
  const group = groupOf(p);
  const first = firstReport(p);
  const all = checks(p);
  const yes = all.filter((c) => c.state === "met").length;
  const same = p.same_event;
  const sameTitle = `Counted under this one: ${plural(same.length, "other market")}`;
  // the three checks, a line each: a mark, the check, its answer, what the news said (under them on a phone);
  // with fewer than 3 headlines they head the right column instead, so the two columns end near each other
  const fewNews = p.headlines.length < 3;
  const checksSec = (
    <section className="wl-panel-sec" aria-labelledby={`cl-checks-${id}`}>
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
  );
  return (
    <Panel
      id={panelId(id)}
      question={p.question}
      returnTo={moreId(p)}
      onClose={onClose}
      verdict={p.verdict}
      main={
        <>
          <section className="wl-panel-sec">
            <h4 className="visually-hidden">What happened</h4>
            <p className="wl-panel-lead">{words(p.what_happened)}</p>
            {group && (
              <p className="cl-event">
                Part of <span>{group}</span>
              </p>
            )}
            <p className="wl-panel-text">
              <b>{RUN_IN[p.verdict]}</b> {words(p.reason)}
            </p>
          </section>

          {!fewNews && checksSec}

          {(p.who_knows || p.on_open_list_from) && (
            <section className="wl-panel-sec">
              {p.who_knows && <p className="wl-panel-text">{p.who_knows}</p>}
              {p.on_open_list_from && (
                <p className="cl-onlist">
                  On the open list from {fmtDay(new Date(`${p.on_open_list_from}T00:00:00Z`))}
                </p>
              )}
            </section>
          )}

        </>
      }
      side={
        <>
          {fewNews && checksSec}
          <section className="wl-panel-sec" aria-labelledby={`cl-news-${id}`}>
            <h4 className="cl-h" id={`cl-news-${id}`}>
              {p.headlines.length ? `${plural(p.headlines.length, "headline")} checked` : "Headlines checked"}
            </h4>
            {p.headlines.length ? (
              <NewsList
                id={`cl-news-${id}-list`}
                items={p.headlines}
                mark={(h) => (h === first ? "First report" : null)}
                pin={(h) => h === first}
              />
            ) : (
              <p className="cl-empty">None found.</p>
            )}
          </section>

          {same.length > 0 &&
            (same.length >= FOLD ? (
              <details className="cl-more wl-panel-fold">
                <summary>
                  {sameTitle}
                  <Chevron />
                </summary>
                <ul className="wl-plain">
                  {same.map((q) => (
                    <li key={q}>{q}</li>
                  ))}
                </ul>
              </details>
            ) : (
              <section className="wl-panel-sec" aria-labelledby={`cl-same-${id}`}>
                <h4 className="cl-h" id={`cl-same-${id}`}>
                  {sameTitle}
                </h4>
                <ul className="wl-plain">
                  {same.map((q) => (
                    <li key={q}>{q}</li>
                  ))}
                </ul>
              </section>
            ))}
        </>
      }
      foot={
        <>
          <AddToMain p={p} onMain={onMain} />
          <a className="cl-link cl-ext" href={p.url} target="_blank" rel="noopener noreferrer">
            Polymarket <span aria-hidden>↗</span>
          </a>
        </>
      }
    />
  );
}

function DayPicker({ data, day }: { data: ClosedData; day: ClosedDay }) {
  const days = data.days;
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
          const so = cutOf(data, d).short;
          // the chosen day shows just its date (the header has its counts); the others say if they hold a potential case
          if (d === day) return { value: d.day, label: `${dayShort(d.day)} (UTC${so && `, ${so}`})` };
          const n = d.picks.filter((p) => p.verdict === "potential case").length;
          return { value: d.day, label: `${dayShort(d.day)}${so && ` (${so})`}${n ? ` · ${plural(n, "potential case")}` : ""}` };
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

function HowPicked({ data }: { data: ClosedData }) {
  const r = data.rules;
  return (
    <details className="cl-more cl-dayinfo">
      <summary>
        How these were picked
        <Chevron />
      </summary>
      <p>
        Markets that closed on this UTC day with at least {fmtUSD(r.min_volume)} traded, leaving out games, prices, weather,
        elections, public counts, mention markets and releases kept secret until a set time. A model reads each one with the
        news about it, never prices or trades, and answers three questions: could a small group know first, was the outcome a
        surprise, and was it kept quiet until it came out. Every potential or weaker case is shown, up to {fmtInt(r.per_day)},
        topped up to {fmtInt(r.at_least)} with the closest of the rest, and a market that was on the Open now list is always
        shown.
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
 * A day counted before it was over stops at the run that counted it: its
 * counted_at (in a file from before days carried one, the file's
 * generated_at) falls on the day itself. While that UTC day lasts its counts
 * are "so far"; once it is over they say where they stop, "by 04:17" (the
 * run's time), so a partial count never reads as a whole day, the file's
 * last day or an older one whose next run failed. `short` goes in the day
 * picker, `long` after "closed" in a sentence.
 */
type Cut = { short: string; long: string };

function cutOf(data: ClosedData, d: ClosedDay): Cut {
  const run = d.counted_at ?? data.generated_at;
  // counted on a later day than its own: the whole day
  if (!run || run.slice(0, 10) !== d.day) return { short: "", long: "" };
  if (d.day === new Date().toISOString().slice(0, 10)) return { short: "so far", long: " so far" };
  const by = hhmm(run);
  return { short: `by ${by}`, long: ` by ${by} UTC` };
}

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

/** `data` null: no closed list yet (a missing file), which reads like a list with no day in it. */
function ClosedSection({ data, mainSlugs }: { data: ClosedData | null; mainSlugs: Map<string, string> }) {
  const query = useQuery();
  const day = data ? chosenDay(data, query.day, query.open) : undefined;
  if (!data || !day)
    return (
      <>
        <ClosedTitle />
        <p className="cl-empty">Nothing checked yet.</p>
      </>
    );
  const dayCut = cutOf(data, day).long;
  const n = day.picks.length;
  const many = n >= 4;
  // a card opened from a link that names no day, on a day other than the default: closing it stays on its day
  const keepDay = !data.days.some((d) => d.day === query.day) && day !== chosenDay(data, undefined) ? day.day : null;
  const close = (p: ClosedPick) => closeSheet(shortId(p), { open: null, ...(keepDay ? { day: keepDay } : {}) });
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
              <b>{fmtInt(day.closed_markets)}</b> markets closed{dayCut}
            </li>
            <li>Times UTC</li>
          </ul>
        </div>
        <DayPicker data={data} day={day} />
      </header>

      {n ? (
        <div className="cl-cards" data-many={many || undefined} style={{ "--cols": colsFor(n) } as CSSProperties}>
          {day.picks.map((p) => {
            const open = query.open === shortId(p);
            const onMain = mainSlugs.get(p.slug) ?? null;
            return (
              <Fragment key={p.condition_id}>
                <Card p={p} open={open} onMain={onMain} onClose={() => close(p)} />
                {open && <Details p={p} onMain={onMain} onClose={() => close(p)} />}
              </Fragment>
            );
          })}
        </div>
      ) : (
        <p className="cl-empty">
          {day.candidates > 0 ? "The markets left on this day could not be checked yet." : "Nothing left to check on this day."}
        </p>
      )}

      <HowPicked data={data} />
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
