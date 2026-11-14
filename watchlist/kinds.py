"""
The one vocabulary both lists use: the closed list (watchlist.upsets) and
the "Open now" list. Plain data and small pure functions, no network, no
store, no model, so anything can import it (it imports nothing from the
package but the standard library).

The user approved the "Open now" proposal on 2026-11-13 ("ok try to
implement this"; final.md in the session scratchpad). What it fixes here:

1. Embargoed releases. Official statistics, central bank rate decisions
   (scheduled or emergency), scheduled company results and scheduled share
   offerings are guarded by a procedure built to keep the answer from
   traders, and no case of insider betting on one is documented. Both lists
   drop them with is_embargoed() before any model call. On the closed list's
   old replay (201 events, Oct 31 - Nov 12) the rule finds exactly the 15
   embargoed ones.
2. The kinds and the three groups: who holds the answer before the public,
   and how tightly. Group 1: a few people decide and choose the moment.
   Group 2: a formal process or several sides. Group 3: nobody holds it in a
   way the news can show. A fixed rule a person wrote from the documented
   cases; the model plays no part in it. Word rules on the event's title
   and first question file an event (file_event); one they cannot place is
   left unfiled (kind None, counted in group 2) for the model or a person
   to file from the same list, never put in a default kind.
3. The dated place: answers decided in advance and held loosely (a jury's
   vote, a taped finale, a finished game, a company's year-end list, a
   magazine's pick, a setlist, a scripted speech). Settled by a live public
   vote or decided while people watch: left out.
4. Per kind, the card's plain question, its "who would know first" sentence
   (names from the market: subject, decider, ...), the event that would
   settle it (what the model is told to look for) and the "why this kind is
   watched" sentence, which names a documented case from the outside record
   (outside_cases.md) or says that none is documented. No sentence says who
   might bet: they say who will KNOW first.
5. Headline words: the step words (the shortlist's yes/no test, and the
   headlines the step reading is given), the leak
   words (a step reported through sources ranks first), the odds and
   betting filter (such headlines never reach the model), the source ranks
   (agencies, then national outlets, then the rest) and the word-for-word
   copy check.

The closed list's exclusion rules (public counts, mention markets, nature,
the election keyword, "continues through") live here too, so the two lists
share them; watchlist.upsets re-exports them unchanged. closed_exclusion()
is the closed list's version after the approved factual changes (embargoed
releases out; a company's own year-end list and a scripted speech or
produced video are no longer left out), and upsets.candidates applies it
before any model call.

    is_embargoed(question, event_title="") -> reason | None
    open_exclusion(title, question="") -> reason | None   the open pool's word rules
    closed_exclusion(question, event_title="") -> reason | None   the closed list's, after the changes
    file_event(title, question="") -> Filing(kind, group, place, how)
    card_text(kind, names) -> {"question", "who_knows", "why_watched", "settles"}
    is_odds(title, source="") / has_step_words(title) / is_leak(title)
    source_rank(source) / card_headline(headlines) / match_copy(copy, headlines)
    names_subject(title, names) / effective_ts(published) / is_date_only(published)
"""
import re
import unicodedata
from collections import namedtuple
from datetime import datetime, timezone

_R = lambda p: re.compile(p, re.I)  # noqa: E731


# ── the open list's rules (final.md sections 2-4) ────────────────────────────

MIN_VOLUME = 10_000        # an event needs this much traded to be in the pool (the closed list's bar)
PRICE_CAP = 0.99           # ... and no outcome at 99c or more when it FIRST entered the pool (checked once,
                           # never again: a jump to 99c later may be the very case the list is for)
DUE_SOON_HOURS = 12        # an event due within this many hours is not in the pool
LIVE_DAYS = 21             # a market is live while it has a step headline this recent
FRESH_DAYS = 3             # a step this recent is a "new step"; live but older is "gone quiet"
STAY_DAYS = 3              # a card that enters with a new step stays at least this many lists (while live, not held back)
DATED_DAYS = 3             # the dated place takes an answer due within this many days
HELD_BACK_DAYS = 7         # an "already out" or "unclear" headline this recent holds a card back
PER_DAY = 5                # cards a day, at most ...
AT_LEAST = 3               # ... and the page says so when fewer qualify (never padded)
FRESH_PLACES = 3           # places 1-3: new steps; place 4: gone quiet; place 5: dated
MAX_PER_DECIDER = 2        # at most this many cards with the same decider
NEWS_DAYS = 7              # the model reads headlines from the last 7 days ...
NEWS_MAX = 15              # ... up to 15 a story
DETAILS_DAYS = 14          # Details lists the last 14 days: up to DETAILS_MAX plus every step
DETAILS_MAX = 10
STORIES_PER_REQUEST = 5
OPEN_MODEL = "gemini-3.5-flash-lite"   # often busy (503): wait 30-45 s and ask again
BUSY_WAIT = (30, 45)
# The open list's step reading (watchlist.opennow, PROMPT_VERSION): a free Gemma model on the same key, with its
# own allowance (no daily cap was met in about 540 requests on 2026-11-13). Its 503s come back in under a second,
# so a shorter wait does; one story per request, 20-90 s an answer.
STEP_MODEL = "gemma-4-31b-it"
STEP_BUSY_WAIT = (20, 35)


# ── 1. embargoed releases (both lists, before any model call) ────────────────

_CB_NAME = (r"(fed|fomc|federal reserve|ecb|european central bank|central bank|reserve bank|national bank|"
            r"people'?s bank of china|pboc|riksbank|norges bank|banxico|copom|bank of (england|japan|canada|mexico|"
            r"korea|brazil|russia|israel|thailand|indonesia|turkey|chile|colombia|india|australia|new zealand|poland|"
            r"sweden|norway|ghana|nigeria|ukraine|taiwan))")
_CB_RATE = (r"(interest rates?|rates?|rate (cuts?|hikes?|decision|change)|bps|basis points?|no change|dissents?|"
            r"dissent\w*|upper bound|target range|policy rate|repo rate|cash rate|overnight rate|key rate|base rate|"
            r"bank rate|selic)")
# A bare move word counts only right after the bank's name and when it is about rates: "Will the Fed cut
# in December?", "Fed hike by ...", never "the Fed cut staff".
_CB_MOVE = (r"(cuts?|hikes?|pause[sd]?)\b(?=\s*(\?|$|\|\||in\b|by\b|at\b|after\b|before\b|this\b|next\b|again\b|"
            r"rates?\b|interest\b|\d))")
# Who sets it under a quiet period and a lock-up: a central bank's rate, named with the bank ("Fed decision
# in December?", "Bank of Japan: no change", "Fed decreases interest rates by 25 bps"). A market that
# only mentions the bank or a meeting is not one: "Fed Chair" / "Fed Governor" picks, "Will Trump fire
# Powell before the December Fed meeting?", "Will the Fed cut staff?", and interest rates nobody sets in
# secret ("cap credit card interest rates") stay.
CENTRAL_BANK = _R(rf"\b{_CB_NAME}\b.*\b{_CB_RATE}\b|\b{_CB_RATE}\b.*\b{_CB_NAME}\b|"
                  rf"\b{_CB_NAME}\b\s+(interest rate |rate |policy |monetary policy )?decisions?\b|"
                  rf"\b{_CB_NAME}\b(\s+\w+){{0,2}}\s+{_CB_MOVE}|\bselic\b|\b\d+ ?bps\b")
OFFICIAL_STATISTIC = _R(r"\b(cpi|ppi|pce|core pce|inflation(?! reduction act)|deflation|gdp|recession|unemployment|"
                        r"jobless (claims|rate)|"
                        r"non-?farm|payrolls?|jobs (added|report)|pmi|ism (manufacturing|services)|"
                        r"consumer (sentiment|confidence)|retail sales|trade (deficit|surplus|balance)|"
                        r"industrial production|housing starts|births|soft landing|hard landing|economic state)\b"
                        r"|\badds?\b.{0,25}\bjobs\b")
_RESULT = r"(revenue|sales|profit|deliveries|subscribers|margin|eps|earnings|net income|ratio|growth)"
COMPANY_RESULTS = _R(r"\b(earnings|eps|comparable sales|comp sales|organic (net )?sales|same-store sales|combined ratio|"
                     r"quarterly (results|revenue|earnings|sales|profit))\b"
                     rf"|\b(q[1-4]|(first|second|third|fourth)[- ]quarter)\b.*\b{_RESULT}\b")
# A scheduled offering's price (its range, valuation, market cap at the close, shares sold, money
# raised) is set by the company and its underwriters under a quiet period. Read on the market's own
# question only: whether or when a company goes public at all (Kraken, Holtec's pulled offering) is a
# company decision, filed as ipo_filing.
SHARE_OFFERING = _R(r"\b(ipo|initial public offering)\b.{0,60}\b(price[ds]?|pricing|valuation|market cap\w*|"
                    r"opening|closing|first[- ]day|raises?)\b"
                    r"|\b(price[ds]?|pricing|valuation|market cap|raise[sd]?)\b.{0,60}\b(ipo|initial public offering)\b"
                    r"|%\s+of .{0,30}\b(sold|shares)\b.{0,30}\bipo\b|\bsell .{0,30}\bof its shares\b")
