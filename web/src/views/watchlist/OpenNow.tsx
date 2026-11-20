import { Fragment, type CSSProperties } from "react";
import type { OpenCard, OpenData } from "../../types";
import { fmtDate, fmtDay, fmtDayAt, fmtInt, fmtStamp, torontoDay } from "../../lib/format";
import { closeSheet, useQuery } from "../../lib/router";
import { Chevron, MoreButton, NewsList, Panel, colsFor, ms, openPanel, panelId, plural } from "./shared";
import { CardWatch, WatchControl } from "./controls";

// "Open now": markets still open where a few people will know the answer
// before the public, and this week's news shows them moving toward it
// (watchlist/opennow.py → open.json, served by the API). The latest list's 3-5 cards, in the
// closed cards' grid, each under the section's indigo rule: the plain
// question, the headline that put it on the list as a quote (a dated card
// with no headline shows its date there instead: when the answer comes out
// and why it is already decided), who knows first, and at the foot "Details"
// and "Watch this market" (controls.tsx). No checks, counts, prices, odds or
// red on them (red is the closed list's "Potential case"). "Details" opens
// the rest in a panel of its own under the row (shared.tsx); `?open=` holds
// which card is open across both sections, so only one ever is.

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

/** a list made longer ago than this missed a day's run (the daily run starts at 04:00 Toronto time) */
const STALE_MS = 40 * 36e5;

const dayDate = (day: string) => new Date(`${day}T00:00:00Z`);
/** "Dec 31", or "Mar 31, 2027" outside the list's own year */
const onDay = (day: string, ref: string) => (day.slice(0, 4) === ref.slice(0, 4) ? fmtDay : fmtDate)(dayDate(day));

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
        {h.published && <> ·&nbsp;{fmtDayAt(h.published)}</>}
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

const close = (k: string) => closeSheet(k, { open: null });

function Card({ c, day, open }: { c: OpenCard; day: string; open: boolean }) {
  const k = openKey(c);
  const names = knowers(c.who_knows);
  return (
    <article
      className="card cl-card op-card"
      data-open={open || undefined}
      // while shut it opens from anywhere on it, unless the click ended a text selection
      onClick={() => !open && !window.getSelection()?.toString() && openPanel(k)}
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
      <div className="cl-card-foot">
        <MoreButton id={moreId(k)} open={open} panel={panelId(k)} onToggle={() => (open ? close(k) : openPanel(k))} />
        <CardWatch card={c} />
      </div>
    </article>
  );
}

/** a question as compared: case, punctuation and spacing folded */
const norm = (q: string) => q.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

/**
 * The open card's panel. On the left what Polymarket asks (its own question
 * for the nearest date, the other dates; when its question is the card's,
 * which the panel's first line already says, only the other dates), why
 * this kind is watched, the other side if a headline denies the step; under
 * them since when it is listed, Polymarket and "Watch this market"
 * (controls.tsx) on a line of its own. On the right the recent news as a
 * plain list, folded when long, the card's headline always shown (the last
 * 14 days, plus every step, which on a gone-quiet card can be up to 21 days
 * old), and whether the answer is reported out yet.
 */
function Details({ c, day }: { c: OpenCard; day: string }) {
  const k = openKey(c);
  const more = c.polymarket.other_dates;
  const same = norm(c.polymarket.question) === norm(c.question);
  const done = c.timing.kind === "dated" ? "the answer is out" : "it has happened";
  return (
    <Panel
      id={panelId(k)}
      question={c.question}
      returnTo={moreId(k)}
      onClose={() => close(k)}
      main={
        <>
          {same ? (
            more.length > 0 && <p className="op-ask-more">Polymarket asks the same question for {dateList(more, day)}.</p>
          ) : (
            <section className="wl-panel-sec" aria-labelledby={`op-asks-${k}`}>
              <h4 className="cl-h" id={`op-asks-${k}`}>
                Polymarket asks
              </h4>
              <p className="wl-panel-lead">{c.polymarket.question}</p>
              {more.length > 0 && <p className="op-ask-more">It also asks about {dateList(more, day)}.</p>}
            </section>
          )}
          <section className="wl-panel-sec" aria-labelledby={`op-why-${k}`}>
            <h4 className="cl-h" id={`op-why-${k}`}>
              Why this kind is watched
            </h4>
            <p className="wl-panel-text">{c.why_watched}</p>
          </section>
          {c.other_side && (
            <section className="wl-panel-sec" aria-labelledby={`op-other-${k}`}>
              <h4 className="cl-h" id={`op-other-${k}`}>
                The other side
              </h4>
              <NewsList id={`op-other-${k}-list`} items={[c.other_side]} />
            </section>
          )}
        </>
      }
      side={
        <section className="wl-panel-sec" aria-labelledby={`op-news-${k}`}>
          <h4 className="cl-h" id={`op-news-${k}`}>
            Recent news
          </h4>
          {c.headlines.length ? (
            <NewsList
              id={`op-news-${k}-list`}
              items={c.headlines}
              mark={(h) => (h.on_card ? "On the card" : h.step ? "Step" : null)}
              pin={(h) => !!h.on_card}
            />
          ) : (
            <p className="cl-empty">None found.</p>
          )}
          {c.checked_at && (
            <p className="op-checked">
              No report yet that {done} (checked {fmtDayAt(c.checked_at)})
            </p>
          )}
        </section>
      }
      foot={
        <>
          <span>On the list since {onDay(c.listed_since, day)}</span>
          <a className="cl-link cl-ext" href={c.polymarket.url} target="_blank" rel="noopener noreferrer">
            Polymarket <span aria-hidden>↗</span>
          </a>
          <WatchControl card={c} />
        </>
      }
    />
  );
}

