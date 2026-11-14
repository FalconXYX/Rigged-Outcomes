import { type CSSProperties } from "react";
import type { OpenCard, OpenData, OpenDay } from "../../types";
import { fmtDate, fmtDay, fmtInt } from "../../lib/format";
import { setQuery, useQuery } from "../../lib/router";
import { Chevron, MoreButton, NewsList, colsFor, hhmm, ms, plural } from "./shared";
import { Sheet } from "./Sheet";

// "Open now": markets still open where a few people will know the answer
// before the public, and this week's news shows them moving toward it
// (watchlist/opennow.py → data/open.json). The latest list's 3-5 cards, in the
// closed cards' grid, each under the section's indigo rule: the plain
// question, the headline that put it on the list as a quote (a dated card
// with no headline shows its date there instead: when the answer comes out
// and why it is already decided), and who knows first. No checks, counts, prices, odds or red on them (red is
// the closed list's "Potential case"). "Details" opens the rest in a side
// sheet; `?open=` holds which card is open across both sections, so only
// one ever is.

/** A string's FNV-1a hash in base 36: a short, stable id from a story key. */
function hash(s: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(36);
}

/** `?open=` for an open card: "o-" and its story key's hash, never a closed card's id (those start "0x"). */
const openKey = (c: OpenCard) => `o-${hash(c.id)}`;
export const isOpenKey = (v: string | undefined) => v?.startsWith("o-") ?? false;

const moreId = (k: string) => `op-more-${k}`;

/** a list made longer ago than this missed a day's run (they run daily) */
const STALE_MS = 28 * 36e5;

const dayDate = (day: string) => new Date(`${day}T00:00:00Z`);
/** "Dec 31", or "Mar 31, 2027" outside the list's own year */
const onDay = (day: string, ref: string) => (day.slice(0, 4) === ref.slice(0, 4) ? fmtDay : fmtDate)(dayDate(day));
/** "Nov 13, 04:00 UTC" */
const stamp = (iso: string) => `${fmtDay(new Date(iso))}, ${hhmm(iso)} UTC`;