# Offerings already scheduled (the company has begun its roadshow): every market about such an offering's
# date, month, banks or price is a scheduled share offering, as the proposal's "left out" table has it for
# Anthropic. A person adds a company here when its roadshow begins (reported in the news), and removes it
# once its shares trade.
SCHEDULED_OFFERINGS = ("anthropic",)
_OFFERING_WORDS = r"(ipo|initial public offering|go(es)? public|listing|lead bank|underwriter)"
SCHEDULED_OFFERING = _R(rf"\b({'|'.join(SCHEDULED_OFFERINGS)})\b.*\b{_OFFERING_WORDS}\b|"
                        rf"\b{_OFFERING_WORDS}\b.*\b({'|'.join(SCHEDULED_OFFERINGS)})\b")
EMBARGOED = (
    ("central bank rate decision", CENTRAL_BANK),
    ("official statistic", OFFICIAL_STATISTIC),
    ("scheduled company results", COMPANY_RESULTS),
)
EMBARGO_REASON = "embargoed release (official statistic, rate decision, company results or share offering)"


def is_embargoed(question, event_title=""):
    """Which embargoed release a market asks about ("central bank rate
    decision", "official statistic", "scheduled company results", "scheduled
    share offering"), or None. The first three are read on the question and
    the event title, the offering price on the question alone."""
    t = normalise_title(f"{question or ''} || {event_title or ''}")
    why = next((why for why, rx in EMBARGOED if rx.search(t)), None)
    if why is None and (SHARE_OFFERING.search(normalise_title(question)) or SCHEDULED_OFFERING.search(t)):
        why = "scheduled share offering"
    return why


# ── 2. the closed list's exclusion rules (moved here from watchlist.upsets) ──

# Counted or charted in public as it happens (views, tweets, box office, charts, case counts, ship
# transits, live streams): the price follows public data, not a secret. Found by checking 68 ranked
# markets against what happened (2026-11-12): 19 were public data and 2 of them plausible.
PUBLIC_COUNT = re.compile(
    r"\b(views|tweets|retweets|posts|followers|subscribers|likes|streams|listeners|downloads)\b"
    r"|box office|domestic gross|opening weekend|\bbillboard\b|\bhot 100\b|#\s?\d+\b|\btop (song|album|seller)s?\b"
    r"|\bspotify\b|\bsteam\b.*\b(top|seller)|\b\d+(\.\d+)?[kmbt]\s+tokens\b|\b\d[\d,]*\+?\s+(\w+\s+)?cases\b"
    r"|\btransits?\b|\b(live ?stream(ing|er)?|subathon|marathon)\b", re.I)
# Mention markets ("Will Trump say "Bibi" this week?"): the words are said in public, often live.
MENTION = re.compile(r"\b(say|says|said|mention|mentions)\s+[\"“']", re.I)
# Weather and nature nobody can know early (filters.exclusion catches temperature and rain).
NATURE = re.compile(r"\b(tornado(es)?|hurricanes?|earthquakes?|named storms?|wildfires?|volcan(o|ic)|eruptions?"
                    r"|hottest|warmest|coldest|wettest|driest)\b", re.I)
ELECTION = re.compile(r"\b(election|elected|primary|runoff|referendum|mayoral|governor race|senate race|house seat|"
                      r"presidential race|win the .* (race|seat)|electoral|ballot measure)\b"
                      r"|\bsweep\b.{0,40}\b(house|senate|congress)\b", re.I)
# "X continues through <date>" and "will there be no X": the answer that means the event happened is No.
CONTINUES = re.compile(r"\b(continues?|remains?|stays?|still|holds?|keeps?)\b", re.I)
NEGATED = re.compile(r"\bwill there be no\b|\bno (new|next)\b", re.I)

# The approved factual changes (final.md section 6, items 3-5). A company's own year-end list is
# compiled from its own data and revealed on one day: not a public count (Google's Year in Search is
# the case the Justice Department charged). The words of a scripted, scheduled speech or a produced
# video are written before they are said: not a public speech (a White House teleprompter operator
# settled with the CFTC; Kalshi fined a video editor).
COMPANY_LIST = _R(r"most searched|year in search|\bwrapped\b|top (songs?|artists?|albums?|podcasts?) of (the year|20\d\d)")
SCRIPTED = _R(r"state of the union|(joint|inaugural|oval office|prime-?time|televised|farewell) address|"
              r"address to (congress|the nation|the un|the united nations)|general assembly (speech|address)|"
              r"\bkeynote\b|commencement (speech|address)|prepared remarks|teleprompter|\bmrbeast\b|"
              r"\b(next|new|youtube) video\b")
# Who "could know first", for the judge's examples (item 2): no central bank board or statistics body.
KNOW_FIRST_EXAMPLES = ("officials planning a strike or a raid, a leader deciding to fire someone, the board around a "
                       "person who might resign, a company's launch team, a jury, a production crew, a campaign's staff")


def happened_side(question):
    """The answer that means the event happened: Yes, or No on "X continues
    through <date>" (the ceasefire did not continue) and "will there be no
    X by <date>"."""
    q = str(question or "")
    return "no" if (CONTINUES.search(q) or NEGATED.search(q)) else "yes"


def closed_exclusion(question, event_title=""):
    """The closed list's word rules after the approved factual changes, as
    the reason a market is left out before any model call, or None
    (watchlist.upsets.candidates applies it). An election or a vote first (a
    combined market such as "Dem sweep & US recession" is an election), then
    embargoed releases, public counts (a company's own year-end list is not
    one), mention markets (the words of a scripted speech or a produced video
    are not left out), nature."""
    t = normalise_title(f"{question or ''} {event_title or ''}")
    if ELECTION.search(t):
        return "election or vote"
    if is_embargoed(question, event_title):
        return EMBARGO_REASON
    if PUBLIC_COUNT.search(t) and not COMPANY_LIST.search(t):
        return "counted or charted in public (views, box office, charts, ...)"
    if MENTION.search(t) and not SCRIPTED.search(t):
        return "words said in public (mention market)"
    if NATURE.search(t):
        return "weather"
    return None


# ── 3. kinds, groups and the dated place ─────────────────────────────────────

GROUPS = {
    1: "A few people decide and choose the moment",
    2: "A formal process or several sides",
    3: "Nobody holds it in a way the news can show",
}
UNFILED_GROUP = 2          # a market the word rules cannot place waits here for the model or a person

# The slots a kind's sentences take, all copied from the market or named by a person or the model:
SLOTS = {
    "subject": "the market's main subject as the market names it (a person, company, country or product)",
    "short": "a short form of the subject (a surname, a company's usual name); defaults to the subject",
    "aliases": "other names the news uses for the subject (a list); used only to check headlines",
    "decider": "who decides (Trump, Nvidia's board, Israel's government); defaults to the subject",
    "others": "other people who would also know (a list), named after the decider",
    "other": "the other side (the target of a strike, the other party to a deal, the exchange)",
    "post": "the job or office (the Commerce Department, Nvidia's chief executive, the S&P 500)",
    "what": "the outcome as a noun phrase (a merger, a strike on Iran, its token, the Turner Prize)",
    "which": "how a dated question starts (Who, Which country, Which film); defaults to Who",
    "question": "a plain question, only for kinds whose answer no template fits (a game's content, a setlist)",
}

Kind = namedtuple("Kind", "key group section place slots question settles who_knows why")

_MILITARY = "Strikes, military operations, emergency orders and weapons tests"
_LEADER = "A leader's own calls: firings, appointments and picks, orders, pardons, and a justice retiring"
_REGIME = "A ruler or regime falling"
_DEALS = "Ceasefires, deals and meetings between governments"
_LAUNCH = "Company launches and leadership changes (products, AI models, films, chief executives)"
_CODEALS = "Company deals: mergers, share-offering filings and index changes"
_CRYPTO = "Crypto token launches, airdrops and listings"
_CASTING = "Casting"
_LEAVING = "Elected leaders, party leaders and US politicians leaving"
_COURTS = "Charges, arrests and court rulings"
_TERRITORY = "Annexations, withdrawals and leaving alliances"
_AGENCIES = "Agencies, regulators and foreign governments' formal acts"
_PERSONAL = "Personal life and music releases"
_CAMPAIGN = "Candidacies and trips home"
_BANKRUPT = "Bankruptcies"
_CLASH = "Clashes and coups"
_TRIALS = "Trials with set dates"
_OPEN = "Space launches with public windows, science claims, hacks and open processes"
_DATED = "Dated answers decided in advance and held loosely"

# Documented cases (outside_cases.md, collected 2026-11-13), one sentence each, used by the kinds below.
_CASE_MILITARY = ("Strikes are planned ahead by a few officials and the soldiers who carry them out. A US Special "
                  "Forces soldier was charged in April 2026 over trades on the Maduro raid he helped plan, and an "
                  "Israeli Air Force reservist was indicted in February 2026 over trades on Israel's June 2025 "
                  "strike on Iran.")
_CASE_COMMAND = ("Military orders are drawn up by a small command before anyone outside hears of them. Israeli Air "
                 "Force personnel were indicted in 2026 for passing on secret information about the timing of strikes "
                 "on Iran, which they had traded on, according to Ynetnews.")
_CASE_PARDONS = ("Decisions like this are known to a few aides before they are announced. Two accounts made about "
                 "$300,000 trading on Biden's pardons, according to NPR.")