function HowPicked({ data }: { data: OpenData }) {
  const r = data.rules;
  return (
    <details className="cl-more cl-dayinfo">
      <summary>
        How these were picked
        <Chevron />
      </summary>
      <p>
        Polymarket markets with at least ${fmtInt(r.min_volume)} traded whose answer a few people learn before the public
        (no games, prices, weather, elections, public counts, mention markets or scheduled releases). A market is listed
        when headlines from the last 7 days show those people taking a concrete step toward the answer, such as talks or a
        search for a candidate, as read by a model that never sees prices or trading
        {r.steps_by !== "model" && " and confirmed by a person"}. One of the {fmtInt(r.per_day)} places goes to a market whose
        last step is {fmtInt(r.fresh_days + 1)} to {fmtInt(r.live_days)} days old, and one to an answer decided in advance
        that comes out within {plural(r.dated_days, "day")}. A market leaves the list once its answer is public, which
        includes the game, film, show, album or book it is about coming out.
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
  // missed today's run: the pipeline said so, or no run since yesterday's
  const stale = !!day && (day.status === "not updated today" || Date.now() - ms(day.run_at) > STALE_MS);
  const lastRun = data?.days.find((d) => d.status === "updated")?.run_at ?? day?.run_at;
  // "today" only while the list's day lasts in Toronto; after midnight it names its day ("3 new on Nov 13")
  const current = !!day && !stale && day.day === torontoDay();
  const onItsDay = day && !stale && !current ? ` on ${onDay(day.day, day.day)}` : "";
  const few = stale
    ? `${n === 1 ? "Only 1 market qualifies" : `Only ${fmtInt(n)} markets qualify`}.`
    : current
      ? `${n === 1 ? "Only 1 market qualifies" : `Only ${fmtInt(n)} markets qualify`} today.`
      : `${n === 1 ? "Only 1 market qualified" : `Only ${fmtInt(n)} markets qualified`}${onItsDay}.`;
  const none = stale
    ? "No market qualifies."
    : `No market ${current ? "qualifies today" : `qualified${onItsDay}`}.`;
  return (
    <section className="wl-open" aria-labelledby="wl-open-title">
      <header className="cl-top">
        <div className="cl-heading">
          <h2 className="wl-sec-title" id="wl-open-title">
            <span className="wl-dot" aria-hidden />
            Open now
          </h2>
          {day && (
            <ul className="meta-line label cl-meta tabular">
              <li>
                <b>{fmtInt(n)}</b> {n === 1 ? "market" : "markets"}
              </li>
              {!stale && (
                <li>
                  <b>{fmtInt(day.new_today)}</b> new {current ? "today" : `on ${onDay(day.day, day.day)}`}
                </li>
              )}
              <li>Updated {fmtStamp(stale ? (lastRun ?? day.run_at) : day.run_at)}</li>
            </ul>
          )}
        </div>
      </header>

      {day && (
        <p className="op-lede">Open markets with the potential for insider trading.</p>
      )}

      {data === null || (data && !day) ? (
        <Ghosts cols={cols} msg="Open markets worth watching will appear here." />
      ) : day && n === 0 ? (
        <Ghosts cols={cols} msg={none} />
      ) : (
        day && (
          <div className="cl-cards" data-many={n >= 4 || undefined} style={{ "--cols": colsFor(n) } as CSSProperties}>
            {day.cards.map((c) => {
              const open = query.open === openKey(c);
              return (
                <Fragment key={c.id}>
                  <Card c={c} day={day.day} open={open} />
                  {open && <Details c={c} day={day.day} />}
                </Fragment>
              );
            })}
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

      {data && day && <HowPicked data={data} />}
    </section>
  );
}