/** "Jan 31, Feb 28, Jun 30 and Dec 31, 2027": a year once, after the last date in it, when it isn't the list's own; a date never breaks across lines. */
function dateList(days: string[], ref: string): string {
  const words = days.map((d, i) =>
    (days[i + 1]?.slice(0, 4) !== d.slice(0, 4) && d.slice(0, 4) !== ref.slice(0, 4) ? fmtDate(dayDate(d)) : fmtDay(dayDate(d))).replace(
      / /g,
      "\u00a0",
    ),
  );
  return words.length < 2 ? words.join("") : `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

/** A date in a line: "Nov 14", never broken across lines. */
const nb = (day: string, ref: string) => onDay(day, ref).replace(/ /g, " ");

/**
 * A dated kind's two lines beside its date: when the answer comes out, and
 * why it is already decided (each from the kind's own "who knows" in
 * watchlist/kinds.py). "Result announced Nov 14" over "The jury votes are
 * cast before then".
 */
const DATED: Record<string, [lead: string, why: string]> = {
  jury_prize: ["Result announced", "The jury votes are cast before then"],
  company_list: ["List published", "It is compiled before then"],
  magazine_pick: ["Pick announced", "It is made before the issue is printed"],
  taped_tv: ["Result airs", "It is filmed before it airs"],
  finished_content: ["Comes out", "It is finished before then"],
  show_setlist: ["Show on", "It is planned and rehearsed before then"],
  scripted_words: ["Said on", "The words are written before then"],
};

/**
 * A dated card with no headline, in the quote's place and at its height: a
 * calendar tile under the indigo rule (the month, with the year when it is
 * not the list's, over the day) and beside it when the answer comes out (at
 * the event the data names, else on the day: "Result announced Nov 14") and
 * why it is already decided. The tile repeats the line's date, so a screen
 * reader skips it unless the line names the event instead.
 */
function DateBlock({ c, day }: { c: OpenCard; day: string }) {
  const t = c.timing;
  const [lead, why] = DATED[c.kind] ?? ["Answer comes out", "It is decided before then"];
  const bare = lead.replace(/ on$/, "");
  const when = t.reveal_note
    ? `${bare} ${t.reveal_note}`
    : t.reveal
      ? `${lead} ${nb(t.reveal, day)}`
      : `${bare} on a set day`;
  const [mon, dd] = t.reveal ? fmtDay(dayDate(t.reveal)).split(" ") : [];
  const year = t.reveal && t.reveal.slice(0, 4) !== day.slice(0, 4) ? ` ${t.reveal.slice(0, 4)}` : "";
  return (
    <div className="op-date">
      {t.reveal && (
        <time className="op-date-tile" dateTime={t.reveal} aria-hidden={!t.reveal_note || undefined}>
          <span className="op-date-mon">
            {mon}
            {year}
          </span>
          <span className="op-date-day">{dd}</span>
        </time>
      )}
      <p className="op-date-text">
        <span className="op-date-when">{when}</span>
        <span className="op-date-why">{why}</span>
      </p>
    </div>
  );
}

/**
 * The headline that put the market on the list, as a quote: the headline in
 * ink after the section's indigo rule, "Reuters · Nov 12" under it, the whole
 * of it a link to the article. With no headline, a dated card shows its date
 * block; an any-day card (not expected) says until when.
 */
function Quote({ c, day }: { c: OpenCard; day: string }) {
  const h = c.headline;
  const t = c.timing;
  if (!h && t.kind === "dated") return <DateBlock c={c} day={day} />;
  if (!h) return <p className="op-quote op-quote-text">Could happen any day{t.until && ` until ${nb(t.until, day)}`}</p>;
  return (
    <a className="op-quote" href={h.url} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}>
      <span className="op-quote-text">{h.title}</span>
      <span className="op-quote-src">
        {h.source}
        {h.published && <> ·&nbsp;{fmtDay(new Date(h.published))}</>}
      </span>
    </a>
  );
}

/**
 * The names in the kind's "who knows first" sentence: "If Lutnick goes, the
 * President, Lutnick and a few aides will know before the public does." →
 * "the President, Lutnick and a few aides". Null when the sentence names
 * no one in that shape (the card then shows it whole).
 */
function knowers(s: string): string | null {
  const m = /^(?:.*?,\s*so\s+|(?:If|Until)\b[^,]*,\s*)?(.+?),?\s+will know\b/.exec(s.trim());
  return m && !/^no\b/i.test(m[1]) ? m[1] : null;
}

function Card({ c, day, open }: { c: OpenCard; day: string; open: boolean }) {
  const k = openKey(c);
  const show = () => setQuery({ open: k });
  const names = knowers(c.who_knows);
  return (
    <article
      className="card cl-card op-card"
      // opens from anywhere on it, unless the click ended a text selection
      onClick={() => !window.getSelection()?.toString() && show()}
    >
      <h3 className="cl-card-title">{c.question}</h3>
      <Quote c={c} day={day} />
      <p className="op-who">
        {names ? (
          <>
            <span className="op-who-k">Known first by:</span> {names}
          </>
        ) : (
          c.who_knows
        )}
      </p>
      <MoreButton id={moreId(k)} open={open} onOpen={show} />
    </article>
  );
}

/** a question as compared: case, punctuation and spacing folded */
const norm = (q: string) => q.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

/**
 * The open card's sheet: what Polymarket asks (its own question for the
 * nearest date, the other dates), why this kind is watched, the other side
 * if a headline denies the step, the news from the last 14 days as a plain
 * list, whether the answer is reported out yet, and since when it is listed.
 */
function Details({ c, day }: { c: OpenCard; day: string }) {
  const k = openKey(c);
  const more = c.polymarket.other_dates;
  const same = norm(c.polymarket.question) === norm(c.question);
  const done = c.timing.kind === "dated" ? "the answer is out" : "it has happened";
  return (
    <Sheet label={c.question} returnTo={moreId(k)} onClose={() => setQuery({ open: null })}>
      <section className="wl-sheet-sec" aria-labelledby={`op-asks-${k}`}>
        <h4 className="cl-h wl-sheet-first" id={`op-asks-${k}`}>
          Polymarket asks
        </h4>
        <p className="wl-sheet-lead">{c.polymarket.question}</p>
        {more.length > 0 && (
          <p className="op-ask-more">
            {same ? "The same question for" : "It also asks about"} {dateList(more, day)}.
          </p>
        )}
      </section>
      <section className="wl-sheet-sec" aria-labelledby={`op-why-${k}`}>
        <h4 className="cl-h" id={`op-why-${k}`}>
          Why this kind is watched
        </h4>
        <p className="wl-sheet-text">{c.why_watched}</p>
      </section>
      {c.other_side && (
        <section className="wl-sheet-sec" aria-labelledby={`op-other-${k}`}>
          <h4 className="cl-h" id={`op-other-${k}`}>
            The other side
          </h4>
          <NewsList items={[c.other_side]} />
        </section>
      )}
      <section className="wl-sheet-sec" aria-labelledby={`op-news-${k}`}>
        <h4 className="cl-h" id={`op-news-${k}`}>
          News from the last 14 days
        </h4>
        {c.headlines.length ? (
          <NewsList items={c.headlines} mark={(h) => (h.on_card ? "On the card" : h.step ? "Step" : null)} />
        ) : (
          <p className="cl-empty">None found.</p>
        )}
        {c.checked_at && (
          <p className="op-checked">
            No report yet that {done} (checked {fmtDay(new Date(c.checked_at))})
          </p>
        )}
      </section>
      <div className="wl-sheet-foot">
        <span>On the list since {onDay(c.listed_since, day)}</span>
        <a className="cl-link cl-ext" href={c.polymarket.url} target="_blank" rel="noopener noreferrer">
          Polymarket <span aria-hidden>↗</span>
        </a>
      </div>
    </Sheet>
  );
}

function HowPicked({ data, day }: { data: OpenData; day: OpenDay }) {
  const r = data.rules;
  const person = r.steps_by !== "model";
  const waiting = day.awaiting ?? 0;
  return (
    <details className="cl-more cl-dayinfo">
      <summary>
        How these were picked
        <Chevron />
      </summary>
      <p>
        This list was made {fmtDay(new Date(day.run_at))} at {hhmm(day.run_at)} UTC from Polymarket's open markets with at
        least ${fmtInt(r.min_volume)} traded. It leaves out what the closed list leaves out (games, prices, weather and nature,
        elections and votes, things counted or ranked in public, words said in public, and releases kept secret until a set
        time: official statistics, central bank decisions, scheduled company results and share-offering prices). It also
        leaves out joke markets, answers settled by a live public vote, disease, and answers that build up in public, such
        as ground taken in a war.
      </p>
      {person ? (
        <p>
          A market can be listed when this week's news shows the few people who decide taking a concrete step toward the
          answer, such as talks, preparations or a search for a candidate. A model reads each market's step headlines from
          the last 7 days twice, the second time in reverse order, and suggests the ones both readings mark as a step. The
          list is a trial: a person reads each card's step headline and approves it before the card shows
          {waiting > 0 ? ` (${plural(waiting, "more market")} with a new step ${waiting === 1 ? "is" : "are"} waiting for that check)` : ""}.
          A second model reads the headlines for a report that the answer is already out, also twice, and the code checks
          every headline it copies word for word. Neither model sees a price, the odds or any trading, and neither chooses
          the cards. These fixed rules do:
        </p>
      ) : (
        <p>
          A market can be listed when this week's news shows the few people who decide taking a concrete step toward the
          answer, such as talks, preparations or a search for a candidate. A model reads each market's step headlines from
          the last 7 days twice, the second time in reverse order. A headline counts as a step only when both readings mark it
          and the people the model says act are named in it. A second model reads the headlines for a report that the answer
          is already out, also twice, and the code checks every headline it copies word for word. Neither model sees a price,
          the odds or any trading, and neither chooses the cards. These fixed rules do:
        </p>
      )}
      <ul>
        <li>
          Up to {fmtInt(r.per_day - 2)} places go to markets with a new step in the last {plural(r.fresh_days, "day")}: first
          those a few people decide, then steps that leaked through sources, then the newest.
        </li>
        <li>A card that comes on with a new step stays at least {plural(r.stay_days, "day")}, unless it is held back.</li>
        <li>
          One place goes to a market that has gone quiet: its latest step is more than {plural(r.fresh_days, "day")} and at
          most {plural(r.live_days, "day")} old. The documented cases often came in that quiet stretch.
        </li>
        <li>
          One place goes to an answer decided in advance, such as a jury's vote, that comes out within{" "}
          {plural(r.dated_days, "day")}. When no market fits the quiet or the dated place, it goes to the next market with a
          new step.
        </li>
        <li>No more than 2 cards have the same person or body deciding, and one piece of news fills one card.</li>
        <li>
          A market is held back while a headline from the last {plural(r.held_back_days, "day")} suggests its answer may
          already be out.{day.held_back > 0 && ` On this list that holds back ${plural(day.held_back, "market")}.`}
        </li>
      </ul>
      <p>
        Fewer than {fmtInt(r.at_least)} cards means fewer markets qualified{person ? " or have been approved so far" : ""}: the
        list is never padded. When a listed market closes, the closed list below checks it, and its details there say when it
        was on this list.
      </p>
    </details>
  );
}

/** The ghost cards' places, by distance from the one with the message: narrower screens keep the nearest. */
const ghostPos = (i: number, m: number) => (i === m ? "msg" : i === m - 1 ? "prev" : i === m + 1 ? "next" : "far");

/**
 * No list to show: ghost cards in the open cards' shape and the closed cards'
 * columns (a label, a question, the headline row, the sentence), still and
 * faint, the middle one saying why. Nothing in them is data.
 */
function Ghosts({ cols, msg }: { cols: number; msg: string }) {
  const m = Math.floor((cols - 1) / 2);
  return (
    <div className="wl-ghosts" data-many={cols >= 4 || undefined} style={{ "--cols": cols } as CSSProperties}>
      {Array.from({ length: cols }, (_, i) => {
        const pos = ghostPos(i, m);
        return (
          <div key={i} className="wl-ghost" data-pos={pos} aria-hidden={pos !== "msg" || undefined}>
            {pos === "msg" ? (
              <p className="wl-ghost-head wl-ghost-msg">{msg}</p>
            ) : (
              <span className="wl-ghost-head">
                <span className="wl-bone" data-w="xs" />
                <span className="wl-bone" data-w="l" />
              </span>
            )}
            <span className="wl-ghost-line" aria-hidden>
              <span className="wl-bone" data-w="xs" />
              <span className="wl-bone" data-w="m" />
            </span>
            <span className="wl-ghost-who" aria-hidden>
              <span className="wl-bone" data-w="l" />
              <span className="wl-bone" data-w="s" />
            </span>
          </div>
        );
      })}
    </div>
  );
}

/**
 * The open section. `data`: undefined while loading, null when there is no
 * list yet (a quiet placeholder holds the place). `cols`: the closed cards'
 * columns, which the placeholder's ghosts take.
 */
export function OpenNow({ data, cols }: { data: OpenData | null | undefined; cols: number }) {
  const query = useQuery();
  const day = data?.days[0];
  const n = day?.cards.length ?? 0;
  const sheet = day?.cards.find((c) => query.open === openKey(c));
  // missed today's run: the pipeline said so, or no run since yesterday's
  const stale = !!day && (day.status === "not updated today" || Date.now() - ms(day.run_at) > STALE_MS);
  const lastRun = data?.days.find((d) => d.status === "updated")?.run_at ?? day?.run_at;
  const pill = data === undefined ? null : data === null || !day ? "Coming soon" : data.trial ? "Trial" : null;
  const person = data?.rules.steps_by !== "model";
  // with a person approving steps, fewer cards can mean steps still wait for that check
  const waiting = person ? (day?.awaiting ?? 0) : 0;
  const wait = waiting
    ? ` ${plural(waiting, n ? "more market" : "market")} with a new step ${waiting === 1 ? "is" : "are"} waiting for a person's check.`
    : "";
  const never = "The list is never padded with markets that have no step in the news.";
  const few = `${n === 1 ? "Only 1 market qualifies" : `Only ${fmtInt(n)} markets qualify`}${stale ? "" : " today"}.${wait} ${never}`;
  return (
    <section className="wl-open" aria-labelledby="wl-open-title">
      <header className="cl-top">
        <div className="cl-heading">
          <div className="wl-open-head">
            <h2 className="wl-sec-title" id="wl-open-title">
              <span className="wl-dot" aria-hidden />
              Open now
            </h2>
            {pill && (
              <span
                className="wl-pill label"
                title={pill === "Trial" && person ? "A trial: each card's step is checked by hand before it shows" : undefined}
              >
                {pill}
              </span>
            )}
          </div>
          {day && (
            <ul className="meta-line label cl-meta tabular">
              <li>
                <b>{fmtInt(n)}</b> {n === 1 ? "market" : "markets"}
              </li>
              {!stale && (
                <li>
                  <b>{fmtInt(day.new_today)}</b> new today
                </li>
              )}
              {stale ? (
                <>
                  <li>Not updated today</li>
                  {lastRun && <li>Last updated {stamp(lastRun)}</li>}
                </>
              ) : (
                <li>Updated {stamp(day.run_at)}</li>
              )}
            </ul>
          )}
        </div>
      </header>

      {day && (
        <p className="op-lede">
          Markets still open where a few people will know the answer before the public, and this week's news shows them
          moving toward it. Being listed here does not mean anyone has bet on it.
        </p>
      )}

      {data === null || (data && !day) ? (
        <Ghosts cols={cols} msg="Open markets worth watching will appear here." />
      ) : day && n === 0 ? (
        <Ghosts cols={cols} msg={`No market qualifies${stale ? "" : " today"}.${wait} ${never}`} />
      ) : (
        day && (
          <div className="cl-cards" data-many={n >= 4 || undefined} style={{ "--cols": colsFor(n) } as CSSProperties}>
            {day.cards.map((c) => (
              <Card key={c.id} c={c} day={day.day} open={query.open === openKey(c)} />
            ))}
            {/* a short row: its empty columns say why, the first in words */}
            {Array.from({ length: colsFor(n) - n }, (_, i) => (
              <div key={`fill-${i}`} className="op-fill" aria-hidden={i > 0 || undefined}>
                {i === 0 && <p>{few}</p>}
              </div>
            ))}
          </div>
        )
      )}

      {data && day && n > 0 && n < data.rules.at_least && (
        <p className="op-few" data-fill={colsFor(n) > n || undefined}>
          {few}
        </p>
      )}

      {data && day && <HowPicked data={data} day={day} />}
      {sheet && day && <Details key={sheet.id} c={sheet} day={day.day} />}
    </section>
  );
}