_CASE_PICK = ("Picks like this are known to the person choosing and a few advisers until they are announced. The "
              "Wall Street Journal reported that a tip about JD Vance's selection as the 2024 running mate is under "
              "investigation; the people named deny it.")
_CASE_MADURO = ("A ruler's fall is planned by a few people inside or abroad. A US Special Forces soldier was charged "
                "in April 2026 over trades on the raid that removed Maduro, which he helped plan.")
_CASE_MEETING = ("Meetings are arranged by a few staff on each side days ahead. Five new accounts took positions on a "
                 "Trump-Zelensky meeting within hours of each other, the day before Zelensky announced it in "
                 "December 2025, according to the Guardian.")
_CASE_CEASEFIRE = ("Deals between governments are settled by a few negotiators before they are announced. About 50 "
                   "new accounts took positions on the US-Iran ceasefire in the hours before Trump announced it in "
                   "April 2026, according to the Guardian.")
_CASE_LAUNCH = ("Launch dates are known to a company's leaders and launch team first. Thirteen new accounts put "
                "$309,486 on OpenAI's browser in the 40 hours before it launched in October 2025, according to "
                "Gizmodo.")
_CASE_MODEL = ("Release dates are known to a lab's launch team and its testers first. OpenAI fired an employee in "
               "February 2026 over trades on one of its model launches, as Wired reported.")
_CASE_EXEC = ("A chief executive's change is settled by the board before it is announced. Unusual Whales flagged a "
              "new account that made about $16,000 on Sam Altman's return to OpenAI in November 2023.")
_CASE_DEALS = ("Deals are prepared for weeks by both companies and their bankers. Lawyers at Debevoise list mergers "
               "and deals among the events at high risk of insider trading on prediction markets; no Polymarket "
               "case is documented yet.")
_CASE_NONE_INDEX = ("No Polymarket case of this kind is documented. It is watched because a committee of about ten "
                    "people decides and chooses when to announce.")
_CASE_NONE_CRYPTO = ("No Polymarket case of this kind is documented. It is watched because a handful of founders "
                     "decide when it happens, with no disclosure rules.")
_CASE_NONE_CASTING = ("No Polymarket case of this kind is documented. It is watched because studios, producers and "
                      "agents are a loose group who know the choice before it is announced.")
_CASE_NONE_FILM = ("No Polymarket case of this kind is documented. Studios began barring their crews from prediction "
                   "markets in 2026, after suspected leaks on Survivor.")
_CASE_GROUP2 = ("This goes through a formal process or several sides, and no case of trading ahead of one like it is "
                "documented.")
_CASE_COURT = ("Rulings come from judges and their clerks on the court's own calendar. No case of trading ahead of a "
               "court ruling has been reported, including on the Supreme Court's tariff ruling.")
_CASE_CHARGES = ("Charges go through prosecutors and a grand jury sworn to secrecy, and no case of trading ahead of "
                 "one is documented.")
_CASE_PERSONAL = ("News like this is known to a small circle first. Mitts and Ofir's 2026 study of informed trading "
                  "on Polymarket counts Taylor Swift's engagement among its examples.")
_CASE_GROUP3 = "Nobody holds an answer like this in a way the news can show, and no case of this kind is documented."
_CASE_JURY = ("The votes are cast before the ceremony. France Football filed a police complaint over trades on "
              "the 2026 Ballon d'Or, whose result three to five people knew, and the Nobel Institute concluded that "
              "cyber espionage most likely explained the overnight run on the 2025 Peace Prize winner.")
_CASE_LIST = ("The list is compiled from the company's own data before it is published. A Google engineer was "
              "charged by the Justice Department in May 2026 over trades on Google's 2025 Year in Search list.")
_CASE_MAGAZINE = ("The pick is printed before it is announced. TIME's 2025 Person of the Year cover leaked on "
                  "Polymarket's public Discord the day before, according to 404 Media.")
_CASE_TAPED = ("The result is filmed months before it airs. Kalshi is investigating trading on the Survivor 50 "
               "winner, decided eight months before the finale, and Paramount is adding prediction-market language "
               "to its crews' agreements.")
_CASE_CONTENT = ("The content is set before it comes out. Twelve accounts took positions on Axiom before ZachXBT "
                 "published his planned exposé in February 2026, and the top eight made more than $1.2 million, "
                 "according to on-chain analysts.")
_CASE_SETLIST = ("A show is planned and rehearsed in advance. A new account put $32,000 on the first song at the "
                 "2026 Super Bowl halftime show, and Kalshi treated leaks from the performer's side as part of the "
                 "market's risk, according to Popular Information.")
_CASE_SCRIPTED = ("The words are written before they are said. A former White House teleprompter operator settled "
                  "with the CFTC in August 2026 over trades on the words of Trump's speeches, and Kalshi fined a "
                  "MrBeast video editor in February 2026.")


def _k(key, group, section, slots, question, settles, who_knows, why, place="any_day"):
    return Kind(key, group, section, place, tuple(slots), question, settles, who_knows, why)


KINDS = {k.key: k for k in (
    # group 1: a few people decide and choose the moment
    _k("planned_strike_or_op", 1, _MILITARY, ("subject", "what"),
       "Will {subject} carry out {what}?", "{subject} carries out {what}",
       "If it happens, {people_list} and the officers planning it will know before the public does.", _CASE_MILITARY),
    _k("emergency_measure", 1, _MILITARY, ("subject", "what"),
       "Will {subject} order {what}?", "{subject} orders {what}",
       "If it happens, {people_list} and the officials drawing up the order will know before the public does.",
       _CASE_COMMAND),
    _k("weapons_test", 1, _MILITARY, ("subject", "what"),
       "Will {subject} carry out {what}?", "{subject} carries out {what}",
       "If it happens, {people_list} and the weapons programme's staff will know before the public does.", _CASE_COMMAND),
    _k("us_executive", 1, _LEADER, ("subject", "what"),
       "Will {subject} announce {what}?", "{subject} announces {what}",
       "If it happens, {people_list} and a few aides will know before the public does.", _CASE_PARDONS),
    _k("us_official", 1, _LEADER, ("subject", "post", "decider"),
       "Will {subject} leave {post}?", "{subject} leaves {post}",
       "If {short} goes, {people_list} and a few aides will know before the public does.", _CASE_PARDONS),
    _k("pardon_or_release", 1, _LEADER, ("subject", "decider"),
       "Will {subject} be pardoned or freed?", "{subject} is pardoned or freed",
       "If it happens, {people_list} and a few lawyers will know before the public does.", _CASE_PARDONS),
    _k("judge", 1, _LEADER, ("subject", "post"),
       "Will {subject} retire from {post}?", "{subject} announces a retirement from {post}",
       "If it happens, {people_list}, family and a few clerks will know before the public does.", _CASE_PARDONS),
    _k("personnel_pick", 1, _LEADER, ("post", "decider"),
       "Who will be named {post}?", "{decider} announces who will be {post}",
       "Until it is announced, {people_list} and a few advisers will know the choice before the public does.", _CASE_PICK),
    _k("autocrat_or_regime", 1, _REGIME, ("subject",),
       "Will {subject} lose power?", "{subject} is removed from power or leaves it",
       "If it happens, {short}'s inner circle, or the officials abroad planning it, will know before the public does.",
       _CASE_MADURO),
    _k("meeting_or_visit", 1, _DEALS, ("subject", "what"),
       "Will {subject} hold {what}?", "{subject} holds {what}",
       "If it happens, a few staff around {people} will know the date before the public does.", _CASE_MEETING),
    _k("ceasefire_peace", 1, _DEALS, ("subject", "what"),
       "Will {subject} agree to {what}?", "{what} is agreed by {subject}",
       "If it happens, the mediators and the negotiators for {people} will know before the public does.",
       _CASE_CEASEFIRE),
    _k("agreement_or_recognition", 1, _DEALS, ("subject", "what"),
       "Will {subject} announce {what}?", "{what} is announced by {subject}",
       "If it happens, the negotiators for {people} will know before the public does.", _CASE_CEASEFIRE),
    _k("product_launch", 1, _LAUNCH, ("subject", "what"),
       "Will {subject} launch {what}?", "{subject} launches {what}",
       "If it happens, {people}'s leaders and launch team will know the date before the public does.", _CASE_LAUNCH),
    _k("ai_model_release", 1, _LAUNCH, ("subject", "what"),
       "Will {subject} release {what}?", "{subject} releases {what}",
       "If it happens, {people}'s leaders, launch team and early testers will know the date before the public does.",
       _CASE_MODEL),
    _k("company_exec", 1, _LAUNCH, ("subject", "post", "decider"),
       "Will {subject} step down as {post}?", "{subject} leaves {post}",
       "If it happens, {people} will know before the public does.", _CASE_EXEC),
    _k("company_exec_pick", 1, _LAUNCH, ("subject", "decider"),
       "Will {subject} name {what}?", "{subject} names {what}",
       "If it happens, {people_list} and the person chosen will know before the public does.", _CASE_EXEC),
    _k("film_tv_book", 1, _LAUNCH, ("subject", "what"),
       "Will {subject} announce {what}?", "{subject} announces {what}",
       "If it happens, {people} will know before the public does.", _CASE_NONE_FILM),
    _k("merger_acquisition", 1, _CODEALS, ("subject", "decider"),
       "Will {subject} announce {what}?", "{what} is announced by {subject}",
       "If it happens, {people_list}, both boards and their bankers will know before the public does.", _CASE_DEALS),
    _k("ipo_filing", 1, _CODEALS, ("subject",),
       "Will {subject} go public?", "{subject} completes its initial public offering",
       "If it happens, {people}'s leaders, bankers and lawyers will know the timing before the public does.",
       _CASE_DEALS),
    _k("index_inclusion", 1, _CODEALS, ("subject", "post"),
       "Will {subject} join {post}?", "{subject} is added to {post}",
       "If it happens, the index committee will know before the public does.", _CASE_NONE_INDEX),
    _k("token_launch_or_airdrop", 1, _CRYPTO, ("subject",),
       "Will {subject} launch {what}?", "{subject} launches {what}",
       "If it happens, {people}'s founders, team and investors will know the date before the public does.",
       _CASE_NONE_CRYPTO),
    _k("company_stablecoin_or_coin", 1, _CRYPTO, ("subject", "what"),
       "Will {subject} launch {what}?", "{subject} launches {what}",
       "If it happens, {people_list} and its partners will know before the public does.", _CASE_NONE_CRYPTO),
    _k("exchange_listing", 1, _CRYPTO, ("subject", "other"),
       "Will {subject} be listed on {other}?", "{subject} is listed on {other}",
       "If it happens, {other}'s listing team and {short}'s team will know before the public does.",
       _CASE_NONE_CRYPTO),
    _k("casting", 1, _CASTING, ("post", "decider"),
       "Who will be cast as {post}?", "{decider} announces who will play {post}",
       "Until it is announced, {people_list}, the producers and the actors' agents will know the choice before the "
       "public does.", _CASE_NONE_CASTING),
    # group 2: a formal process or several sides
    _k("elected_head", 2, _LEAVING, ("subject", "post"),
       "Will {subject} leave {post}?", "{subject} leaves {post}",
       "If it happens, {people_list}, coalition partners and party leaders will know before the public does.", _CASE_GROUP2),
    _k("party_leader", 2, _LEAVING, ("subject", "post"),
       "Will {subject} leave {post}?", "{subject} leaves {post}",
       "If it happens, {people_list} and the party's inner circle will know before the public does.", _CASE_GROUP2),
    _k("us_politician", 2, _LEAVING, ("subject", "post"),
       "Will {subject} leave {post}?", "{subject} leaves {post}",
       "If it happens, {people_list}, family and close staff will know before the public does.", _CASE_GROUP2),
    _k("charge_or_arrest", 2, _COURTS, ("subject",),
       "Will {subject} be charged or arrested?", "{subject} is charged or arrested",
       "If it happens, the prosecutors, the grand jury and the investigators will know before the public does.",
       _CASE_CHARGES),
    _k("court_ruling", 2, _COURTS, ("decider", "what"),
       "Will {decider} rule {what}?", "{decider} rules {what}",
       "Until it is published, the judges and their clerks will know the ruling before the public does.", _CASE_COURT),
    _k("annex_or_withdraw", 2, _TERRITORY, ("subject", "what"),
       "Will {subject} carry out {what}?", "{subject} carries out {what}",
       "If it happens, {people}'s leaders will know before the public does.", _CASE_GROUP2),
    _k("bloc_or_relations", 2, _TERRITORY, ("subject", "what"),
       "Will {subject} announce {what}?", "{what} is announced by {subject}",
       "If it happens, {people}'s leaders will know before the public does.", _CASE_GROUP2),
    _k("agency_regulator", 2, _AGENCIES, ("subject", "what"),
       "Will {subject} announce {what}?", "{what} is announced by {subject}",
       "If it happens, {people}'s officials will know before the public does.", _CASE_GROUP2),
    _k("foreign_government", 2, _AGENCIES, ("subject", "what"),
       "Will {subject} announce {what}?", "{what} is announced by {subject}",
       "If it happens, {people}'s leaders will know before the public does.", _CASE_GROUP2),
    _k("relationship_family", 2, _PERSONAL, ("subject", "what"),
       "Will {subject} announce {what}?", "{what} is announced by {subject}",
       "If it happens, {people_list} and their close circle will know before the public does.", _CASE_PERSONAL),
    _k("music_release", 2, _PERSONAL, ("subject", "what"),
       "Will {subject} release {what}?", "{subject} releases {what}",
       "If it happens, {people_list}, the label and management will know the date before the public does.", _CASE_GROUP2),
    _k("candidacy_or_party", 2, _CAMPAIGN, ("subject", "what"),
       "Will {subject} announce {what}?", "{what} is announced by {subject}",
       "If it happens, {people_list} and close staff will know the date before the public does.", _CASE_GROUP2),
    _k("travel_return", 2, _CAMPAIGN, ("subject", "what"),
       "Will {subject} make {what}?", "{subject} makes {what}",
       "If it happens, {people_list} and their security staff will know the date before the public does.", _CASE_GROUP2),
    _k("bankruptcy_failure", 2, _BANKRUPT, ("subject",),
       "Will {subject} file for bankruptcy?", "{subject} files for bankruptcy or fails",
       "If it happens, {people}'s board, lenders and advisers will know before the public does.", _CASE_GROUP2),
    # group 3: nobody holds it in a way the news can show
    _k("clash", 3, _CLASH, ("subject",),
       "Will {subject} have {what}?", "{what} between {subject}",
       "If it is planned, {people}'s commanders will know first; most clashes are not planned.", _CASE_GROUP3),
    _k("coup", 3, _CLASH, ("subject",),
       "Will there be {what} in {subject}?", "{what} in {subject}",
       "If it happens, the plotters will know before the public does.", _CASE_GROUP3),
    _k("trial_verdict_or_sentence", 3, _TRIALS, ("subject", "what"),
       "Will {subject} be {what}?", "{subject} is {what}",
       "Until it is read out, the jury or the judge will know the verdict before the public does.", _CASE_GROUP3),
    _k("space_launch", 3, _OPEN, ("subject", "what"),
       "Will {subject} carry out {what}?", "{subject} carries out {what}",
       "Launch windows are public, so {people}'s team will know the timing only a little before the public does.",
       _CASE_GROUP3),
    _k("science_claim", 3, _OPEN, ("what",),
       "Will {what} be confirmed?", "{what} is confirmed",
       "If it happens, the researchers and the people checking the claim will know before the public does.",
       _CASE_GROUP3),
    _k("hack_or_flaw", 3, _OPEN, ("subject", "what"),
       "Will {subject} be hit by {what}?", "{subject} is hit by {what}",
       "If it happens, the attackers or the researchers who find it will know first.", _CASE_GROUP3),
    _k("open_process", 3, _OPEN, ("what",),
       "Will {what} happen?", "{what} happens",
       "It happens in the open, so no small group will know it much before the public does.", _CASE_GROUP3),
    # the dated place: decided in advance, held loosely, revealed at a set time
    # the dated kinds name no one the model wrote: who knows is fixed by the kind, or the subject the
    # market's own words name (a magazine, a company)
    _k("jury_prize", 1, _DATED, ("what",),
       "{which} wins {what}?", "the winner of {what} is announced",
       "The votes are cast before the result is announced, so the jurors and the vote counters will know it first.",
       _CASE_JURY, "dated"),
    _k("company_list", 1, _DATED, ("subject", "what"),
       "{which} is on {what}?", "{subject} publishes {what}",
       "{subject} compiles the list before publishing it, so the staff with access to it will know it first.",
       _CASE_LIST, "dated"),
    _k("magazine_pick", 1, _DATED, ("subject", "what"),
       "{which} is {what}?", "{subject} announces {what}",
       "The pick is made before the issue is printed, so {subject}'s editors and print staff will know it first.",
       _CASE_MAGAZINE, "dated"),
    _k("taped_tv", 1, _DATED, ("what",),
       "{which} wins {what}?", "the winner of {what} is shown",
       "The result is filmed before it airs, so the cast and crew will know it first.", _CASE_TAPED, "dated"),
    _k("finished_content", 1, _DATED, ("question",),
       "{question}", "the content comes out and answers it: {question}",
       "The content is finished before it comes out, so the people who made it will know it first.",
       _CASE_CONTENT, "dated"),
    _k("show_setlist", 1, _DATED, ("question", "what"),
       "{question}", "{what} takes place and answers it: {question}",
       "The show is planned and rehearsed in advance, so the performers, dancers and crew will know it first.",
       _CASE_SETLIST, "dated"),
    _k("scripted_words", 1, _DATED, ("subject", "what"),
       "Will {subject} say {what}?", "{subject} says {what}",
       "The words are written before they are said, so {subject}'s writers and staff will know them first.",
       _CASE_SCRIPTED, "dated"),
)}
DEFAULTS = {"which": "Who", "what": {"merger_acquisition": "a merger", "company_exec_pick": "a new chief executive",
                                      "token_launch_or_airdrop": "its token", "clash": "a military clash",
                                      "coup": "a coup"}}
DATED_KINDS = frozenset(k for k, v in KINDS.items() if v.place == "dated")
ANY_DAY_KINDS = frozenset(k for k, v in KINDS.items() if v.place == "any_day")

Filing = namedtuple("Filing", "kind group place how")


def group_of(kind):
    """The kind's group (1, 2 or 3); an unfiled market (None) counts in group 2."""
    return KINDS[kind].group if kind in KINDS else UNFILED_GROUP


# The word rules. Each is (kind or exclusion reason, regex) on "<event title> || <first question>",
# checked in order; the first match files the event. Ported from the proposal's filing (p02_kinds.py,
# p03_profile.py) with the review's corrections (final.md section 2): joke markets out; picks by one
# leader or a small body are personnel picks, not votes; Google's lists go to the dated place; a
# "ceasefire continues through" market is filed by the strike that would end it; and no default kind.
# On the Nov 13 pool they agree with the proposal's hand-checked filing on all but a few dozen of 1,087
# events; the ones they cannot place go to the model or a person.
JOKE = _R(r"flip the bird|wear a yarmulke|publicly insult|trump insult|tweet \"|jesus christ|nothing ever happens|"
          r"confirmed to be satoshi|ai be charged|goes to space|clavicular wear|aliens exist|uponly podcast|"
          r"satoshi('s identity| move)|renames? ice to nice|\$250 bill|strait of trump|rename an ocean|"
          r"new mexico to new america|rename itself|insult hegseth")
LIVE_VOTE = _R(r"dancing with the stars|american idol|the voice\b|love island|gmgn ggc|fan vote|public vote|"
               r"people'?s choice|\beurovision\b")
JURY = _R(r"\bjury\b|\bjuries\b")
PICK = _R(r"vp nominee|running mate|vice[- ]presidential (nominee|pick)|secretary-general of the u|"
          r"secretary general of the u|next israeli minister of|next president of germany|press secretary (announced|"
          r"named|pick)|next (white house )?press secretary|next fed chair|fed chair nominee|who will trump nominate")

DATED_RULES = (
    ("company_list", COMPANY_LIST),
    ("magazine_pick", _R(r"person of the year|time 100|sexiest man alive")),
    ("taped_tv", _R(r"bachelorette|the bachelor\b|\bsurvivor (\d+|season)|the traitors")),
    ("jury_prize", _R(r"\boscars?\b|academy awards|grammys?|cma (new artist|awards?|entertainer)|\bheisman\b|"
                      r"turner prize|\bnobel\b|ballon d'?or|golden globes?|\bemmys?\b|booker prize|pulitzer|"
                      r"eurovision.*\bjury\b|\bjury (winner|vote)\b")),
    ("show_setlist", _R(r"halftime show|\bset ?list\b|(first|last|opening) song")),
    ("finished_content", _R(r"\bgta (vi|6)\b|\bwitcher\b|avengers: doomsday.*\bappear|which characters will appear|"
                            r"who will die in|post-credits")),
    ("scripted_words", re.compile(r"(?=.*\b(say|says|said|mention|mentions)\b)(?=.*(" + SCRIPTED.pattern + r"))",
                                  re.I)),
)
# (reason, rule, unless): left out of the open pool by its words.
OPEN_EXCLUDE = (
    ("game or sport", _R(r"pro football|\bafc |\bnfc |\bufc\b|fight next|\bnba\b|\bnhl\b|\bmlb\b|premier league|"
                         r"champions league|world cup|super bowl (winner|champion)"), None),
    ("price or valuation", _R(r"market cap|largest company|richest person|valuation|treasury yield|bond yield|"
                              r"eur/usd|gbp/usd|usd/jpy|usd/krw|nikkei|s&p 500 \(spx\)|mortgage rate|all time high|"
                              r"kospi|median home value|gpu rental|iranian rials|exchange rate|circuit breaker|depeg|"
                              r"liquidation|open interest|mindshare|revenue hit|card sell for more|national debt|"
                              r"diesel stocks|closing price|open price|highest ipo|largest ipo|hit __ by|"
                              r"\$1t\+? (ipo|valuation)|bubble burst|odds hit|bull run combo"), None),
    ("public count or ranking", _R(r"best ai|second-best|third-best|best chinese ai|humanity.s last exam|"
                                   r"arena (score|debut|rank)|text arena|hle-diamond|top ___ ai|first hit 1550|"
                                   r"odds leader|nomination odds|flips vance|approval rating|rotten tomatoes|"
                                   r"highest grossing|opening week|how many|gold cards|\bmillion\b|commitments for|"
                                   r"raised on coinbase|token sales raise|verified on world|aircraft carriers in centcom|"
                                   r"traffic returns? to normal|hormuz traffic|which company has the best|"
                                   r"\bhold \$?\d|holds more than|production reach"), None),
    ("election or vote", _R(r"presidential nominee|nominee 20\d\d|next prime minister of|next mayor|next premier|"
                            r"next chancellor of|which party will win|coalition will form|parties will be in|"
                            r"next israeli government|israeli government formed|by-elections?|\belections?\b|"
                            r"senators? (will )?vote|reconciliation bill|override any veto|congress approves|"
                            r"legislation becomes law|\bbill\b.*\b(pass|law|enact)|stock trading ban|enact\w*|"
                            r"daylight sav|\bconvict\b|impeach|no-confidence|vote of confidence|data center moratorium|"
                            r"national budget|government shutdown|parliament dissolved|house of representatives "
                            r"dissolved|new constitution|millionaire tax|minimum wage|unsc resolution|"
                            r"next us president's age|be part of (the next|a german)|chairman of the council|"
                            r"how many uk prime ministers|clarity act|nationalize elections|repeal presidential|"
                            r"cap on gambling|referendum|\bprimary\b|runoff|draft authorized"), None),
    ("builds up in public", _R(r"russia (capture|enter)s?|ukraine re-?enters?|recapture crimea|houthis (capture|enter aden)|"
                               r"rebels capture|yemeni forces enter|which cities will russia"), None),
    ("nature or disease", _R(r"meteor|pandemic|natural disaster|ebola|hantavirus|plague|el niño|lake mead|lake powell|"
                             r"great salt lake|mississippi|cyclones?|west nile|covid|measles|bird flu|h5n1"),
     _R(r"vaccine")),
)
_US = r"(?=.*\b(trump|us|u\.s\.|united states|white house|federal|treasury|pentagon|congress)\b)"
ANY_DAY_RULES = (
    # the review's corrections first
    ("planned_strike_or_op", _R(r"ceasefire.*\b(continues?|holds?|remains?|through)\b")),
    ("us_official", _R(r"press briefing|new role in the trump")),
    ("personnel_pick", PICK),
    ("annex_or_withdraw", _R(r"\b(acquire|annex|buy|purchase|take|control)\w*\b.{0,30}\b(greenland|panama canal|canada)\b|"
                             r"51st state|\bannex\w*")),
    ("us_executive", _R(_US + r".*\b(take|takes|taking) a stake")),
    # crypto
    ("exchange_listing", _R(r"listed on|tokenized stocks|list on (coinbase|binance|robinhood)")),
    ("company_stablecoin_or_coin", _R(r"stablecoin|launch a coin")),
    ("token_launch_or_airdrop", _R(r"launch a token|airdrop|\btge\b|token launch|\bfdv\b")),
    # AI models and company deals
    ("ai_model_release", _R(r"\b(claude|gpt|gemini|grok|kimi|deepseek|llama|mistral|muse spark|astra|sol \(|fable)\b"
                            r".*\b(releas\w*|launch\w*|announc\w*)|when will gpt")),
    ("index_inclusion", _R(r"(added to|join|joins|inclusion in) (the )?s&p 500|s&p 500 inclusion|"
                           r"added to the (nasdaq|dow)")),
    ("ipo_filing", _R(r"\bipos?\b|go(es)? public|initial public offering")),
    ("bankruptcy_failure", _R(r"bankruptcy|insolvent|bank failure|banks? (will )?fail|wind-down|defaults? on")),
    ("merger_acquisition", _R(r"\bacquire[sd]?\b|\bmerger\b|\bmerge\b|takeover|buyout|\bbuys? (a )?stake|"
                              r"take a stake")),
    # people leaving
    ("judge", _R(r"\balito\b|clarence thomas|supreme court vacancy|de moraes|justice .* (retire|out)")),
    ("company_exec_pick", _R(r"new .{0,20}\bceo\b|next ceo|ceo named|be the next ceo|next .{0,20} ceo")),
    ("company_exec", _R(r"\bceos?\b.*\bout\b|\bout as .{0,30}\bceo\b|ceos will be out|step down as ceo|kalanick|"
                        r"florentino|ari weinstein|kimberly powell")),
    ("party_leader", _R(r"\b(poilievre|farage|polanski|starmer|badenoch|deri)\b.*\b(out|resign|leader)\b|"
                        r"party leader .*out|out as .{0,20}party leader|burnham cabinet")),
    ("us_official", _R(r"\b(hegseth|rubio|lutnick|rfk|kennedy jr|kash patel|pirro|bondi|noem|vance|powell|lisa cook|"
                       r"warsh|bessent|gabbard|wiles|leavitt|miller|zacharia)\b.*\b(out|leave|resign|fired|removed)\b|"
                       r"leave (the )?trump administration|rejoin the trump|tries to fire|try to fire|"
                       r"cabinet (member|secretary) .*(out|leave)|secretary of \w+ .*\bout\b|\bout as secretary\b")),
    ("autocrat_or_regime", _R(r"\b(xi jinping|putin|kim jong un|khamenei|maduro|delcy|díaz-canel|diaz-canel|"
                              r"bin salman|kadyrov|al-sharaa|goïta|nguesso|tshisekedi|lukashenko|erdoğan|erdogan|"
                              r"ortega|pezeshkian|ghalibaf|araghchi|qassem|abbas)\b.*\b(out|removed|ousted|"
                              r"leave|step down|lose power|no longer)\b|regime falls?|regime fall|leadership change|"
                              r"next leader out|leader end of|de facto leader|supreme leader|out as leader of|"
                              r"\blead iran\b")),
    ("us_politician", _R(r"\b(fetterman|mcconnell|mamdani|walz|ken martin|schumer|pelosi|aoc)\b.*\b(out|resign|"
                         r"leave|steps? down)\b|trump (resign|out|removed)|will trump resign|25th amendment|"
                         r"out as us rep|\bus rep\b.*\bout\b")),
    ("elected_head", _R(r"\bout as .{0,30}(president|prime minister|pm|chancellor|premier)\b|\b(president|"
                        r"prime minister|pm|chancellor)\b.*\bout\b|\bout by\b|\bout before\b|\bout in 20\d\d|"
                        r"resigns? as")),
    # courts and charges
    ("pardon_or_release", _R(r"pardon|commute|released from (custody|prison)|out of (custody|prison)|clemency")),
    ("trial_verdict_or_sentence", _R(r"\bconvicted\b|found guilty|\bguilty\b|prison time|sentenced|\bjailed\b|"
                                     r"disbarred|acquitted|foul play")),
    ("court_ruling", _R(r"scotus|supreme court rules?|courts? (block|strike|rule)|international court|court of justice|"
                        r"\bicj\b|ruling")),
    ("charge_or_arrest", _R(r"\bcharged\b|federally charges|charges (against|filed)|\barrested\b|\bindicted\b|"
                            r"\bindictment\b|\bcustody\b|extradit\w*|denaturaliz\w*|citizenship revoked|\braided\b|"
                            r"arrest warrant|\bpurge\b")),
    # military
    ("coup", _R(r"\bcoup\b")),
    ("clash", _R(r" x .*\bclash|clash with each other|military (engagement|clash)|border clash")),
    ("weapons_test", _R(r"nuclear test|\bnuke\b|ballistic missile|missile test|\bicbm\b|detonat\w*")),
    ("emergency_measure", _R(r"airspace|ben gurion|evacuat\w*|internet blackout|state of (emergency|siege)|"
                             r"insurrection act|mobiliz\w*|article [45]\b|bab el-mandeb|blockade ends|"
                             r"end of .{0,20}blockade|warships|\buss |undersea|martial law|curfew|national guard")),
    ("bloc_or_relations", _R(r"leave (nato|brics|opec|the gulf|the eu)|leaves? the eu|withdraws? from (the )?(eu|nato|"
                             r"npt|opec|brics)|\bdissolves\b|\bsever\b|\bexpels?\b|add a new member|"
                             r"coalition breaks?|rejoin the g7|rejoins? (nato|the eu|opec|brics)")),
    ("annex_or_withdraw", _R(r"withdraws? (from|its)|withdraw from|panama canal")),
    ("planned_strike_or_op", _R(r"\bstrikes?\b|\binvade\w*|invasion|ground (operation|offensive|incursion)|"
                                r"\bblockade\b|military (action|operation|intervention)|\btroops\b|"
                                r"intervention in|forces in|declare war|\btarget\b|kharg island|perim island|"
                                r"capture another world leader|\battack\w*|\bbomb\w*|\braid\b|drone strike|"
                                r"airstrike|send (troops|forces)|boots on the ground|deploy\w*|houthis enter")),
    # people (before the generic launch words)
    ("casting", _R(r"james bond|\b007\b|gwen stacy|miles morales|cast as|casting|to play\b")),
    ("music_release", _R(r"release (a |an )?(new )?(song|album|single|ep)s?\b|release songs|new album|"
                         r"release new albums|\bcomeback\b|new single")),
    ("candidacy_or_party", _R(r"announce (a )?(presidential )?run|announce presidential|won't run|forms? (a )?party|"
                              r"leave the national|leaves the democrats|run for (president|office|public office|"
                              r"senate|governor)|"
                              r"\bendorse\w* .{0,40}\b(president|presidential|candidate|senate|governor|mayor)\b")),
    # diplomacy
    ("meeting_or_visit", _R(r"\bmeets?\b|\bmeeting\b|talk to|speak to|\bvisits?\b|seen together|attend a round|"
                            r"direct talks|summit|phone call|call with")),
    ("ceasefire_peace", _R(r"ceasefire|peace deal|\bcede\b|give up the rest|not to join nato|limit size of|"
                           r"security guarantee|peacekeep\w*|boryspil|disarm|truce|armistice|recogni\w* russian")),
    ("agreement_or_recognition", _R(r"normali[sz]e|recogni[sz]\w*|nuclear deal|trade deal|agreement|\bdeal\b|"
                                    r"agree to|abraham accords|mecca agreement|join brics|caatsa|iaea|enrichment|"
                                    r"enriched uranium|embassy|lower tariffs|usmca|nord stream|falklands|"
                                    r"endorses china|f-35 program")),
    # government acts
    ("us_executive", _R(_US + r".*\b(executive order|capital gains|bitcoin reserve|ethereum reserve|stake in|"
                        r"tariff increase|tariffs? on|sanctions on|diesel|sanction relief|hormuz fees|public access|"
                        r"chinese military companies|treasury transactions|department of education|corporate taxes|"
                        r"declassif\w*|\bveto\b|dividend|ban\b|bans\b|imports?|eliminates?|create)")),
    ("us_executive", _R(r"\btrump\b|tariff increase|chinese military companies|bitcoin reserve|ethereum reserve|"
                        r"department of education|capital gains tax|corporate taxes|treasury transactions")),
    ("agency_regulator", _R(r"\bsec\b|\bfda\b|\bfcc\b|\bftc\b|\bdoj\b|\bepa\b|weed resched|reschedul\w*|"
                            r"bla submitted|nuclear (reactor|license)|debt downgrade|moody'?s|banned in any|"
                            r"approved by|\bapproves?\b")),
    # companies
    ("space_launch", _R(r"starship|\bneutron\b|doge-1|data center in space|orbital|launch window|new glenn")),
    ("science_claim", _R(r"millennium prize|riemann|cmi declare|exponential time|math problems?|\bagi\b|imo gold|"
                         r"room-temperature|superconduct\w*")),
    ("hack_or_flaw", _R(r"exploited|security flaw|\bhack(ed)?\b|lab leak|data breach|vulnerab\w*")),
    ("open_process", _R(r"sha-256|vaccine|clinical trial")),
    ("film_tv_book", _R(r"\bdelayed\b|stranger things|winds of winter|\btrailer\b|\bsequel\b|"
                        r"season \d+ (release|premiere)|episode released")),
    ("product_launch", _R(r"\blaunch(es|ed)?\b|\brelease[sd]?\b|\bunveil\w*|waymo|\brobotaxi\b|browser|\bdevice\b|"
                          r"\bglasses\b")),
    ("relationship_family", _R(r"engaged|engagement|pregnan\w*|\bsplit\b|divorce|\bbaby\b|wedding|propose|"
                               r"\baffair\b|dating|married|converted to|join (a new company|spacex)")),
    ("travel_return", _R(r"back in|returns? to|return home|enter (iran|venezuela|cuba)|travel to")),
    ("foreign_government", _R(r"\bdeclares?\b|independence|accuses|dollariz\w*|unban|\bbans?\b|\bdefault\w*|"
                              r"buy bitcoin|public appearance|appears? in public|declassif\w*|charges .* fees")),
)


def event_text(title, question=""):
    """The text the word rules read: the event's title and its first question,
    typographic quotes and dashes folded (normalise_title)."""
    return normalise_title(f"{title or ''} || {question or ''}")


def open_exclusion(title, question=""):
    """Why the open pool leaves an event out by its words, or None. The
    reasons follow final.md's "Left out" table: embargoed releases, joke
    markets and unscripted public speech, live public votes, games, prices,
    public counts and rankings, elections and votes, ground taken in a war
    (it builds up in public), nature and disease. A dated answer of a kind
    the dated place takes, and a pick by one leader or a small body, are not
    left out (file_event places them)."""
    t = event_text(title, question)
    why = is_embargoed(question, title)
    if why:
        return f"embargoed release: {why}"
    if JOKE.search(t) or (MENTION.search(t) and not SCRIPTED.search(t)):
        return "public speech or joke market"
    if LIVE_VOTE.search(t) and not JURY.search(t) and not re.search(r"particip", t, re.I):
        return "settled by a live public vote"
    if PICK.search(t) or any(rx.search(t) for _, rx in DATED_RULES):
        return None
    for why, rx, unless in OPEN_EXCLUDE:
        if rx.search(t) and not (unless and unless.search(t)):
            return why
    return None


def file_event(title, question=""):
    """Filing(kind, group, place, how) for one open event by its words:
    how is "excluded" (kind is then the reason), "rule" (a word rule placed
    it) or "unfiled" (kind None, group 2: the model or a person files it from
    KINDS; it never falls into a default kind)."""
    t = event_text(title, question)
    why = open_exclusion(title, question)
    if why:
        return Filing(why, None, None, "excluded")
    for kind, rx in DATED_RULES:
        if rx.search(t):
            return Filing(kind, KINDS[kind].group, "dated", "rule")
    for kind, rx in ANY_DAY_RULES:
        if rx.search(t):
            return Filing(kind, KINDS[kind].group, "any_day", "rule")
    return Filing(None, UNFILED_GROUP, None, "unfiled")


# ── 4. the card's sentences ──────────────────────────────────────────────────

def _words(name):
    return {w for w in re.findall(r"[\w'’.-]+", name.lower()) if w != "the"}


def _unique(names):
    """The names, a repeat left out: the same name (case-blind), or a name
    whose words are all in an earlier one ("Trump" after "Donald Trump",
    "US" after "US Government")."""
    out = []
    for n in names:
        n = str(n or "").strip()
        if n and not any(_words(n) <= _words(o) for o in out):
            out.append(n)
    return out


# A list item said twice in a row once the names are in a sentence ("the producers, the producers and
# the actors' agents", when the decider is the template's own "the producers").
_REPEATED_ITEM = re.compile(r"\b((?:the |a |an )?[\w'’.-]+(?: [\w'’.-]+){0,4}), \1\b(?=,| and )", re.I)


def _and(names):
    """"A", "A and B", "A, B and C", repeats left out (case-blind)."""
    out = _unique(names)
    if len(out) <= 1:
        return out[0] if out else ""
    return ", ".join(out[:-1]) + " and " + out[-1]


def _list(names):
    """"A", "A, B": for a list a sentence ends itself ("A, B and a few aides")."""
    return ", ".join(_unique(names))


def _cap(s):
    return s[:1].upper() + s[1:]


# Who "people" names in a kind's who-knows sentence: the decider and the subject (a person leaving, a
# company launching), the decider alone (the subject is an outcome or a post: a pick, a strike), or
# both sides (a meeting, a deal, a clash).
_KNOWERS_DECIDER = frozenset({"personnel_pick", "casting", "jury_prize", "finished_content", "company_exec_pick",
                              "merger_acquisition", "planned_strike_or_op", "emergency_measure", "weapons_test"})
_KNOWERS_SIDES = frozenset({"meeting_or_visit", "ceasefire_peace", "agreement_or_recognition", "clash"})


def names_for(kind, names):
    """The slot values a kind's sentences take: the given names, the
    defaults (short and decider from the subject, which = "Who", a kind's
    usual outcome) and "people" / "people_list", who would know (see
    _KNOWERS_*; the "others" list is added after them, repeats left out).
    Raises ValueError naming a missing slot."""
    k = KINDS[kind]
    given = names or {}
    n = {s: " ".join(v.split()) for s, v in given.items() if isinstance(v, str) and v.strip()}
    for s in k.slots:
        if not n.get(s):
            raise ValueError(f"{kind} needs the {s!r} name ({SLOTS[s]})")
    n.setdefault("short", n.get("subject", ""))
    n.setdefault("decider", n.get("subject", ""))
    n.setdefault("which", DEFAULTS["which"])
    if kind in DEFAULTS["what"]:
        n.setdefault("what", DEFAULTS["what"][kind])
    others = [o for o in given.get("others") or [] if isinstance(o, str)]
    if kind in _KNOWERS_DECIDER:
        first = [n["decider"]]
    elif kind in _KNOWERS_SIDES:
        first = [n.get("subject", ""), n.get("other", "")]
    else:
        first = [n["decider"], n["short"]]
    n["people"] = _and(first + others)
    n["people_list"] = _list(first + others)     # for "{people_list} and a few aides": no "and" of its own
    return n


def card_text(kind, names):
    """The card's sentences for one market of a kind, from the names the
    market gives: {"question", "who_knows", "why_watched", "settles"}.
    question and who_knows carry the names; why_watched is the kind's fixed
    sentence naming a documented case; settles is the event that would settle
    the market, for the model's prompt. Raises ValueError on a missing name."""
    k = KINDS[kind]
    n = names_for(kind, names)
    return {
        "question": _cap(k.question.format(**n)),
        "who_knows": _cap(_REPEATED_ITEM.sub(r"\1", k.who_knows.format(**n))),
        "why_watched": k.why,
        "settles": _cap(k.settles.format(**n)),
    }


def subject_terms(names):
    """The words a headline must contain to name the market's main subject:
    the subject, its short form, its aliases, and each part of a subject
    joined by "and" / "or" / "x" / " - " (Tesla and SpaceX, Israel or Iran);
    a hyphen inside a name keeps it whole (Bankman-Fried)."""
    n = names or {}
    out = [n.get("subject"), n.get("short")] + list(n.get("aliases") or [])
    for s in (n.get("subject"), n.get("short")):
        out += re.split(r"\s+(?:and|or|x|vs\.?)\s+|\s+-\s+|\s*[–—/,&]\s*", str(s or ""), flags=re.I)
    return [t.strip() for t in out if isinstance(t, str) and len(t.strip()) >= 2]


# A short all-capitals name ("SBF", "OKX") is also a ticker or another body's initials ("SBF 120" is a
# French stock index): alone in a headline it names the subject only beside a word of the event.
_ACRONYM = re.compile(r"[A-Z][A-Z0-9&]{1,3}")
_CONTEXT_STOP = frozenset("""will with from that this than their there have has had which what when where into over
    after before more most news says said could would should about also been being does make makes made
    announce announces announced officially""".split())
# Kinds whose market names the outcome (a product, a model, a release, an order) rather than only who acts:
# a headline must carry the outcome's own word, since "Tesla" alone is not the Cybercab.
OUTCOME_KINDS = frozenset({"ai_model_release", "product_launch", "music_release", "film_tv_book", "emergency_measure",
                           "us_executive"})
_GENERIC_OUTCOME = frozenset("""the a an new next its his her their model models ai act order orders executive
    update version series edition product device album song single film movie show season""".split())


def _stem(w):
    return w[:5]


def _stems(text):
    return {_stem(w) for w in re.findall(r"[a-z0-9][a-z0-9'-]+", normalise_title(text).lower())
            if len(w) >= 3 and w not in _CONTEXT_STOP}


def match_names(kind, names, text=None):
    """The names a headline is checked against for a story of this kind
    (names_subject): the names plus "context", stems of the words of the
    event that would settle it (the kind's sentence and the news query, the
    subject's own words left out), and, for a kind whose market names the
    outcome (OUTCOME_KINDS), "require": the outcome's own words that the
    market writes ("Cybercab", "GPT-5.5"), one of which a headline must
    carry. text: the market's own words; an outcome word not in them is
    not required."""
    n = dict(names or {})
    k = KINDS.get(kind)
    subject = {_stem(w) for t in subject_terms(n) for w in re.findall(r"[a-z0-9'-]+", t.lower())}
    words = " ".join([re.sub(r"\{\w+\}", " ", k.settles) if k else "", str(n.get("search_event") or ""),
                      str(n.get("what") or ""), str(n.get("post") or "")])
    n["context"] = sorted(_stems(words) - subject)
    if kind in OUTCOME_KINDS and n.get("what"):
        terms = [m.group(0) for m in re.finditer(r"[A-Za-z][\w.+-]*\w", n["what"])
                 if (re.search(r"[A-Z]", m.group(0)) or re.search(r"\d", m.group(0))) and len(m.group(0)) >= 3
                 and m.group(0).lower() not in _GENERIC_OUTCOME
                 and _stem(m.group(0).lower()) not in subject]
        if text is not None:
            low = normalise_title(text).lower()
            terms = [t for t in terms if re.search(r"(?<!\w)" + re.escape(t.lower()) + r"(?!\w)", low)]
        if terms:
            n["require"] = terms
    return n


def _has(title, term):
    """A term in a headline as whole words, case-blind, typographic quotes
    folded; a name written with capitals does not match the same letters
    written all in lower case ("Discord" is not "as discord persists")."""
    t, w = normalise_title(title), normalise_title(term)
    for m in re.finditer(r"(?<!\w)" + re.escape(w.lower()) + r"(?!\w)", t.lower()):
        found = t[m.start():m.end()]
        if w == w.lower() or found != found.lower():
            return True
    return False


def has_context(title, context):
    """Whether a headline has a word of the event (match_names' "context" stems)."""
    return bool(_stems(title) & set(context or ()))


def names_subject(title, names):
    """True when a headline names the market's main subject (any of
    subject_terms, as whole words, case-blind but for a capitalised name
    written all in lower case, typographic quotes folded). With
    match_names' extras: a short all-capitals name alone counts only beside
    a word of the event ("context"), and a headline must also carry one of
    the outcome's words ("require") when the names give them."""
    n = names or {}
    hits = [term for term in subject_terms(n) if _has(title, term)]
    if not hits:
        return False
    if all(_ACRONYM.fullmatch(normalise_title(h)) for h in hits) and n.get("context"):
        if not has_context(title, n["context"]):
            return False
    req = n.get("require") or []
    return not req or any(_has(title, r) for r in req)


# Who decides, for the cap of two cards per decider (watchlist.opennow.decider_keys): the ways the model
# writes the same decider. "The President" is read as the US President, who decides most of these.
DECIDER_ALIASES = {
    "trump": frozenset({"president", "the president", "president trump", "donald trump", "trump",
                        "president donald trump",
                        "the white house", "white house", "the trump administration", "trump administration",
                        "the administration", "the us government", "us government", "the u.s. government",
                        "the us federal government", "us federal government", "the us president", "us president",
                        "the president of the united states", "potus"}),
}

# Stories one piece of news settles that the title rules cannot join (final.md's hand-checked merges,
# p03_profile.py MERGE, with the Israel x Hamas ceasefire split from Israel x Iran as final/f01_pool.py
# does). Groups that are one news thread rather than one story are not here (invasions of different
# Latin American countries, Greenland's purchase and its invasion, two carriers, Starship's separate
# milestones, two Hormuz agreements, two crypto reserves, separate maths problems): one piece of news
# would not settle them all. {event id: story key}; a person keeps this.
STORY_MERGES = {eid: key for key, ids in (
    ("trump leaves office", ("74050", "34349", "73969", "73176")),
    ("modi out", ("79221", "1162367")),
    ("zelenskyy out", ("34052", "1150959")),
    ("china invades or blockades taiwan", ("34044", "330223", "281145", "536282")),
    ("iranian regime falls", ("72347", "1116383", "1116384", "255195", "237598")),
    ("venezuela leader", ("143443", "448148", "145253")),
    ("cuban regime falls", ("259355", "143567")),
    ("hegseth out", ("330222", "1120129")),
    ("putin and zelenskyy meet", ("917892", "1100761", "1100695", "74056")),
    ("trump and putin meet", ("528413", "528389")),
    ("trump putin and zelensky meet", ("106940", "106938")),
    ("next us iran senior meeting", ("1066394", "1066485")),
    ("trump and mojtaba khamenei", ("555963", "259462")),
    ("next claude opus", ("1071679", "1274168")),
    ("gpt astra 6 1", ("965173", "1305832", "1230571")),
    ("next gemini flash lite", ("850742", "1274169")),
    ("next press secretary", ("1201095", "1200884")),
    ("next james bond", ("498879", "1149857")),
    ("next nvidia ceo", ("1367877", "1353337")),
    ("jensen huang affair", ("1374088", "1372280")),
    ("jensen huang joins a company", ("1367931", "1367884")),
    ("russia ukraine ceasefire", ("478472", "486199")),
    ("openai ipo", ("68543", "193867")),
    ("stripe acquires paypal", ("228549", "228565")),
    ("us iran nuclear deal", ("310530", "326475", "629791", "620025")),
    ("lindsay clancy verdict", ("961709", "872462", "964399")),
    ("maduro verdict", ("141075", "141230")),
    ("epstein disclosures charges", ("196716", "196215")),
    ("ilhan omar prosecution", ("1177705", "1177681", "912458", "1195263")),
    ("obama charged", ("73186", "73177")),
    ("diaz canel charged or in custody", ("496496", "257075")),
    ("us removes public access to an ai model", ("663847", "663848", "663849")),
    ("skin cancer vaccine", ("876500", "876728")),
    ("france emergency or siege", ("1211791", "1211834")),
    ("israeli airspace closed", ("1030155", "1279913")),
    ("trump endorses a candidate", ("117530", "117525")),
) for eid in ids}


# ── 5. headlines: step words, leaks, odds, sources, copies ───────────────────

# A story goes on the model's shortlist when a new headline has one of these (yes or no, never a count,
# so heavy coverage never crowds out a quiet story). final/f02_shortlist.py.
STEP_WORDS = _R(r"\b(prepar\w*|weigh\w*|consider\w*|shortlist\w*|finalists?|screen[- ]tests?|talks|negotiat\w*|"
                r"launch\w*|oust\w*|plans? to|planning|discuss\w*|narrow\w*|offer\w*|bid|deploy\w*|order(s|ed)?|"
                r"draft\w*|sources say|people familiar|officials say|reportedly|exclusive|mainnet|TGE|airdrop|"
                r"filing|files for|to announce|could be announced|set to)\b")
# A step reported through a leak ranks before one the deciders announced (final.md section 3): a leak
# shows the group holds the answer loosely. The code decides this, never the model.
LEAK_WORDS = _R(r"sources? say|: sources|people familiar|officials say|said to|reportedly|reports? says?|"
                r"bloomberg reports|exclusive|: report\b")
# Headlines that report odds or come from betting sites never reach the model. A percentage counts as
# odds unless it is plainly a rate of something else (50% tariffs, a 10% stake, 5% of jobs).
ODDS_WORDS = _R(r"\bodds\b|polymarket|kalshi|\bbettors?\b|\bbetting\b|\bbets?\b|bookies|bookmakers?|\bwagers?\b|"
                r"sportsbook|favou?rite|front-?runner|prediction markets?|implied probability")
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s?(?:%|percent\b|per cent\b)(?!\s+(?:tariffs?|duty|duties|levy|tax(?:es)?|stake|"
                      r"of\b|pay\b|raise|cut|rise|increase|drop|fall|jump|gain|loss|growth|inflation|interest|"
                      r"rate\b|owned|ownership|share of|of the|equity))", re.I)
BETTING_SOURCES = _R(r"kalshi|polymarket|\bodds|oddschecker|\bcovers\b|action network|bettingpros|sportsbook|"
                     r"predictionhunt|prediction news|betting|\bbet(s|\d+)?\b|wager|bookmaker")

# Source ranks for the card's headline: news agencies, then national papers and broadcasters, then the
# rest (within a rank, the newest first). Stops an obscure or machine-translated copy leading a card.
AGENCIES = _R(r"^(reuters|ap|ap news|associated press|the associated press|afp|agence france-presse|bloomberg|"
              r"bloomberg\.com|bloomberg news|anadolu|anadolu agency|anadolu ajansı)$")
NATIONAL = _R(r"^(wsj|the wall street journal|wall street journal|the new york times|new york times|nytimes|"
              r"the washington post|washington post|ft|financial times|the financial times|bbc|bbc news|cnn|"
              r"nbc news|nbc|cbs news|cbs|abc news|abc|fox news|npr|the guardian|guardian|politico|axios)$")


def is_odds(title, source="", percent=True):
    """True when a headline reports odds or comes from a betting site: it
    is removed before the model sees anything. percent=False leaves a bare
    percentage alone (the closed list's judge reads what happened, and "cuts
    prices 20%" is part of it)."""
    t = str(title or "")
    return bool(ODDS_WORDS.search(t) or (percent and _PERCENT.search(t)) or BETTING_SOURCES.search(str(source or "")))


def has_step_words(title):
    return bool(STEP_WORDS.search(str(title or "")))


def is_leak(title):
    return bool(LEAK_WORDS.search(str(title or "")))


def source_rank(source):
    """0 for a news agency, 1 for a national paper or broadcaster, 2 for the rest."""
    s = " ".join(str(source or "").split())
    return 0 if AGENCIES.match(s) else 1 if NATIONAL.match(s) else 2


_DATE_ONLY = re.compile(r"T0[78]:00(:00(\.0+)?)?(Z|\+00:00)?$")


def is_date_only(published):
    """Google News stamps a report its source dated but did not time at
    07:00 or 08:00 UTC exactly (midnight Pacific): read those as date-only."""
    return bool(_DATE_ONLY.search(str(published or "")))


def effective_ts(published):
    """Epoch seconds a headline counts from: its time, or the end of its day
    (23:59 UTC) when it is date-only, so a date-only report never counts
    before it could have been read. None when it has no usable time."""
    s = str(published or "").strip()
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    if is_date_only(s):
        t = t.astimezone(timezone.utc).replace(hour=23, minute=59, second=0, microsecond=0)
    return int(t.timestamp())


def card_headline(headlines):
    """The headline the card shows among the copied steps: the best source
    rank, then the newest. None for no headlines."""
    hs = [h for h in headlines or [] if h]
    if not hs:
        return None
    return min(hs, key=lambda h: (source_rank(h.get("source")), -(effective_ts(h.get("published")) or 0)))


_TYPO = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"', "‟": '"',
                       "–": "-", "—": "-", "‐": "-", "‑": "-", "−": "-", "…": "...", " ": " "})


def normalise_title(text):
    """A headline for comparing copies: NFKC, typographic quotes and dashes
    folded to ASCII, whitespace collapsed."""
    t = unicodedata.normalize("NFKC", str(text or "")).translate(_TYPO)
    return " ".join(t.split())


def match_copy(copy, headlines):
    """The given headline the model's copy is, word for word (case-blind,
    typographic quotes, dashes and spaces folded, a trailing " - Source"
    allowed), or None: a copy that matches nothing given counts as "none"."""
    c = normalise_title(copy).lower().strip(" \"'")
    if not c or c in ("none", "unclear"):
        return None
    for h in headlines or []:
        full = normalise_title(h.get("title")).lower()
        t = full.strip(" \"'")          # stripped like the copy, so a headline ending in a quote can match
        src = normalise_title(h.get("source")).lower()
        if c == t or (src and c == f"{full} - {src}".strip(" \"'")):
            return h
    return None
